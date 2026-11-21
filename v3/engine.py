"""v3 row engine. Fresh implementation, no v2 code.

Rules enforced here, not just documented:
- A flow with a "/start" send step is rejected, at validate time
  and at run. Flows begin with the target bot's task menu.
- The only sleeps in this file are: an explicit flow-authored
  "wait" step, the bot-configurable flow interval between rows, a
  timer's sleep until its user-supplied start time, and a 1 second
  history-poll step when no stream waiter is available. There is
  no step pause, no per-call limit, no check tick, and no
  per-account waiting scheme of any kind.
- Button matching is strict exact after emoji, punctuation and
  case normalisation only. No substring matching, no suffix
  filling. An ambiguous match or a missing guarded pre-paste
  option stops the slot with OptionRemoved; the row stays
  pending and any reservation is refunded.
- A provider rate-limit error is detected only through the
  helper in v3.pyrogram_target. It is never specially waited
  out, retried, or scheduled around: the slot stops, the row
  stays pending, and the run resumes only on a fresh Start.
- The moment the flow's pastes are complete for a row, that row
  is the point of no return: it is marked done and its
  reservation becomes the charge. A later step failing can
  never re-send it, and can never refund it either.
"""

import asyncio
import inspect
import logging
import os
import re
import time

try:
    from v3 import config as _config
except Exception:  # noqa: BLE001 - data layer may land after this file
    _config = None
try:
    from v3 import billing as _billing
except Exception:  # noqa: BLE001 - data layer may land after this file
    _billing = None
try:
    from v3 import store as _store
except Exception:  # noqa: BLE001 - data layer may land after this file
    _store = None
from v3.pyrogram_target import PyrogramTarget as _PyrogramTarget
from v3.target import StreamWaiter

log = logging.getLogger("tectonic-v3")


class OptionRemoved(Exception):
    pass


class RowFailed(Exception):
    pass


class OutOfCredits(Exception):
    pass


class NeedPassword(Exception):
    pass


class StopRun(Exception):
    """Halt the owner's whole run, menu-Stop semantics.

    Raised by the run-mode read-first rule when a step's button is
    still missing after the reread (or when the first-step 📋 Tasks
    recovery still finds no 📋 Tasks). Unlike OptionRemoved, which
    stops only its slot, StopRun ends every slot of the run: the
    rows stay pending, reservations are refunded, and the run ends
    exactly as if ⏹ Stop had been pressed.
    """


class ReplyTimeout(TimeoutError):
    """Run-mode reply bound expired: the target never answered.

    A hang, not a transport failure, so it never triggers a
    network retry. A TimeoutError subclass, so existing callers
    that catch TimeoutError keep working unchanged.
    """


# User-ordered 2026-11-01: the run-guard wait cap. A run-mode guard
# that misses its button rescans on every new incoming message or
# edit until the button appears or this cap expires — never a fixed
# sleep, never an unbounded wait. The wait runs only on a miss, so
# a found button returns at once and healthy rows run at full
# speed. The other hardcoded timings in v3 remain the per-flow
# interval (plus Timer datetimes, transport timeouts, and the 1s
# degraded-poll floor), the inter-step pause below, and the check
# probe wait below that.
GUARD_WAIT_SECONDS = 12

# User-ordered 2026-11-01: run-mode reply-wait bound. A step
# whose reply never arrives fails the row instead of freezing
# the slot (v2's hang-row-skip rule). Check mode keeps no bound
# here: the 60 s probe cap bounds it instead.
WAIT_REPLY_TIMEOUT_SECONDS = 45

# User-ordered 2026-11-01 (comparison item 6): guard settle
# interval, ported from v2's settle. A found button is trusted
# only after one poll interval (cut short by a fresh wakeup)
# plus a fresh re-scan, so a button that vanished in the
# interval is never clicked. The interval is exactly the
# degraded-poll floor, with no added variance.
SETTLE_WAIT_SECONDS = 1

# User-ordered 2026-11-01: hardcoded pause after every flow step
# except the last one, pastes included. It lands after the step
# fully completes (including its reply wait) and before the next
# step begins. Not configurable through the bot, by his order.
STEP_PAUSE_SECONDS = 1

# User-ordered 2026-11-01: Zara's check cadence. A watching or
# failed check probe re-runs every 60 seconds. A single probe is
# capped at the same 60 seconds, so a step whose reply never
# arrives can delay a re-check but never freeze the check. Two
# constants on purpose: the cap must stay above one probe's own
# duration (steps plus pauses), while the wait sets the cadence.
CHECK_PROBE_WAIT_SECONDS = 60
CHECK_PROBE_TIMEOUT_SECONDS = 60

# User-ordered 2026-11-01: extra attempts per row on a transport
# failure. A row that fails on a transport error is attempted
# 1 + ROW_NETWORK_RETRIES times in total, then fails like any
# other failed row. User-ordered 2026-11-01 (comparison item 5):
# the wait before each retry is ROW_RETRY_BACKOFF_SECONDS times
# the attempt number, with no added variance (v2's spacing,
# v3's variance-free rule).
ROW_NETWORK_RETRIES = 2
ROW_RETRY_BACKOFF_SECONDS = 15

# User-ordered 2026-11-09 (Admin's auto-off retry): when a run-mode
# row is about to stop the run because the task option is gone,
# the engine re-verifies before the stop takes effect. Each
# attempt sends ❌ Cancel to reset the target menu, then re-runs
# the pre-paste probe; a passed probe resumes the run. Attempts
# are STOP_RETRY_ATTEMPTS at STOP_RETRY_WAIT_SECONDS apart — the
# wait is a plain sleep, because the interval is Admin's
# "1-minute interval between each check", not a wakeup. A row is
# re-verified at most STOP_RECOVERIES_PER_ROW times; the next
# stop after that is final, so a menu that keeps vanishing
# cannot spin one row forever.
STOP_RETRY_ATTEMPTS = 5
STOP_RETRY_WAIT_SECONDS = 60
STOP_RECOVERIES_PER_ROW = 3


def _is_network_error(exc):
    """True for transport failures only: OSError except a reply hang.

    ReplyTimeout is a TimeoutError (hence OSError) but means the
    target never answered — a hang, not a broken connection — so
    it never retries. RPC errors (bad requests, rate limits) are
    not OSError and never retry either.
    """
    return isinstance(exc, OSError) and not isinstance(exc, ReplyTimeout)


_DEAD_ACCOUNT_ERRORS = {
    "AuthKeyUnregistered",
    "AuthKeyDuplicated",
    "AuthKeyInvalid",
    "SessionRevoked",
    "SessionExpired",
    "UserDeactivated",
    "UserDeactivatedBan",
}


# ------------------------------------------------------------ config/billing/store access

def _cfg(name, default):
    if _config is not None:
        return getattr(_config, name, default)
    return default


def _cost():
    return _cfg("COST_PER_ROW_POISHA", 50)


def _low_thresholds():
    return _cfg("LOW_BALANCE_WARN_POISHA", (2500, 500))


def _sessions_dir():
    return _cfg("SESSIONS_DIR", "data/sessions")


def _api_id():
    return _cfg("API_ID", None)


def _api_hash():
    return _cfg("API_HASH", None)


def _billable(owner):
    if _billing is not None and hasattr(_billing, "billable"):
        try:
            return _billing.billable(owner)
        except Exception:  # noqa: BLE001 - fall back to the plain rule
            pass
    return owner != "admin"


def _format_bdt(poisha):
    if _billing is not None and hasattr(_billing, "format_bdt"):
        try:
            return _billing.format_bdt(poisha)
        except Exception:  # noqa: BLE001 - fall back to plain formatting
            pass
    return f"৳{poisha / 100:.2f}"


def _ids_left(balance):
    if _billing is not None and hasattr(_billing, "ids_left"):
        try:
            return _billing.ids_left(balance)
        except Exception:  # noqa: BLE001 - fall back to plain division
            pass
    return balance // _cost()


def _topup_text(owner):
    if _billing is not None and hasattr(_billing, "topup_instructions"):
        try:
            uid = owner.split(":", 1)[1] if owner.startswith("user:") \
                else owner
            arg = int(uid) if uid.isdigit() else uid
            return _billing.topup_instructions(arg)
        except Exception:  # noqa: BLE001 - fall back to plain text
            pass
    return "Top up to continue using the bot."


def _s(name):
    if _store is None or not hasattr(_store, name):
        raise RuntimeError(f"v3.store.{name} is not available")
    return getattr(_store, name)


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


async def _call(obj, name, *args, **kwargs):
    attr = getattr(obj, name, None)
    if attr is None:
        return None
    return await _maybe_await(attr(*args, **kwargs))


def _get_name(me):
    if me is None:
        return "Account"
    return getattr(me, "first_name", None) \
        or getattr(me, "username", None) or "Account"


# ------------------------------------------------------------ matching

def _norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _match(button_text, label):
    """Strict exact after normalisation, nothing looser."""
    nb, nl = _norm(button_text), _norm(label)
    if nb and nl:
        return nb == nl
    return (button_text or "").strip() == (label or "").strip()


def _btn_text(btn):
    if isinstance(btn, str):
        return btn
    if isinstance(btn, dict):
        return btn.get("text") or ""
    return getattr(btn, "text", "") or ""


def _btn_callback(btn):
    if isinstance(btn, dict):
        return btn.get("callback_data") or None
    return getattr(btn, "callback_data", None) or None


def _menu_buttons(msg):
    if not msg:
        return []
    if isinstance(msg, dict):
        return msg.get("buttons") or []
    return []


def _menu_labels(msg):
    return [_btn_text(b) for b in _menu_buttons(msg)]


def _find_buttons(msg, label):
    return [b for b in _menu_buttons(msg) if _match(_btn_text(b), label)]


def _ambiguous(label, buttons):
    seen = [_btn_text(b) for b in buttons]
    return (f"ambiguous button for {label!r}: "
            f"more than one match ({seen!r})")


# ------------------------------------------------------------ validation

def validate_flow(flow):
    """Reject empty flows and any flow containing a /start step.

    The no-paste refusal lives in Engine.start, not here, because
    check mode legitimately runs flows whose pastes never fire.
    """
    steps = flow.get("steps", []) if isinstance(flow, dict) else []
    if not steps:
        raise ValueError("flow has no steps")
    for step in steps:
        if step.get("type") == "send" \
                and (step.get("text") or "").strip() == "/start":
            raise ValueError(
                "v3 flows do not include /start: start with 📋 Tasks")
    return True


def _pre_paste_steps(flow):
    steps = flow.get("steps", [])
    out = []
    for step in steps:
        if step.get("type") == "paste":
            break
        out.append(step)
    return out if out else list(steps)


# ------------------------------------------------------------ flow session

def _normalise_entry(entry):
    if isinstance(entry, dict):
        return {
            "id": entry.get("id"),
            "text": entry.get("text") or "",
            "buttons": entry.get("buttons") or [],
            "incoming": entry.get("incoming", True),
            "edit_date": entry.get("edit_date"),
            "reply_to": entry.get("reply_to"),
        }
    if isinstance(entry, (list, tuple)) and entry:
        return {
            "id": None,
            "text": entry[0] or "",
            "buttons": entry[1] if len(entry) > 1 else [],
            "incoming": True,
            "edit_date": None,
            "reply_to": None,
        }
    return {"id": None, "text": "", "buttons": [],
            "incoming": True, "edit_date": None, "reply_to": None}


class FlowSession:
    """One attempt's view of the target bot conversation.

    The session baselines its reply cursor on prepare() so a
    previous row's leftover replies can never let steps fire
    ahead. History is the source of truth; a stream wakeup is
    only a wakeup.
    """

    def __init__(self, target, waiter=None):
        self.target = target
        self.waiter = waiter
        self.last_seen = None
        self.last_edit = None
        self._last_text = None
        self.pastes_done = 0
        self._cache = None
        self._cache_fresh = False
        self.guard_clicks = False
        self.wait_for_reply = False
        # Run-mode read-first rule is off in check mode: a missing
        # pre-paste option there means "watching", never a stop.
        self.check_mode = False

    async def _history(self, limit):
        raw = await self.target.history(limit=limit)
        return [_normalise_entry(e) for e in (raw or [])]

    async def _fetch_incoming(self, limit=10):
        return [m for m in await self._history(limit) if m["incoming"]]

    async def prepare(self):
        incoming = await self._fetch_incoming(5)
        if incoming:
            first = incoming[0]
            self.last_seen = first["id"]
            self.last_edit = first["edit_date"]
            self._last_text = first["text"]
            self._cache = first
        else:
            self._cache = None
        self._cache_fresh = True

    def _pick_new(self, incoming):
        """First genuinely new incoming message, newest-first.

        A new message that replies to our last sent message is
        preferred when present: it is definitively the answer to
        our command, even if a notice arrived after it. Without
        reply threading, or without a match, the newest new
        message wins, exactly as before.
        """
        sent_id = getattr(self.target, "last_sent_id", None)
        fallback = None
        for msg in incoming:
            mid = msg["id"]
            if self.last_seen is None:
                return msg
            if mid is not None:
                if mid > self.last_seen:
                    if fallback is None:
                        fallback = msg
                    if sent_id is not None \
                            and msg.get("reply_to") == sent_id:
                        return msg
                    continue
                if fallback is None and mid == self.last_seen \
                        and msg["edit_date"] is not None \
                        and (self.last_edit is None
                             or msg["edit_date"] > self.last_edit):
                    return msg
                break
            if fallback is not None:
                break
            if msg["text"] != self._last_text:
                return msg
            break
        return fallback

    async def wait_reply(self):
        """Wait for a new reply or a newer edit.

        Run mode only: bounded by WAIT_REPLY_TIMEOUT_SECONDS, so
        a hung row fails instead of freezing the slot; the
        runner then fails the row and proceeds to the next one,
        and other slots are unaffected. Check mode keeps no bound
        here: the probe cap bounds it instead.
        """
        if self.waiter is not None and hasattr(self.waiter, "drain"):
            # v2's discipline: a wakeup left set by our own send
            # must not make the first wait return at once.
            self.waiter.drain()
        deadline = None
        if not self.check_mode:
            deadline = time.monotonic() + WAIT_REPLY_TIMEOUT_SECONDS
        while True:
            incoming = await self._fetch_incoming(5)
            hit = self._pick_new(incoming)
            if hit is not None:
                self.last_seen = hit["id"]
                self.last_edit = hit["edit_date"]
                self._last_text = hit["text"]
                self._cache = hit
                self._cache_fresh = True
                return hit
            if deadline is not None \
                    and time.monotonic() >= deadline:
                raise ReplyTimeout("reply wait expired")
            if self.waiter is not None \
                    and hasattr(self.waiter, "wait_event"):
                await self.waiter.wait_event()
            else:
                await asyncio.sleep(1)

    async def _latest_incoming(self):
        if self._cache_fresh:
            return self._cache
        incoming = await self._fetch_incoming(5)
        msg = incoming[0] if incoming else None
        if msg is not None:
            self.last_seen = msg["id"]
            self.last_edit = msg["edit_date"]
            self._last_text = msg["text"]
            self._cache = msg
        self._cache_fresh = True
        return msg

    async def _refresh_message(self, msg):
        if msg is None or msg.get("id") is None:
            return None
        for m in await self._fetch_incoming(10):
            if m["id"] == msg["id"]:
                return m
        return None

    async def _button_present(self, label):
        """Fresh scan for one unique strict-exact match.

        Returns (message, button) or (None, None). Raises
        OptionRemoved on an ambiguous match, as in v2. Baselines
        the reply cursor to the newest fetched message first, as
        v2's do_click does before pressing, so the click's reply
        wait can never mistake pre-click state for a reply.

        Two sources, in order. First the recent window (10, as
        v2): it covers inline prompts and any menu the panel
        state missed. Then the live panel state recorded by the
        stream handler: a reply keyboard persists as chat state,
        so the panel can sit outside the window while remaining
        visibly present; checking the recorded panel finds it
        without scanning back. Older superseded panels are never
        stored — the handler overwrites on every new panel
        message — so a stale panel button cannot press.
        """
        window = await self._fetch_incoming(10)
        if window:
            newest = window[0]
            self.last_seen = newest["id"]
            self.last_edit = newest["edit_date"]
            self._last_text = newest["text"]
            self._cache = newest
            self._cache_fresh = True
        for m in window:
            found = _find_buttons(m, label)
            if len(found) > 1:
                raise OptionRemoved(_ambiguous(label, found))
            if len(found) == 1:
                return m, found[0]
        panel = getattr(self.target, "panel_message", None)
        if panel is not None and all(
                panel.get("id") != m["id"] for m in window):
            found = _find_buttons(panel, label)
            if len(found) > 1:
                raise OptionRemoved(_ambiguous(label, found))
            if len(found) == 1:
                return panel, found[0]
        return None, None

    async def _settle(self):
        """One poll interval, cut short by a fresh stream wakeup.

        A menu that changed a beat ago needs a beat for the
        change to land before a found button is trusted. With a
        waiter, the wait ends early on a fresh wakeup and never
        exceeds the interval, whatever the waiter does; without
        one, it sleeps the interval.
        """
        if self.waiter is not None and hasattr(self.waiter, "wait_event"):
            if hasattr(self.waiter, "drain"):
                self.waiter.drain()
            try:
                await asyncio.wait_for(self.waiter.wait_event(),
                                       timeout=SETTLE_WAIT_SECONDS)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(SETTLE_WAIT_SECONDS)

    async def _wait_for_button(self, label, timeout=None):
        """Rescan on each new incoming message or edit until the
        button appears or the cap expires.

        A notice or any non-menu event does not end the wait: the
        loop keeps scanning until a scan finds the button or the
        deadline passes. Returns (message, button) or (None, None).
        An ambiguous match still raises OptionRemoved, as before.

        A hit is settled before it is trusted: one poll interval
        plus a fresh re-scan. A button that vanished in the
        interval is stale — the loop keeps waiting instead of
        clicking it. On deadline expiry one final fresh scan
        runs before giving up, so a menu that changed a beat ago
        does not stop the run on a stale last scan.
        """
        if timeout is None:
            timeout = GUARD_WAIT_SECONDS
        deadline = time.monotonic() + timeout
        while True:
            msg, btn = await self._button_present(label)
            if btn is not None:
                await self._settle()
                msg, btn = await self._button_present(label)
                if btn is not None:
                    return msg, btn
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg, btn = await self._button_present(label)
                if btn is not None:
                    return msg, btn
                return None, None
            if self.waiter is not None and hasattr(self.waiter, "wait_event"):
                if hasattr(self.waiter, "drain"):
                    self.waiter.drain()
                await self.waiter.wait_event()
            else:
                await asyncio.sleep(min(1, remaining))

    async def ensure_button(self, label):
        """Run-mode read-first rule for one button label.

        Fresh read; on a miss rescan on the target's next activity
        until the button appears or GUARD_WAIT_SECONDS expires;
        on expiry raise StopRun, which halts the whole run.
        A hit returns (message, button).
        """
        msg, btn = await self._wait_for_button(label)
        if btn is not None:
            return msg, btn
        panel = getattr(self.target, "panel_message", None)
        if panel is not None and _menu_buttons(panel):
            seen = _menu_labels(panel)
        else:
            window = await self._fetch_incoming(10)
            seen = None
            for m in window:
                if _menu_buttons(m):
                    seen = _menu_labels(m)
                    break
        raise StopRun(
            f"option not on the menu: {label!r} (menu had: {seen!r})")

    async def do_send(self, text):
        await self.target.send(text)
        if self.wait_for_reply:
            await self.wait_reply()

    async def _perform_click(self, msg, btn):
        text = _btn_text(btn)
        data = _btn_callback(btn)
        if data and hasattr(self.target, "callback"):
            try:
                await self.target.callback(msg["id"], data, text)
            except TypeError:
                await self.target.callback(msg["id"], data)
        else:
            # Sent directly, not via do_send, so the response wait
            # below is the only wait for this step.
            await self.target.send(text)
        if self.wait_for_reply:
            await self.wait_reply()

    async def do_click(self, label):
        msg = await self._latest_incoming()
        if msg is None:
            await self.do_send(label)
            return
        matches = _find_buttons(msg, label)
        if len(matches) > 1:
            raise OptionRemoved(_ambiguous(label, matches))
        if len(matches) == 1:
            await self._perform_click(msg, matches[0])
            return
        # No match on the snapshot: re-fetch that message fresh and
        # decide identically before believing the miss.
        fresh = await self._refresh_message(msg)
        if fresh is not None:
            matches = _find_buttons(fresh, label)
            if len(matches) > 1:
                raise OptionRemoved(_ambiguous(label, matches))
            if len(matches) == 1:
                self._cache = fresh
                self._cache_fresh = True
                await self._perform_click(fresh, matches[0])
                return
            msg = fresh
        if self.guard_clicks and _menu_buttons(msg):
            # Guarded pre-paste click: invalidate the cache and scan
            # a fresh window immediately. A hit rescues the run;
            # only a fresh miss stops it, with fresh labels.
            self._cache = None
            self._cache_fresh = False
            window = await self._fetch_incoming(10)
            for m in window:
                found = _find_buttons(m, label)
                if len(found) > 1:
                    raise OptionRemoved(_ambiguous(label, found))
                if len(found) == 1:
                    self._cache = m
                    self._cache_fresh = True
                    self.last_seen = m["id"]
                    self.last_edit = m["edit_date"]
                    self._last_text = m["text"]
                    await self._perform_click(m, found[0])
                    return
            seen = None
            for m in window:
                if _menu_buttons(m):
                    seen = _menu_labels(m)
                    break
            if seen is None:
                seen = _menu_labels(msg)
            raise OptionRemoved(
                f"option not on the menu: {label!r} (menu had: {seen!r})")
        await self.do_send(label)

    async def do_paste(self, column, row):
        key = {1: "col_a", 2: "col_b", 3: "col_c", 4: "col_d"}[column]
        value = (row or {}).get(key) or ""
        if not value:
            raise RowFailed(f"empty column {column}")
        await self.do_send(value)
        self.pastes_done += 1

    async def execute(self, step, row):
        stype = step.get("type")
        if stype == "send":
            text = step.get("text", "")
            if not self.check_mode and text.strip():
                # Run mode: read first. A send step's text is a
                # button label in these flows, so a miss after the
                # reread stops the whole run via StopRun.
                await self.ensure_button(text)
            await self.do_send(text)
        elif stype == "click":
            if self.check_mode:
                # Check probes keep v2's guarded path: a miss is
                # OptionRemoved (watching), never a stop.
                await self.do_click(step.get("label", ""))
            else:
                msg, btn = await self.ensure_button(
                    step.get("label", ""))
                await self._perform_click(msg, btn)
        elif stype == "wait":
            await asyncio.sleep(step.get("seconds", 0))
        elif stype == "paste":
            if not self.check_mode:
                # Fresh read first, as for every sending step. A
                # paste has no button to gate on, so this read
                # never stops the run; it only freshens the session.
                self._cache = None
                self._cache_fresh = False
                await self._latest_incoming()
            await self.do_paste(step.get("column", 1), row)
        else:
            raise RowFailed(f"unknown step type: {stype!r}")


# ------------------------------------------------------------ engine

def _default_client_factory(owner, slot, phone=None):
    from pyrogram import Client

    workdir = os.path.join(_sessions_dir(), owner)
    os.makedirs(workdir, exist_ok=True)
    return Client(f"slot_{slot}", api_id=_api_id(), api_hash=_api_hash(),
                  workdir=workdir, phone_number=phone)


def _default_target_factory(client, target_username, waiter):
    return _PyrogramTarget(client, target_username, waiter=waiter)


class Engine:
    def __init__(self, client_factory=None, target_factory=None):
        self._client_factory = client_factory or _default_client_factory
        self._target_factory = target_factory or _default_target_factory
        self._clients = {}
        self._targets = {}
        self._stream_waiters = {}
        self._pending_links = {}
        self._tasks = {}
        self._runs = {}
        self._check = {}
        self._check_turn = {}
        self._last_stop = {}
        self._stopping = set()
        self._warned = {}
        self._notifier = None
        self._revert_handler = None
        self._shutting_down = False
        self._firing = set()

    # -- wiring ------------------------------------------------

    def set_notifier(self, notifier):
        self._notifier = notifier

    def set_revert_handler(self, handler):
        self._revert_handler = handler

    def has_active_tasks(self, owner):
        tasks = self._tasks.get(owner, {})
        return any(not t.done() for t in tasks.values())

    # -- clients ------------------------------------------------

    def _session_dir(self, owner):
        path = os.path.join(_sessions_dir(), owner)
        os.makedirs(path, exist_ok=True)
        return path

    def _make_client(self, owner, slot, phone=None):
        try:
            return self._client_factory(owner, slot, phone)
        except TypeError:
            return self._client_factory(owner, slot)

    async def _drop_client(self, owner, slot):
        key = (owner, slot)
        # v2 parity: dropping a slot also drops its pending link, so
        # a half-finished login can never linger connected after the
        # slot's running client is gone (dead handler, relink, unlink).
        pend = self._pending_links.pop(key, None)
        if pend is not None:
            try:
                pclient = pend[0]
                if hasattr(pclient, "disconnect"):
                    await _call(pclient, "disconnect")
                elif hasattr(pclient, "stop"):
                    await _call(pclient, "stop")
            except Exception:  # noqa: BLE001 - drop is best-effort
                pass
        client = self._clients.pop(key, None)
        self._targets.pop(key, None)
        self._stream_waiters.pop(key, None)
        if client is not None:
            try:
                if hasattr(client, "disconnect"):
                    await _call(client, "disconnect")
                elif hasattr(client, "stop"):
                    await _call(client, "stop")
            except Exception:  # noqa: BLE001 - drop is best-effort
                pass

    async def begin_link(self, owner, slot, phone):
        """Start linking a worker account. Returns its name when the
        session file was already authorised (no code needed), else
        None once the code has been requested."""
        if _api_id() is None or not _api_hash():
            raise RuntimeError(
                "worker linking needs V3_API_ID and V3_API_HASH "
                "in the bot's .env first")
        await self._drop_client(owner, slot)
        client = self._make_client(owner, slot, phone)
        await _call(client, "connect")
        try:
            me = await _call(client, "get_me")
        except Exception:  # noqa: BLE001 - unauthorised session
            me = None
        if me is not None:
            name = _get_name(me)
            _s("upsert_account")(owner, slot, name=name, phone=phone,
                                 status="active")
            self._clients[(owner, slot)] = client
            return name
        sent = await _call(client, "send_code", phone)
        code_hash = getattr(sent, "phone_code_hash", None)
        self._pending_links[(owner, slot)] = (client, code_hash, phone)
        _s("upsert_account")(owner, slot, name="", phone=phone,
                             status="pending_code")
        return None

    async def complete_link(self, owner, slot, code, password=None):
        link = self._pending_links.get((owner, slot))
        if link is None:
            raise RuntimeError("no pending login for this slot")
        client, code_hash, phone = link
        try:
            await _call(client, "sign_in", phone, code_hash, code)
        except Exception as exc:  # noqa: BLE001 - re-raised below
            if type(exc).__name__ == "SessionPasswordNeeded":
                if password is None:
                    raise NeedPassword(
                        "this account needs its 2-step verification "
                        "password")
                await _call(client, "check_password", password)
            else:
                raise
        me = await _call(client, "get_me")
        name = _get_name(me) if me is not None else f"Account {slot}"
        _s("upsert_account")(owner, slot, name=name, phone=phone,
                             status="active")
        self._pending_links.pop((owner, slot), None)
        self._clients[(owner, slot)] = client
        return name

    async def unlink(self, owner, slot):
        await self._drop_client(owner, slot)
        self._pending_links.pop((owner, slot), None)
        directory = self._session_dir(owner)
        for suffix in (".session", ".session-journal"):
            path = os.path.join(directory, f"slot_{slot}{suffix}")
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass
        _s("remove_account")(owner, slot)

    # -- streams -------------------------------------------------

    def _register_stream(self, owner, slot, client, target_username):
        """Create the target adapter and attach its stream handlers.

        Handler attachment is best-effort: a failure only logs a
        warning, and the target stays usable with history polling.

        The adapter is cached per (owner, slot) — but the cache
        key does NOT include the target, so a cached adapter is
        reused only when its target matches the requested one
        (compared without the '@' prefix, case-insensitively).
        On a target change the old adapter's handlers are
        detached and the cache is dropped, so the fresh adapter
        talks to the new target on the same client session; no
        relink is needed. Targets without a recorded username
        (e.g., test fakes) reuse the cache as before.
        """
        key = (owner, slot)
        if key in self._targets:
            existing = self._targets[key]
            existing_target = getattr(existing, "target", None)
            if existing_target is None or not target_username or \
                    str(existing_target).lstrip("@").lower() == \
                    str(target_username).lstrip("@").lower():
                return existing, self._stream_waiters[key]
            detach = getattr(existing, "detach_stream", None)
            if detach is not None:
                try:
                    detach()
                except Exception as exc:  # noqa: BLE001 - best-effort
                    log.warning("stream detach failed for %s slot %s: %s",
                                owner, slot, exc)
            self._targets.pop(key, None)
            self._stream_waiters.pop(key, None)
        try:
            target = self._target_factory(client, target_username, None)
        except TypeError:
            target = self._target_factory(client, target_username)
        waiter = StreamWaiter(target)
        try:
            target.waiter = waiter
        except Exception:  # noqa: BLE001 - some targets are immutable
            pass
        if hasattr(target, "attach_stream"):
            try:
                target.attach_stream()
            except Exception as exc:  # noqa: BLE001 - stream is optional
                log.warning("stream attach failed for %s slot %s: %s",
                            owner, slot, exc)
        self._targets[key] = target
        self._stream_waiters[key] = waiter
        return target, waiter

    def _target_for(self, owner, slot, flow):
        key = (owner, slot)
        client = self._clients.get(key)
        if client is None:
            raise RuntimeError(f"no linked account for slot {slot}")
        return self._register_stream(owner, slot, client,
                                     flow.get("target", ""))

    async def _ensure_one(self, owner, slot, target_username):
        """True when the slot is live after this call, else False."""
        acc = _s("get_account")(owner, slot)
        if not acc or acc.get("status") != "active":
            return False
        key = (owner, slot)
        if key in self._clients:
            if target_username:
                self._register_stream(owner, slot, self._clients[key],
                                      target_username)
            return True
        client = self._make_client(owner, slot)
        try:
            if hasattr(client, "start"):
                await _call(client, "start")
            elif hasattr(client, "connect"):
                await _call(client, "connect")
            if hasattr(client, "get_me"):
                me = await _call(client, "get_me")
                if me is None:
                    raise RuntimeError("get_me returned nothing")
        except Exception as exc:  # noqa: BLE001 - logged, slot skipped
            log.warning("slot %s for %s unusable: %s", slot, owner, exc)
            try:
                if hasattr(client, "disconnect"):
                    await _call(client, "disconnect")
                elif hasattr(client, "stop"):
                    await _call(client, "stop")
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            return False
        self._clients[key] = client
        if target_username:
            self._register_stream(owner, slot, client, target_username)
        return True

    # -- pushes ---------------------------------------------------

    async def _push(self, owner, text):
        if self._notifier is not None:
            try:
                await self._notifier(owner, text)
            except Exception:  # noqa: BLE001 - pushes are best-effort
                log.exception("push failed for %s", owner)

    async def _push_stop(self, owner, reason):
        """Record a stop and push it, once per distinct reason."""
        prev = self._last_stop.get(owner)
        if prev is not None and prev[0] == reason:
            return
        self._last_stop[owner] = (reason, time.time())
        await self._push(owner, f"⏹ {reason}")

    async def _safe_revert(self, owner):
        if self._shutting_down or self._revert_handler is None:
            return
        try:
            await self._revert_handler(owner)
        except Exception:  # noqa: BLE001 - revert is best-effort
            log.exception("revert handler failed for %s", owner)

    # -- money -----------------------------------------------------

    async def _reserve_row(self, owner):
        """Reserve one ID's cost. Raises OutOfCredits when uncovered."""
        before = _s("get_balance")(owner)
        new_bal = _s("reserve")(owner, _cost(), ref="row")
        if new_bal is None:
            raise OutOfCredits(
                f"balance {_format_bdt(before)} — {_topup_text(owner)}")
        warned = self._warned.setdefault(owner, set())
        for threshold in _low_thresholds():
            if before >= threshold and new_bal < threshold \
                    and threshold not in warned:
                warned.add(threshold)
                await self._push(
                    owner,
                    f"💰 Low balance: {_format_bdt(new_bal)} "
                    f"(~{_ids_left(new_bal)} IDs left).")
            elif new_bal >= threshold:
                warned.discard(threshold)
        return new_bal

    def _refund(self, owner):
        try:
            _s("refund")(owner, _cost(), ref="row")
        except Exception:  # noqa: BLE001 - refund is best-effort
            log.exception("refund failed for %s", owner)

    # -- runs -------------------------------------------------------

    async def start(self, owner, slots, batch_id, flow, mode="now",
                    scheduled_at=None):
        """Start a run. Returns (skipped_slots, moved_row_count)."""
        if mode in ("now", "timer"):
            cur = self._runs.get(owner)
            if cur is not None and cur.get("mode") == "check":
                await self.stop(owner)
        if owner in self._runs:
            raise RuntimeError("a run is already active")
        if mode not in ("now", "check", "timer"):
            raise ValueError(f"unknown mode: {mode!r}")
        if mode == "timer" and not scheduled_at:
            raise ValueError("timer mode needs scheduled_at")
        validate_flow(flow)
        steps = flow.get("steps", [])
        if mode in ("now", "timer") \
                and not any(s.get("type") == "paste" for s in steps):
            raise ValueError(
                "flow has no paste step: no ID would ever be submitted")
        batch = _s("get_batch")(batch_id)
        if batch is None or batch.get("owner") != owner:
            raise ValueError("batch not found for this owner")
        if _billable(owner):
            bal = _s("get_balance")(owner)
            if bal < _cost():
                raise RuntimeError(
                    f"Out of credits: balance {_format_bdt(bal)} — "
                    f"{_topup_text(owner)}")
        working, skipped = [], []
        for slot in slots:
            if await self._ensure_one(owner, slot, flow.get("target", "")):
                working.append(slot)
            else:
                skipped.append(slot)
        if not working:
            raise RuntimeError(
                f"no linked account for slots: {sorted(skipped)}")
        moved = 0
        if skipped:
            try:
                moved = _s("redistribute_pending")(batch_id, skipped,
                                                   working) or 0
            except Exception:  # noqa: BLE001 - logged, run continues
                log.exception("redistribute failed for %s", owner)
        self._runs[owner] = {
            "mode": mode,
            "batch_id": batch_id,
            "flow": flow,
            "flow_name": flow.get("name", ""),
            "slots": list(working),
            "scheduled_at": scheduled_at,
        }
        if mode == "timer":
            # The schedule's only durable record: without this row
            # a restart silently deletes the timer (seen live
            # 2026-11-09: Date & Time "didn't save"). A lost row
            # must never block the run — the in-memory timer still
            # fires for this process lifetime — so the save is
            # best-effort and a missing flow id only warns.
            flow_id = flow.get("id")
            if flow_id is None:
                log.warning("timer for %s has no flow id; not persisted",
                            owner)
            else:
                try:
                    _s("save_scheduled_run")(owner, batch_id, flow_id,
                                              list(working), scheduled_at)
                except Exception:  # noqa: BLE001 - best-effort
                    log.exception("could not persist timer for %s", owner)
        self._last_stop.pop(owner, None)
        self._stopping.discard(owner)
        if mode == "check":
            self._check[owner] = {"state": "probing", "at": time.time()}
            self._check_turn[owner] = {"slots": sorted(working), "turn": 0}
        self._tasks[owner] = {}
        for slot in working:
            task = asyncio.create_task(
                self._slot_runner(owner, slot, batch_id, flow, mode,
                                  scheduled_at),
                name=f"tectonic-{owner[:8]}-slot-{slot}")
            task.add_done_callback(
                lambda t, o=owner, s=slot: self._on_task_done(o, s, t))
            self._tasks[owner][slot] = task
        return skipped, moved

    def _on_task_done(self, owner, slot, task):
        try:
            task.exception()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - already handled in the runner
            pass
        tasks = self._tasks.get(owner, {})
        tasks.pop(slot, None)
        if tasks:
            return
        run = self._runs.pop(owner, None)
        self._tasks.pop(owner, None)
        self._check_turn.pop(owner, None)
        if run is not None and run.get("mode") == "check":
            self._check.pop(owner, None)
        if run is not None and run.get("mode") == "timer" \
                and not self._shutting_down:
            # The schedule was consumed (fired and finished, or the
            # run ended): clear the persisted row so a later boot
            # cannot resurrect it. During shutdown the row is kept
            # on purpose — boot restore re-starts it.
            try:
                _s("delete_scheduled_run")(owner)
            except Exception:  # noqa: BLE001 - best-effort
                log.exception("could not clear timer for %s", owner)
        if not self._shutting_down and self._revert_handler is not None:
            try:
                asyncio.create_task(self._safe_revert(owner))
            except RuntimeError:  # noqa: BLE001 - no running loop
                pass

    def _is_tasks_first_step(self, step):
        """True when a first step is a 📋 Tasks click or send."""
        if step.get("type") == "click":
            return _match(step.get("label", ""), "📋 Tasks")
        if step.get("type") == "send":
            return _match(step.get("text", ""), "📋 Tasks")
        return False

    async def _tasks_first_check(self, session, check_mode=False):
        """First-step rule: fresh read for 📋 Tasks, else one recovery send.

        If the button is missing, send 📋 Tasks once — never
        /start, anywhere — and wait for a reply that actually
        carries the menu, not just the first new event. The send
        is an engine recovery, not a flow step: never saved, never
        validated as one. Run mode: still missing after the capped
        wait raises StopRun, halting the whole run. Check mode
        keeps the old single-reply path: still missing returns
        False, so the caller records watching and retries on the
        check cadence.
        """
        msg, btn = await session._button_present("📋 Tasks")
        if btn is not None:
            return True
        await session.target.send("📋 Tasks")
        if check_mode:
            await session.wait_reply()
            msg, btn = await session._button_present("📋 Tasks")
            if btn is not None:
                return True
            return False
        msg, btn = await session._wait_for_button("📋 Tasks")
        if btn is not None:
            return True
        panel = getattr(session.target, "panel_message", None)
        if panel is not None and _menu_buttons(panel):
            seen = _menu_labels(panel)
        else:
            window = await session._fetch_incoming(10)
            seen = None
            for m in window:
                if _menu_buttons(m):
                    seen = _menu_labels(m)
                    break
        raise StopRun(
            f"option not on the menu: '📋 Tasks' (menu had: {seen!r})")

    async def _retry_stop(self, owner, slot, target, waiter, flow,
                          reason):
        """Re-verify a run-mode stop before it takes effect.

        User-ordered 2026-11-09 (Admin's auto-off retry): when the
        run is about to stop because the task option is gone,
        reset the target menu with ❌ Cancel and re-run the
        pre-paste probe, up to STOP_RETRY_ATTEMPTS times,
        STOP_RETRY_WAIT_SECONDS apart. A passed probe means the
        task is back and the run resumes; still gone after the
        last attempt means the stop stands. Returns True when the
        task is back, False otherwise. Any probe failure counts
        as "still gone" — the probe only ever reads and clicks
        pre-paste steps, so it can never submit a row.

        Every attempt is logged (stop-retry ...): v3 keeps no
        run-stop journal, so the process log is the record — a
        later read of it shows exactly what was re-checked, when,
        and whether it recovered.
        """
        for attempt in range(1, STOP_RETRY_ATTEMPTS + 1):
            log.warning("stop-retry %s slot %s attempt %s/%s: %s",
                        owner, slot, attempt, STOP_RETRY_ATTEMPTS,
                        reason)
            session = FlowSession(target, waiter)
            session.check_mode = True
            try:
                await session.target.send("❌ Cancel")
                await asyncio.wait_for(
                    self._probe_once(session, flow),
                    timeout=CHECK_PROBE_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any probe
                # failure means the task is still gone; the next
                # attempt (if any) waits the ordered interval first.
                log.warning("stop-retry %s slot %s attempt %s/%s failed: "
                            "%s", owner, slot, attempt,
                            STOP_RETRY_ATTEMPTS, exc)
                if attempt < STOP_RETRY_ATTEMPTS:
                    await asyncio.sleep(STOP_RETRY_WAIT_SECONDS)
                continue
            log.warning("stop-retry %s slot %s recovered on attempt "
                        "%s/%s", owner, slot, attempt,
                        STOP_RETRY_ATTEMPTS)
            await self._push(
                owner,
                "⚠️ Task went missing — re-checked and it is back; "
                "resuming.")
            return True
        log.warning("stop-retry %s slot %s exhausted after %s attempts: "
                    "%s", owner, slot, STOP_RETRY_ATTEMPTS, reason)
        return False

    async def _maybe_recover_stop(self, owner, slot, target, waiter,
                                  flow, reason, stop_recoveries):
        """One stop-recovery round for a row. Returns (recovered,
        new_count): the count bounds recoveries per row
        (STOP_RECOVERIES_PER_ROW), so a menu that keeps vanishing
        cannot spin one row forever. A CancelledError propagates
        so the caller can refund and stop, as for any stop."""
        if stop_recoveries >= STOP_RECOVERIES_PER_ROW:
            return False, stop_recoveries
        recovered = await self._retry_stop(owner, slot, target, waiter,
                                           flow, reason)
        return recovered, stop_recoveries + (1 if recovered else 0)

    async def _stop_all(self, owner, current_slot, detail):
        """Menu-Stop semantics for the whole run, triggered by one slot.

        Only the first trigger pushes and records; a second slot
        missing at the same moment just ends its own slot. Other
        slots are cancelled, so their runners refund through the
        CancelledError path and their rows stay pending.
        """
        if owner in self._stopping:
            return
        self._stopping.add(owner)
        self._last_stop[owner] = (detail, time.time())
        await self._push(
            owner,
            f"⏹ Auto-stopped: {detail} Unsubmitted rows stay pending — "
            "press 🚀 Run batch again when the task is back on the menu.")
        tasks = self._tasks.get(owner, {})
        for slot, task in list(tasks.items()):
            if slot != current_slot and not task.done():
                task.cancel()

    async def _slot_runner(self, owner, slot, batch_id, flow, mode,
                           scheduled_at):
        if mode == "timer":
            delay = scheduled_at - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            mode = "now"
        if mode == "check":
            await self._run_check(owner, slot, flow)
            return
        target, waiter = self._target_for(owner, slot, flow)
        steps = flow.get("steps", [])
        first_paste = next(
            (i for i, s in enumerate(steps) if s.get("type") == "paste"),
            len(steps))
        paste_total = sum(1 for s in steps if s.get("type") == "paste")
        rows = _s("get_batch_rows")(batch_id, slot, "pending")
        for idx, row in enumerate(rows):
            reserved = False
            submitted = False
            stop_recoveries = 0
            attempt = 0
            while attempt <= ROW_NETWORK_RETRIES:
                try:
                    if _billable(owner) and not reserved:
                        await self._reserve_row(owner)
                        reserved = True
                    session = FlowSession(target, waiter)
                    await session.prepare()
                    if steps and self._is_tasks_first_step(steps[0]):
                        await self._tasks_first_check(session)
                    for si, step in enumerate(steps):
                        session.guard_clicks = si < first_paste
                        session.wait_for_reply = si < len(steps) - 1
                        await session.execute(step, row)
                        if si < len(steps) - 1:
                            await asyncio.sleep(STEP_PAUSE_SECONDS)
                        if paste_total > 0 \
                                and session.pastes_done >= paste_total \
                                and not submitted:
                            # Point of no return: the charge stands
                            # from here, whatever the remaining
                            # steps do.
                            _s("update_row")(row["id"], "done")
                            submitted = True
                    if not submitted:
                        # Unreachable for valid flows (a flow with
                        # pastes marks done at the point of no
                        # return); kept so a flow cannot finish
                        # unmarked.
                        _s("update_row")(row["id"], "done")
                    break
                except asyncio.CancelledError:
                    if reserved and not submitted:
                        self._refund(owner)
                    raise
                except StopRun as exc:
                    try:
                        recovered, stop_recoveries = \
                            await self._maybe_recover_stop(
                                owner, slot, target, waiter, flow,
                                str(exc), stop_recoveries)
                    except asyncio.CancelledError:
                        if reserved and not submitted:
                            self._refund(owner)
                        raise
                    if recovered:
                        if submitted:
                            break
                        continue
                    if reserved and not submitted:
                        self._refund(owner)
                    await self._stop_all(
                        owner, slot,
                        f"{exc} (re-checked {STOP_RETRY_ATTEMPTS} times,"
                        f" {STOP_RETRY_WAIT_SECONDS}s apart)")
                    return
                except OutOfCredits as exc:
                    await self._push_stop(
                        owner,
                        f"Out of credits: {exc} Rows stay pending — "
                        "top up and Start again.")
                    return
                except OptionRemoved as exc:
                    try:
                        recovered, stop_recoveries = \
                            await self._maybe_recover_stop(
                                owner, slot, target, waiter, flow,
                                str(exc), stop_recoveries)
                    except asyncio.CancelledError:
                        if reserved and not submitted:
                            self._refund(owner)
                        raise
                    if recovered:
                        if submitted:
                            break
                        continue
                    if reserved and not submitted:
                        self._refund(owner)
                    await self._push_stop(
                        owner,
                        f"Auto-stopped: {exc} (re-checked "
                        f"{STOP_RETRY_ATTEMPTS} times, "
                        f"{STOP_RETRY_WAIT_SECONDS}s apart). Start again "
                        "when the task is back on the menu.")
                    return
                except Exception as exc:  # noqa: BLE001 - policy below
                    # No rate-limit handling anywhere (the operator's
                    # order): a rate-limit error is a generic error,
                    # so an unsubmitted row fails like any other
                    # failed row. Transport failures retry after a
                    # backoff (ROW_NETWORK_RETRIES extra attempts,
                    # ROW_RETRY_BACKOFF_SECONDS times the attempt
                    # number); hangs and RPC errors fail the same
                    # way, with no retry.
                    if type(exc).__name__ in _DEAD_ACCOUNT_ERRORS:
                        log.warning("dead account %s slot %s: %s",
                                    owner, slot, exc)
                        if not submitted:
                            _s("update_row")(row["id"], "failed")
                            if reserved:
                                self._refund(owner)
                        await self._drop_client(owner, slot)
                        return
                    if submitted:
                        break
                    if _is_network_error(exc) \
                            and attempt < ROW_NETWORK_RETRIES:
                        delay = ROW_RETRY_BACKOFF_SECONDS * (attempt + 1)
                        log.warning("row %s: transport error on "
                                    "attempt %s, waiting %ss before "
                                    "retry", row["id"], attempt + 1,
                                    delay)
                        await asyncio.sleep(delay)
                        attempt += 1
                        continue
                    _s("update_row")(row["id"], "failed")
                    if reserved:
                        self._refund(owner)
                    break
            if idx < len(rows) - 1:
                await asyncio.sleep(flow.get("interval", 0))

    async def _wait_wakeup(self, waiter):
        """One wakeup wait, or one poll step when no waiter exists.

        Drains stale wakeups first (a probe's own sends set the
        event — waiting on those would spin without suspending
        and starve the loop's timers). A genuinely new stream
        wakeup returns immediately; a cadence expiry also
        returns, so a dead stream degrades to re-probing on the
        read cadence instead of waiting forever.
        """
        if waiter is not None and hasattr(waiter, "wait_event"):
            if hasattr(waiter, "drain"):
                waiter.drain()
            await waiter.wait_event()
        else:
            await asyncio.sleep(1)

    async def _probe_once(self, session, flow):
        """One check probe: menu recovery, then the pre-paste steps.

        When the flow starts with 📋 Tasks, the run-mode
        recovery runs first in check mode: a missing button is
        recovered with a 📋 Tasks send, and only a second miss
        raises OptionRemoved. The caller records watching and
        retries after CHECK_PROBE_WAIT_SECONDS.
        """
        await session.prepare()
        pre = _pre_paste_steps(flow)
        if pre and self._is_tasks_first_step(pre[0]):
            if not await self._tasks_first_check(session, check_mode=True):
                raise OptionRemoved("option not on the menu: '📋 Tasks'")
        for si, step in enumerate(pre):
            session.guard_clicks = True
            session.wait_for_reply = si < len(pre) - 1
            await session.execute(step, None)
            if si < len(pre) - 1:
                await asyncio.sleep(STEP_PAUSE_SECONDS)

    def _claim_probe_turn(self, owner, slot):
        """Turn gate for check probes: one account probes per tick.

        Fully synchronous (no awaits), so the check-and-advance is
        atomic under asyncio's cooperative scheduling. No turn
        state (e.g., unit tests) → probe. Ported from v2 minus the
        rate-limit penalty check: a turn is never withheld for a
        penalty, it only advances.
        """
        st = self._check_turn.get(owner)
        if st is None:
            return True
        slots = st["slots"]
        try:
            pos = slots.index(slot)
        except ValueError:
            return False
        if st["turn"] % len(slots) != pos:
            return False
        st["turn"] += 1
        return True

    async def _run_check(self, owner, slot, flow):
        target, waiter = self._target_for(owner, slot, flow)
        while True:
            if not self._claim_probe_turn(owner, slot):
                await asyncio.sleep(CHECK_PROBE_WAIT_SECONDS)
                continue
            session = FlowSession(target, waiter)
            session.check_mode = True
            try:
                await asyncio.wait_for(
                    self._probe_once(session, flow),
                    timeout=CHECK_PROBE_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except StopRun:
                # Unreachable: check probes use the legacy path and
                # never raise StopRun. Re-raise rather than watch.
                raise
            except OptionRemoved as exc:
                prev = self._check.get(owner, {})
                if prev.get("state") != "watching":
                    log.error("check watching for %s: %s", owner, exc)
                self._check[owner] = {
                    "state": "watching",
                    "detail": str(exc),
                    "at": time.time(),
                    "since": prev.get("since", time.time())
                    if prev.get("state") == "watching" else time.time(),
                }
                await asyncio.sleep(CHECK_PROBE_WAIT_SECONDS)
                continue
            except Exception as exc:  # noqa: BLE001 - loop never dies
                # No rate-limit handling anywhere (the operator's order):
                # a rate-limit error is a generic probe failure, so
                # the probe records failed and continues.
                log.exception("check failed for %s slot %s", owner, slot)
                self._check[owner] = {
                    "state": "failed",
                    "detail": str(exc)[:200],
                    "at": time.time(),
                }
                await asyncio.sleep(CHECK_PROBE_WAIT_SECONDS)
                continue
            self._check[owner] = {"state": "passed", "at": time.time()}
            run = self._runs.get(owner)
            if run is not None and run.get("mode") == "check" \
                    and run.get("batch_id") and run.get("flow") \
                    and owner not in self._firing:
                self._firing.add(owner)
                slots = run.get("slots") or [slot]
                asyncio.create_task(
                    self._fire_start(owner, run["batch_id"],
                                     run["flow"], slots))
            await self._wait_wakeup(waiter)

    async def _fire_start(self, owner, batch_id, flow, slots):
        try:
            await self.stop(owner)
            try:
                await self.start(owner=owner, slots=slots,
                                 batch_id=batch_id, flow=flow, mode="now")
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                log.error("auto-start failed for %s: %s", owner, exc)
                await self._push(
                    owner,
                    f"⚠️ Task appeared but auto-start failed: {exc} "
                    "Press 🚀 Run batch to run it manually.")
                return
            self._check[owner] = {"state": "fired", "at": time.time()}
            log.info("check fired for %s", owner)
            await self._push(
                owner,
                f"▶️ Task appeared — '{flow.get('name', '')}' "
                f"auto-started on {len(slots)} worker(s).")
        finally:
            self._firing.discard(owner)

    async def stop(self, owner, cancel_scheduled=True):
        """Stop the owner's run. A pending timer is cancelled with
        it, unless cancel_scheduled is False (shutdown keeps the
        persisted row so boot restore can re-start it)."""
        tasks = self._tasks.pop(owner, {})
        for task in list(tasks.values()):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(),
                                 return_exceptions=True)
        self._check.pop(owner, None)
        self._last_stop.pop(owner, None)
        self._stopping.discard(owner)
        self._runs.pop(owner, None)
        if cancel_scheduled:
            try:
                _s("delete_scheduled_run")(owner)
            except Exception:  # noqa: BLE001 - best-effort
                log.exception("could not clear timer for %s", owner)

    async def shutdown(self):
        self._shutting_down = True
        for owner in list(self._tasks.keys()):
            await self.stop(owner, cancel_scheduled=False)
        for key in list(self._clients.keys()):
            await self._drop_client(*key)
        for key in list(self._pending_links.keys()):
            client = self._pending_links.pop(key)[0]
            try:
                if hasattr(client, "disconnect"):
                    await _call(client, "disconnect")
                elif hasattr(client, "stop"):
                    await _call(client, "stop")
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                pass

    def status(self, owner):
        run = self._runs.get(owner)
        stop_reason = None
        last = self._last_stop.get(owner)
        if last is not None:
            stop_reason = {"reason": last[0], "at": last[1]}
        if run is None:
            return {
                "active": False,
                "mode": None,
                "batch_id": None,
                "flow_name": None,
                "scheduled_at": None,
                "check": self._check.get(owner),
                "stop_reason": stop_reason,
            }
        return {
            "active": True,
            "mode": run.get("mode"),
            "batch_id": run.get("batch_id"),
            "flow_name": run.get("flow_name"),
            "scheduled_at": run.get("scheduled_at"),
            "check": self._check.get(owner),
            "stop_reason": stop_reason,
        }
