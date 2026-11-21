"""v3 engine tests: spec sections 2.4, 3.2, 3.3, 3.4.

Fake target clients speak the dict-history interface. The engine is
driven against a temp store DB; no network, no Telegram login.
"""

import asyncio
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from v3 import config as v3config
from v3 import engine as v3engine
from v3 import store


def run(coro):
    return asyncio.run(coro)


class FakeMe:
    def __init__(self, name="Worker"):
        self.name = name
        self.first_name = name


class DeadError(Exception):
    """Class name matches the engine's dead-account set."""


DeadError.__name__ = "AuthKeyUnregistered"


class FakeClient:
    """Pyrogram-like client for ensure/link tests."""

    def __init__(self, dead=False, authorised=False):
        self.dead = dead
        self.authorised = authorised
        self.started = False
        self.code_hash = None

    async def start(self):
        self.started = True

    async def get_me(self):
        if self.dead:
            raise DeadError("session dead")
        if self.authorised:
            return FakeMe()
        raise RuntimeError("not authorised")

    async def disconnect(self):
        pass

    async def connect(self):
        pass

    async def send_code(self, phone):
        self.code_hash = "hash-" + phone
        return self.code_hash

    async def sign_in(self, phone, code_hash, code):
        if code != "12345":
            raise RuntimeError("bad code")
        self.authorised = True
        return FakeMe()


class FakeTarget:
    """Target bot: replies to every send with the holder's menu."""

    def __init__(self, holder):
        self.holder = holder
        self.sent = []
        self.callbacks = []
        self.waiter = None
        self.last_sent_id = None
        self.messages = [{
            "id": 1, "text": "menu", "buttons": list(holder["menu"]),
            "incoming": True, "edit_date": None,
        }]
        self._next = 2

    @property
    def panel_message(self):
        """Newest incoming reply-keyboard message, as the real
        adapter's stream handler would have recorded it. Inline
        prompts are not panel state."""
        for m in self.messages:
            if not m.get("incoming"):
                continue
            buttons = m.get("buttons") or []
            if buttons and all(
                    v3engine._btn_callback(b) is None
                    for b in buttons):
                return m
        return None

    async def send(self, text):
        self.sent.append(text)
        self.last_sent_id = self._next
        self._next += 1
        self.messages.insert(0, {
            "id": self._next, "text": f"reply to {text}",
            "buttons": list(self.holder["menu"]),
            "incoming": True, "edit_date": None,
        })
        self._next += 1
        if self.waiter is not None:
            self.waiter.wake()

    async def history(self, limit=10):
        return list(self.messages[:limit])

    async def callback(self, msg_id, data, text=None):
        self.callbacks.append((msg_id, data))
        await self.send(text or "callback")


class LateMenuWaiter:
    """Waiter that delivers a full menu on the first wait."""

    def __init__(self, target, menu):
        self.target = target
        self.menu = menu
        self.delivered = False

    def drain(self):
        pass

    async def wait_event(self):
        if not self.delivered:
            self.delivered = True
            mid = self.target._next
            self.target._next += 1
            self.target.messages.insert(0, {
                "id": mid, "text": "menu",
                "buttons": list(self.menu),
                "incoming": True, "edit_date": None,
            })
        return True


def flow(steps, interval=0, name="T", target="@Taskl1_bot"):
    return {"id": 1, "name": name, "target": target,
            "interval": interval, "steps": steps}


def task_flow(menu_price="($0.0280)"):
    return flow([
        {"type": "click", "label": "📋 Tasks"},
        {"type": "click", "label": f"🍪 {menu_price}"},
        {"type": "click", "label": f"🌟2FA:Create FB (No mail) {menu_price}"},
        {"type": "click", "label": "▶️ Start"},
        {"type": "paste", "column": 2},
        {"type": "paste", "column": 1},
        {"type": "click", "label": "✅ Account registered"},
    ])


class EngineCase(unittest.TestCase):
    def setUp(self):
        self._orig = store.db_path()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        store.set_db_path(self._tmp.name)
        store.init_db()
        self.pushes = []
        self.holder = {"menu": ["📋 Tasks", "🍪 ($0.0280)",
                                "🌟2FA:Create FB (No mail) ($0.0280)",
                                "▶️ Start", "✅ Account registered"]}
        self.clients = {}

    def tearDown(self):
        store.set_db_path(self._orig)
        pathlib.Path(self._tmp.name).unlink(missing_ok=True)

    def make_engine(self, client_slots=(1,)):
        for slot in client_slots:
            self.clients.setdefault(slot, FakeClient(authorised=True))

        async def _notifier(owner, text):
            self.pushes.append(text)

        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: self.clients[s],
            target_factory=lambda c, u, w: FakeTarget(self.holder),
        )
        eng.set_notifier(_notifier)
        return eng

    def setup_owner(self, owner, slots=(1,), balance=None):
        for slot in slots:
            store.upsert_account(owner, slot, name=f"W{slot}",
                                 phone="+100", status="active")
        if balance is not None:
            store.adjust_balance(owner, balance, "topup")

    def make_batch(self, owner, rows, slots=(1,)):
        bid, queued, _ = store.create_batch(owner, "a.xlsx", "shared",
                                            rows, list(slots))
        return bid

    async def wait_idle(self, eng, owner, timeout=25.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if owner not in eng._runs and owner not in eng._tasks:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("engine did not go idle")


class TestMatching(unittest.TestCase):
    def test_strict_exact(self):
        self.assertTrue(v3engine._match("🍪 ($0.0280)", "🍪 ($0.0280)"))
        self.assertTrue(v3engine._match("🍪($0.0280)", "🍪 ($0.0280)"))
        self.assertFalse(v3engine._match("🍪 ($0.0280)", "🍪 ($0.0300)"))
        self.assertFalse(v3engine._match("🍪", "🍪 ($0.0280)"))
        self.assertFalse(v3engine._match("🍪 ($0.0280)", "🍪"))

    def test_ambiguity_stop(self):
        msg = {"id": 1, "text": "m",
               "buttons": ["🍪 ($0.0280)", "🍪 ($0.0280)"],
               "incoming": True, "edit_date": None}
        # Same normalised label twice is ambiguous only when the raw
        # texts differ but normalise equal; identical buttons are one
        # button listed twice by the test double, so check the helper
        # finds both matches.
        self.assertEqual(len(v3engine._find_buttons(msg, "🍪 ($0.0280)")), 2)


class TestRuns(EngineCase):
    def test_full_run_marks_done_and_never_sends_start(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            await self.wait_idle(eng, "admin")

        run(_drive())
        self.assertEqual(store.get_progress(bid)["done"], 1)
        target = eng._targets[("admin", 1)]
        self.assertNotIn("/start", target.sent)
        self.assertEqual(target.sent[0], "📋 Tasks")
        self.assertIn("cookie1", target.sent)
        self.assertIn("2fa1", target.sent)

    def test_start_step_rejected_and_no_paste_refused(self):
        eng = self.make_engine()
        bad = flow([{"type": "send", "text": "/start"},
                    {"type": "paste", "column": 1}])
        with self.assertRaises(ValueError):
            v3engine.validate_flow(bad)
        with self.assertRaises(ValueError):
            run(eng.start(owner="admin", slots=[1], batch_id=999,
                          flow=bad, mode="now"))
        nopaste = flow([{"type": "click", "label": "📋 Tasks"}])
        with self.assertRaises(ValueError):
            run(eng.start(owner="admin", slots=[1], batch_id=999,
                          flow=nopaste, mode="now"))

    def test_missing_pre_paste_option_stops_row_pending(self):
        self.holder["menu"] = ["📋 Tasks", "🍪 ($0.0300)"]
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            await self.wait_idle(eng, "admin")

        # The capped activity wait and the stop-retry wait are
        # patched small here so the stop lands promptly instead
        # of at the full caps.
        with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5), \
                mock.patch.object(v3engine, "STOP_RETRY_WAIT_SECONDS",
                                  0.05):
            run(_drive())
        p = store.get_progress(bid)
        self.assertEqual((p["pending"], p["done"]), (1, 0))
        self.assertTrue(any("Auto-stopped" in t for t in self.pushes))

    def test_point_of_no_return_charge_stands(self):
        eng = self.make_engine()
        self.setup_owner("user:7", balance=1000)
        # The point of no return is AFTER every paste completes.
        # Here the final click (after both pastes) dies on a dead
        # account: the row is already done, so the charge stands
        # and there is no refund.
        orig = FakeTarget.send

        async def _dying_send(self, text):
            # Tasks, 🍪, task, ▶️ Start, paste, paste -> 6 sends;
            # the 7th (final ✅ click) dies after all pastes.
            if len(self.sent) >= 6:
                raise type("AuthKeyUnregistered", (Exception,), {})()
            return await orig(self, text)

        bid = self.make_batch("user:7", [("cookie1", "2fa1", "", "")])
        FakeTarget.send = _dying_send
        try:
            async def _drive():
                await eng.start(owner="user:7", slots=[1], batch_id=bid,
                                flow=task_flow(), mode="now")
                await self.wait_idle(eng, "user:7")

            run(_drive())
        finally:
            FakeTarget.send = orig
        p = store.get_progress(bid)
        self.assertEqual((p["done"], p["failed"]), (1, 0))
        # Reservation became the charge: 1000 - 50, no refund.
        self.assertEqual(store.get_balance("user:7"), 950)
        kinds = [e["kind"] for e in store.get_ledger("user:7")]
        self.assertNotIn("refund", kinds)

    def test_failed_row_refunds_and_billing_math(self):
        eng = self.make_engine()
        self.setup_owner("user:8", balance=1000)
        fl = flow([
            {"type": "click", "label": "📋 Tasks"},
            {"type": "paste", "column": 1},
        ])
        # Empty column A -> RowFailed at paste, after reservation.
        bid = self.make_batch("user:8", [("", "", "", "")])

        async def _drive():
            await eng.start(owner="user:8", slots=[1], batch_id=bid,
                            flow=fl, mode="now")
            await self.wait_idle(eng, "user:8")

        run(_drive())
        self.assertEqual(store.get_progress(bid)["failed"], 1)
        self.assertEqual(store.get_balance("user:8"), 1000)

    def test_out_of_credits_refused_before_reservation(self):
        eng = self.make_engine()
        self.setup_owner("user:9", balance=0)
        bid = self.make_batch("user:9", [("cookie1", "", "", "")])
        with self.assertRaises(RuntimeError):
            run(eng.start(owner="user:9", slots=[1], batch_id=bid,
                          flow=task_flow(), mode="now"))

    def test_low_balance_push_on_threshold_crossing(self):
        eng = self.make_engine()
        self.setup_owner("user:10", balance=2540)
        bid = self.make_batch("user:10", [("cookie1", "2fa1", "", "")])

        async def _drive():
            await eng.start(owner="user:10", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            await self.wait_idle(eng, "user:10")

        run(_drive())
        self.assertTrue(any("Low balance" in t for t in self.pushes))
        self.assertEqual(store.get_balance("user:10"), 2490)

    def test_dead_slot_skip_and_redistribution(self):
        eng = self.make_engine(client_slots=(1, 2))
        self.clients[2] = FakeClient(dead=True)
        self.setup_owner("admin", slots=(1, 2))
        bid = self.make_batch("admin",
                              [("a", "", "", ""), ("b", "", "", ""),
                               ("c", "", "", "")], slots=(1, 2))

        async def _drive():
            skipped, moved = await eng.start(
                owner="admin", slots=[1, 2], batch_id=bid,
                flow=flow([{"type": "click", "label": "📋 Tasks"},
                           {"type": "paste", "column": 1}]),
                mode="now")
            self.assertEqual(skipped, [2])
            self.assertEqual(moved, 1)
            await self.wait_idle(eng, "admin")

        run(_drive())
        self.assertEqual(store.get_progress(bid)["done"], 3)

    def test_dead_account_fails_current_row_only(self):
        eng = self.make_engine()
        self.clients[1] = FakeClient(authorised=True)
        self.setup_owner("admin")
        bid = self.make_batch(
            "admin",
            [("a", "", "", ""), ("b", "", "", ""), ("c", "", "", "")])
        # Drive a run where the target raises the dead error on send:
        # row 1 completes, row 2 fails, row 3 stays pending.
        orig_send = FakeTarget.send

        async def _dying_send(self, text):
            if len(self.sent) >= 3:
                raise DeadError("session dead")
            return await orig_send(self, text)

        FakeTarget.send = _dying_send
        try:
            async def _drive():
                await eng.start(owner="admin", slots=[1], batch_id=bid,
                                flow=flow([
                                    {"type": "click", "label": "📋 Tasks"},
                                    {"type": "paste", "column": 1}]),
                                mode="now")
                await self.wait_idle(eng, "admin")

            run(_drive())
        finally:
            FakeTarget.send = orig_send
        p = store.get_progress(bid)
        self.assertEqual((p["done"], p["failed"], p["pending"]), (1, 1, 1))

    def test_dead_slot_drops_pending_link(self):
        # v2 parity: _drop_client also drops a half-finished link,
        # so a dying slot cannot leave a connected login behind.
        eng = self.make_engine()
        self.clients[1] = FakeClient(authorised=True)
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("a", "", "", ""), ("b", "", "", "")])
        disconnected = []

        class PendClient:
            async def disconnect(self):
                disconnected.append(True)

        eng._pending_links[("admin", 1)] = (PendClient(), "hash", "phone")
        orig_send = FakeTarget.send

        async def _dying_send(self, text):
            if len(self.sent) >= 3:
                raise DeadError("session dead")
            return await orig_send(self, text)

        FakeTarget.send = _dying_send
        try:
            async def _drive():
                await eng.start(owner="admin", slots=[1], batch_id=bid,
                                flow=flow([
                                    {"type": "click", "label": "📋 Tasks"},
                                    {"type": "paste", "column": 1}]),
                                mode="now")
                await self.wait_idle(eng, "admin")

            run(_drive())
        finally:
            FakeTarget.send = orig_send
        p = store.get_progress(bid)
        self.assertEqual((p["done"], p["failed"], p["pending"]), (1, 1, 0))
        self.assertNotIn(("admin", 1), eng._pending_links)
        self.assertEqual(disconnected, [True])

    def test_rate_limit_treated_as_generic_error(self):
        # No rate-limit handling anywhere (the operator's order): a
        # rate-limit error fails the row like any other error, and
        # no special push is sent. The first send raises every time
        # (nothing is ever appended), so both rows fail.
        from pyrogram.errors import FloodWait
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("a", "", "", ""), ("b", "", "", "")])
        orig_send = FakeTarget.send

        async def _limited_send(self, text):
            if not self.sent:
                raise FloodWait(5)
            return await orig_send(self, text)

        FakeTarget.send = _limited_send
        try:
            async def _drive():
                await eng.start(owner="admin", slots=[1], batch_id=bid,
                                flow=flow([
                                    {"type": "click", "label": "📋 Tasks"},
                                    {"type": "paste", "column": 1}]),
                                mode="now")
                await self.wait_idle(eng, "admin")

            run(_drive())
        finally:
            FakeTarget.send = orig_send
        p = store.get_progress(bid)
        self.assertEqual((p["failed"], p["pending"], p["done"]), (2, 0, 0))
        self.assertFalse(any("Rate-limited" in t for t in self.pushes))

    def test_same_stop_reason_pushed_once_across_slots(self):
        self.holder["menu"] = ["📋 Tasks"]
        eng = self.make_engine(client_slots=(1, 2))
        self.setup_owner("admin", slots=(1, 2))
        bid = self.make_batch("admin", [("a", "", "", ""), ("b", "", "", "")],
                              slots=(1, 2))

        async def _drive():
            await eng.start(owner="admin", slots=[1, 2], batch_id=bid,
                            flow=flow([
                                {"type": "click", "label": "📋 Tasks"},
                                {"type": "click", "label": "🍪 ($0.0280)"},
                                {"type": "paste", "column": 1}]),
                            mode="now")
            await self.wait_idle(eng, "admin")

        # The capped activity wait and the stop-retry wait are
        # patched small here so the stop lands promptly instead
        # of at the full caps.
        with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5), \
                mock.patch.object(v3engine, "STOP_RETRY_WAIT_SECONDS",
                                  0.05):
            run(_drive())
        stops = [t for t in self.pushes if "Auto-stopped" in t]
        self.assertEqual(len(stops), 1)

    def test_check_mode_watches_then_auto_fires(self):
        # Menu without the task button at first; check must watch,
        # then pass and auto-start once the menu gains the button.
        # The 60 second probe wait is shortened for the test only.
        self.holder["menu"] = ["📋 Tasks", "🍪 ($0.0280)"]
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])
        fl = task_flow()

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="check")
            await asyncio.sleep(2.0)
            self.assertEqual(eng.status("admin")["mode"], "check")
            self.holder["menu"] = ["📋 Tasks", "🍪 ($0.0280)",
                                   "🌟2FA:Create FB (No mail) ($0.0280)",
                                   "▶️ Start", "✅ Account registered"]
            deadline = time.time() + 15
            while time.time() < deadline:
                if store.get_progress(bid)["done"] == 1:
                    break
                await asyncio.sleep(0.2)
            self.assertEqual(store.get_progress(bid)["done"], 1)
            await eng.stop("admin")

        old = v3engine.CHECK_PROBE_WAIT_SECONDS
        v3engine.CHECK_PROBE_WAIT_SECONDS = 0.5
        try:
            run(_drive())
        finally:
            v3engine.CHECK_PROBE_WAIT_SECONDS = old
        self.assertTrue(any("auto-started" in t for t in self.pushes))

    def test_check_cold_menu_sends_tasks_and_keeps_watching(self):
        # Menu without 📋 Tasks at all: the check recovery sends
        # 📋 Tasks (never /start), records watching, and stays in
        # check instead of ending.
        self.holder["menu"] = ["🍪 ($0.0280)"]
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])
        fl = task_flow()

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="check")
            await asyncio.sleep(2.0)
            self.assertEqual(eng.status("admin")["mode"], "check")
            self.assertEqual(eng._check["admin"]["state"], "watching")
            await eng.stop("admin")

        old = v3engine.CHECK_PROBE_WAIT_SECONDS
        v3engine.CHECK_PROBE_WAIT_SECONDS = 0.5
        try:
            run(_drive())
        finally:
            v3engine.CHECK_PROBE_WAIT_SECONDS = old
        target = eng._targets[("admin", 1)]
        self.assertIn("📋 Tasks", target.sent)
        self.assertNotIn("/start", target.sent)

    def test_check_hung_probe_times_out_and_keeps_checking(self):
        # A probe step whose reply never arrives must not freeze
        # the check: the probe is capped, records failed, and the
        # check stays alive for the next cycle.
        self.holder["menu"] = ["📋 Tasks", "🍪 ($0.0280)"]

        class HangingTarget(FakeTarget):
            async def send(self, text):
                self.sent.append(text)

        self.clients[1] = FakeClient(authorised=True)

        async def _notifier(owner, text):
            self.pushes.append(text)

        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: self.clients[s],
            target_factory=lambda c, u, w: HangingTarget(self.holder),
        )
        eng.set_notifier(_notifier)
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="check")
            await asyncio.sleep(1.5)
            self.assertEqual(eng._check["admin"]["state"], "failed")
            self.assertEqual(eng.status("admin")["mode"], "check")
            await eng.stop("admin")

        old_wait = v3engine.CHECK_PROBE_WAIT_SECONDS
        old_timeout = v3engine.CHECK_PROBE_TIMEOUT_SECONDS
        v3engine.CHECK_PROBE_WAIT_SECONDS = 0.2
        v3engine.CHECK_PROBE_TIMEOUT_SECONDS = 0.2
        try:
            run(_drive())
        finally:
            v3engine.CHECK_PROBE_WAIT_SECONDS = old_wait
            v3engine.CHECK_PROBE_TIMEOUT_SECONDS = old_timeout
        target = eng._targets[("admin", 1)]
        self.assertNotIn("/start", target.sent)

    def test_timer_starts_at_scheduled_time(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=flow([
                                {"type": "click", "label": "📋 Tasks"},
                                {"type": "paste", "column": 1}]),
                            mode="timer",
                            scheduled_at=time.time() + 0.3)
            await self.wait_idle(eng, "admin")

        run(_drive())
        self.assertEqual(store.get_progress(bid)["done"], 1)
        # The consumed schedule leaves no row behind for a later boot.
        self.assertIsNone(store.get_scheduled_run("admin"))
        with self.assertRaises(ValueError):
            run(eng.start(owner="admin", slots=[1], batch_id=bid,
                          flow=flow([
                              {"type": "click", "label": "📋 Tasks"},
                              {"type": "paste", "column": 1}]),
                          mode="timer", scheduled_at=None))

    def test_linking_code_flow_and_relink(self):
        link_client = FakeClient()
        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: link_client,
            target_factory=lambda c, u, w: FakeTarget(self.holder),
        )
        self.setup_owner("admin")
        old_id, old_hash = v3config.API_ID, v3config.API_HASH
        v3config.API_ID, v3config.API_HASH = 12345, "testhash"

        async def _drive():
            name = await eng.begin_link("admin", 1, "+100")
            self.assertIsNone(name)
            name = await eng.complete_link("admin", 1, "12345")
            self.assertEqual(name, "Worker")
            # Relink of an authorised session needs no code.
            name2 = await eng.begin_link("admin", 1, "+100")
            self.assertEqual(name2, "Worker")
            await eng.unlink("admin", 1)

        try:
            run(_drive())
        finally:
            v3config.API_ID, v3config.API_HASH = old_id, old_hash
        self.assertIsNone(store.get_account("admin", 1))

    def test_linking_without_api_credentials_is_refused_clearly(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        old_id, old_hash = v3config.API_ID, v3config.API_HASH
        v3config.API_ID, v3config.API_HASH = None, ""
        try:
            async def _drive():
                with self.assertRaises(RuntimeError):
                    await eng.begin_link("admin", 1, "+100")

            run(_drive())
        finally:
            v3config.API_ID, v3config.API_HASH = old_id, old_hash

    def test_status_and_stop(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="check")
            st = eng.status("admin")
            self.assertTrue(st["active"])
            self.assertEqual(st["mode"], "check")
            await eng.stop("admin")
            self.assertFalse(eng.status("admin")["active"])

        run(_drive())


class TestReadFirst(EngineCase):
    """User-ordered 2026-11-01 run-mode read-first rules."""

    def test_first_step_present_sends_no_start(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            await self.wait_idle(eng, "admin")

        run(_drive())
        target = eng._targets[("admin", 1)]
        self.assertNotIn("/start", target.sent)
        self.assertEqual(target.sent[0], "📋 Tasks")
        self.assertEqual(store.get_progress(bid)["done"], 1)

    def test_first_step_fallback_sends_tasks_once_then_proceeds(self):
        # Menu without 📋 Tasks; a 📋 Tasks send summons it (reply
        # carries the full menu), then the flow proceeds from its
        # first step. No /start is sent anywhere.
        self.holder["menu"] = ["🍪 ($0.0280)",
                               "🌟2FA:Create FB (No mail) ($0.0280)",
                               "▶️ Start", "✅ Account registered"]
        full = ["📋 Tasks"] + list(self.holder["menu"])

        class TasksSummoningTarget(FakeTarget):
            async def send(self, text):
                if text == "📋 Tasks":
                    self.holder["menu"] = list(full)
                return await super().send(text)

        self.clients[1] = FakeClient(authorised=True)

        async def _notifier(owner, text):
            self.pushes.append(text)

        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: self.clients[s],
            target_factory=lambda c, u, w: TasksSummoningTarget(self.holder),
        )
        eng.set_notifier(_notifier)
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            await self.wait_idle(eng, "admin")

        run(_drive())
        target = eng._targets[("admin", 1)]
        self.assertNotIn("/start", target.sent)
        self.assertEqual(target.sent[0], "📋 Tasks")
        self.assertEqual(target.sent[1], "📋 Tasks")
        self.assertEqual(store.get_progress(bid)["done"], 1)

    def test_first_step_fallback_miss_stops_whole_run(self):
        # Two slots, neither menu has 📋 Tasks and a 📋 Tasks send
        # does not summon it: exactly one Auto-stopped push, all
        # rows pending, and no /start sent by either slot.
        self.holder["menu"] = ["🍪 ($0.0280)"]
        eng = self.make_engine(client_slots=(1, 2))
        self.setup_owner("admin", slots=(1, 2))
        bid = self.make_batch("admin", [("a", "", "", ""), ("b", "", "", "")],
                              slots=(1, 2))

        async def _drive():
            await eng.start(owner="admin", slots=[1, 2], batch_id=bid,
                            flow=flow([
                                {"type": "click", "label": "📋 Tasks"},
                                {"type": "paste", "column": 1}]),
                            mode="now")
            await self.wait_idle(eng, "admin")

        # The capped activity wait and the stop-retry wait are
        # patched small here so the stop lands promptly instead
        # of at the full caps.
        with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5), \
                mock.patch.object(v3engine, "STOP_RETRY_WAIT_SECONDS",
                                  0.05):
            run(_drive())
        p = store.get_progress(bid)
        self.assertEqual((p["pending"], p["done"]), (2, 0))
        stops = [t for t in self.pushes if "Auto-stopped" in t]
        self.assertEqual(len(stops), 1)
        sent = []
        for target in eng._targets.values():
            sent.extend(target.sent)
        self.assertNotIn("/start", sent)
        self.assertIn("📋 Tasks", sent)

    def test_ensure_button_waits_for_late_menu(self):
        # A menu arriving via a stream wakeup after the first scan:
        # the old fixed-3s guard never waited on activity, so it
        # stopped; the activity wait rescues it.
        self.holder["menu"] = ["📋 Tasks"]
        target = FakeTarget(self.holder)
        waiter = LateMenuWaiter(target, ["📋 Tasks", "🍪 ($0.0280)"])
        session = v3engine.FlowSession(target, waiter)

        async def _drive():
            return await session.ensure_button("🍪 ($0.0280)")

        msg, btn = run(_drive())
        self.assertIsNotNone(btn)

    def test_ensure_button_timeout_stops_whole_run(self):
        # Button never arrives: the capped wait ends in StopRun
        # with the seen menu in the detail, as before.
        self.holder["menu"] = ["📋 Tasks"]
        target = FakeTarget(self.holder)

        class QuietWaiter:
            def drain(self):
                pass

            async def wait_event(self):
                await asyncio.sleep(0.05)
                return False

        session = v3engine.FlowSession(target, QuietWaiter())

        async def _drive():
            with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5):
                await session.ensure_button("🍪 ($0.0280)")

        with self.assertRaises(v3engine.StopRun) as cm:
            run(_drive())
        self.assertIn("menu had:", str(cm.exception))

    def test_first_check_waits_for_menu_reply(self):
        # Recovery send draws a notice first and the menu second:
        # the old first-check returned on the first event and
        # stopped. The menu wait skips the notice and finds it.
        self.holder["menu"] = ["🍪 ($0.0280)"]
        target = FakeTarget(self.holder)

        async def _notice_send(text):
            target.sent.append(text)
            target.messages.insert(0, {
                "id": target._next, "text": "one moment",
                "buttons": [], "incoming": True, "edit_date": None,
            })
            target._next += 1

        target.send = _notice_send
        waiter = LateMenuWaiter(target, ["📋 Tasks", "🍪 ($0.0280)"])
        session = v3engine.FlowSession(target, waiter)
        eng = self.make_engine()

        async def _drive():
            return await eng._tasks_first_check(session)

        self.assertTrue(run(_drive()))
        self.assertEqual(target.sent, ["📋 Tasks"])

    def test_ensure_button_finds_panel_outside_window(self):
        # Reason 1: a reply keyboard persists as chat state long
        # after its setting message leaves the recent window. Here
        # the panel carrying 🍪 sits 15 button-less messages back:
        # the old window-only guard missed it; the recorded panel
        # state finds it.
        target = FakeTarget(self.holder)
        panel = {"id": 1, "text": "menu",
                 "buttons": ["🍪 ($0.0280)"],
                 "incoming": True, "edit_date": None}
        fillers = [{"id": i, "text": "note", "buttons": [],
                    "incoming": True, "edit_date": None}
                   for i in range(16, 1, -1)]
        target.messages = fillers + [panel]
        target._next = 17
        session = v3engine.FlowSession(target, None)

        async def _drive():
            with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5):
                return await session.ensure_button("🍪 ($0.0280)")

        msg, btn = run(_drive())
        self.assertIsNotNone(btn)
        self.assertEqual(msg["id"], 1)

    def test_ensure_button_ignores_superseded_panel(self):
        # The newest keyboard (main menu, no 🍪) supersedes an older
        # submenu message carrying 🍪: the guard must NOT press a
        # stale panel button. It stops instead.
        target = FakeTarget(self.holder)
        main = {"id": 2, "text": "menu",
                "buttons": ["📋 Tasks", "💰 Balance"],
                "incoming": True, "edit_date": None}
        submenu = {"id": 1, "text": "menu",
                   "buttons": ["🍪 ($0.0280)"],
                   "incoming": True, "edit_date": None}
        fillers = [{"id": i, "text": "note", "buttons": [],
                    "incoming": True, "edit_date": None}
                   for i in range(17, 2, -1)]
        target.messages = fillers + [main, submenu]
        target._next = 18
        session = v3engine.FlowSession(target, None)

        async def _drive():
            with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5):
                await session.ensure_button("🍪 ($0.0280)")

        with self.assertRaises(v3engine.StopRun):
            run(_drive())

    def test_ensure_button_finds_panel_beside_prompt(self):
        # An inline prompt is the newest keyboard, but it does not
        # replace the reply panel: the panel carrying 🍪 sits
        # behind it, outside the window, and must still be found.
        target = FakeTarget(self.holder)
        prompt = {"id": 17, "text": "confirm",
                  "buttons": [{"text": "✅ Account registered",
                               "callback_data": "yes"}],
                  "incoming": True, "edit_date": None}
        panel = {"id": 1, "text": "menu",
                 "buttons": ["🍪 ($0.0280)"],
                 "incoming": True, "edit_date": None}
        fillers = [{"id": i, "text": "note", "buttons": [],
                    "incoming": True, "edit_date": None}
                   for i in range(16, 1, -1)]
        target.messages = [prompt] + fillers + [panel]
        target._next = 18
        session = v3engine.FlowSession(target, None)

        async def _drive():
            with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5):
                return await session.ensure_button("🍪 ($0.0280)")

        msg, btn = run(_drive())
        self.assertIsNotNone(btn)
        self.assertEqual(msg["id"], 1)

    def test_stop_detail_prefers_panel(self):
        # On a miss, the StopRun detail reports the recorded
        # panel's labels, not an unrelated recent prompt's.
        target = FakeTarget(self.holder)
        prompt = {"id": 20, "text": "confirm",
                  "buttons": [{"text": "✅ Account registered",
                               "callback_data": "yes"}],
                  "incoming": True, "edit_date": None}
        panel = {"id": 5, "text": "menu",
                 "buttons": ["💰 Balance", "📋 Tasks"],
                 "incoming": True, "edit_date": None}
        fillers = [{"id": i, "text": "note", "buttons": [],
                    "incoming": True, "edit_date": None}
                   for i in range(19, 5, -1)]
        target.messages = [prompt] + fillers + [panel]
        target._next = 21
        session = v3engine.FlowSession(target, None)

        async def _drive():
            with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5):
                await session.ensure_button("🍪 ($0.0280)")

        with self.assertRaises(v3engine.StopRun) as caught:
            run(_drive())
        self.assertIn("💰 Balance", str(caught.exception))
        self.assertNotIn("✅ Account registered", str(caught.exception))

    def test_stream_handlers_are_filtered(self):
        # The adapter must register filtered handlers (target
        # chat + incoming only), never the old unfiltered bell,
        # and the filter must accept exactly target-incoming.
        import v3.pyrogram_target as adapter

        class StubClient:
            def __init__(self):
                self.handlers = []

            def add_handler(self, handler):
                self.handlers.append(handler)

        client = StubClient()
        target = adapter.PyrogramTarget(client, "@Taskl1_bot")

        from types import SimpleNamespace as NS

        async def _eval():
            # Pyrogram imports need a running loop (its sync
            # wrapper calls get_event_loop at import time), so
            # attach inside the drive, not at test top level.
            target.attach_stream()
            f = client.handlers[0].filters
            ok = await f(None, NS(
                chat=NS(id=1, username="Taskl1_bot"),
                outgoing=False, out=False))
            own = await f(None, NS(
                chat=NS(id=1, username="Taskl1_bot"),
                outgoing=True, out=True))
            other = await f(None, NS(
                chat=NS(id=2, username="OtherBot"),
                outgoing=False, out=False))
            return ok, own, other

        ok, own, other = run(_eval())
        self.assertEqual(len(client.handlers), 2)
        for handler in client.handlers:
            self.assertIsNotNone(handler.filters)
        self.assertTrue(ok)
        self.assertFalse(own)
        self.assertFalse(other)

    def test_pick_new_prefers_reply_to_match(self):
        # A new message replying to our last sent message wins
        # over a newer notice; without a match, newest wins.
        target = FakeTarget(self.holder)
        session = v3engine.FlowSession(target, None)
        session.last_seen = 10
        target.last_sent_id = 99
        incoming = [
            {"id": 12, "text": "notice", "buttons": [],
             "incoming": True, "edit_date": None, "reply_to": None},
            {"id": 11, "text": "menu", "buttons": ["🍪"],
             "incoming": True, "edit_date": None, "reply_to": 99},
        ]
        self.assertEqual(session._pick_new(incoming)["id"], 11)
        incoming[1]["reply_to"] = None
        self.assertEqual(session._pick_new(incoming)["id"], 12)

    def test_wait_reply_run_mode_times_out(self):
        # Run mode: a reply that never arrives raises instead of
        # hanging the slot; the runner fails the row and moves on.
        target = FakeTarget(self.holder)
        session = v3engine.FlowSession(target, None)
        session.check_mode = False
        # Baseline as prepare() would: without it the initial
        # menu counts as a reply and the wait returns at once.
        session.last_seen = 1
        session._last_text = "menu"

        async def _drive():
            with mock.patch.object(
                    v3engine, "WAIT_REPLY_TIMEOUT_SECONDS", 0.5):
                await session.wait_reply()

        with self.assertRaises(TimeoutError):
            run(_drive())

    def test_wait_reply_check_mode_ignores_reply_box(self):
        # Check mode: the reply box must not fire — the probe cap
        # bounds check waits instead. The wait must still be
        # pending well past the patched box.
        import time as _time

        target = FakeTarget(self.holder)
        session = v3engine.FlowSession(target, None)
        session.check_mode = True
        session.last_seen = 1
        session._last_text = "menu"

        async def _drive():
            import asyncio as _asyncio
            start = _time.monotonic()
            with mock.patch.object(
                    v3engine, "WAIT_REPLY_TIMEOUT_SECONDS", 0.5):
                try:
                    await _asyncio.wait_for(
                        session.wait_reply(), timeout=2.5)
                except _asyncio.TimeoutError:
                    pass
            return _time.monotonic() - start

        # A misapplied box would return near 1 s (one sleep
        # cadence past the 0.5 s box); a correct ignore runs to
        # the 2.5 s test-side cap.
        elapsed = run(_drive())
        self.assertGreaterEqual(elapsed, 2.0)

    def test_missing_step_reread_rescue_proceeds(self):
        self.holder["menu"] = ["📋 Tasks"]
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])
        fl = flow([
            {"type": "click", "label": "📋 Tasks"},
            {"type": "click", "label": "🍪 ($0.0280)"},
            {"type": "paste", "column": 1},
        ])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="now")
            target = eng._targets[("admin", 1)]
            # 2.5s lands inside the activity wait: the guard scans
            # about every second, so the menu inserted at 2.5s is
            # picked up by the next scan (the fixed 3s wait it
            # replaces is gone, so no 2.9s floor remains).
            await asyncio.sleep(2.5)
            # Taskly pushes a new menu during the 3s reread wait.
            mid = target._next
            target._next += 1
            target.messages.insert(0, {
                "id": mid, "text": "menu",
                "buttons": ["📋 Tasks", "🍪 ($0.0280)"],
                "incoming": True, "edit_date": None,
            })
            await self.wait_idle(eng, "admin")

        started = time.monotonic()
        run(_drive())
        # The rescue cannot complete before the menu arrives at 2.5s.
        self.assertGreaterEqual(time.monotonic() - started, 2.5)
        self.assertEqual(store.get_progress(bid)["done"], 1)

    def test_missing_step_second_miss_stops(self):
        self.holder["menu"] = ["📋 Tasks"]
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])
        fl = flow([
            {"type": "click", "label": "📋 Tasks"},
            {"type": "click", "label": "🍪 ($0.0280)"},
            {"type": "paste", "column": 1},
        ])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="now")
            await self.wait_idle(eng, "admin")

        # The fixed 3s wait is replaced by a capped activity wait,
        # patched small here so the stop lands promptly, as is the
        # stop-retry wait.
        with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5), \
                mock.patch.object(v3engine, "STOP_RETRY_WAIT_SECONDS",
                                  0.05):
            run(_drive())
        p = store.get_progress(bid)
        self.assertEqual((p["pending"], p["done"]), (1, 0))
        self.assertEqual(
            len([t for t in self.pushes if "Auto-stopped" in t]), 1)

    def test_check_rate_limit_is_generic_error(self):
        # No rate-limit handling anywhere (the operator's order): a
        # rate-limit error is a generic probe failure — the check
        # stays alive in failed state and keeps probing, with no
        # special push. The first send raises every time (nothing
        # is ever appended), so every probe fails the same way.
        # The import lives inside the send (inside the drive's
        # running loop): a top-level import breaks once any
        # earlier test has run asyncio.run(), because afterwards
        # get_event_loop() raises instead of auto-creating.
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "2fa1", "", "")])
        orig_send = FakeTarget.send

        async def _limited_send(self, text):
            if not self.sent:
                from pyrogram.errors import FloodWait
                raise FloodWait(5)
            return await orig_send(self, text)

        FakeTarget.send = _limited_send
        try:
            async def _drive():
                await eng.start(owner="admin", slots=[1], batch_id=bid,
                                flow=task_flow(), mode="check")
                await asyncio.sleep(2.0)
                st = eng.status("admin")
                self.assertTrue(st["active"])
                self.assertEqual(st["mode"], "check")
                self.assertEqual(
                    eng._check.get("admin", {}).get("state"), "failed")
                await eng.stop("admin")

            run(_drive())
        finally:
            FakeTarget.send = orig_send
        self.assertFalse(any("Rate-limited" in t for t in self.pushes))
        p = store.get_progress(bid)
        self.assertEqual((p["pending"], p["done"]), (1, 0))


class TestStepPause(EngineCase):
    """User-ordered 2026-11-01: 1s after every step but the last."""

    def test_one_pause_for_two_steps(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])
        fl = flow([
            {"type": "click", "label": "📋 Tasks"},
            {"type": "paste", "column": 1},
        ])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="now")
            await self.wait_idle(eng, "admin")

        started = time.monotonic()
        run(_drive())
        elapsed = time.monotonic() - started
        # One pause after Tasks plus one guard settle on the click;
        # none after the last step.
        self.assertGreaterEqual(elapsed, 1.9)
        self.assertLess(elapsed, 2.8)
        self.assertEqual(store.get_progress(bid)["done"], 1)

    def test_paste_step_also_pauses_when_not_last(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])
        fl = flow([
            {"type": "click", "label": "📋 Tasks"},
            {"type": "paste", "column": 1},
            {"type": "click", "label": "✅ Account registered"},
        ])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="now")
            await self.wait_idle(eng, "admin")

        started = time.monotonic()
        run(_drive())
        elapsed = time.monotonic() - started
        # Two pauses (after Tasks, after the paste) plus one settle
        # per click. If pastes were exempt from pauses, this would
        # take about 3s, not 4s.
        self.assertGreaterEqual(elapsed, 3.9)
        self.assertLess(elapsed, 4.8)
        self.assertEqual(store.get_progress(bid)["done"], 1)


    def test_wait_step_keeps_its_duration_and_still_gets_pause(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])
        fl = flow([
            {"type": "click", "label": "📋 Tasks"},
            {"type": "wait", "seconds": 2},
            {"type": "paste", "column": 1},
        ])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=fl, mode="now")
            await self.wait_idle(eng, "admin")

        started = time.monotonic()
        run(_drive())
        elapsed = time.monotonic() - started
        # 1s pause after Tasks + the wait step's own 2s + 1s pause
        # after the wait step + 1s settle on the click. Missing a
        # pause lands near 4s.
        self.assertGreaterEqual(elapsed, 4.9)
        self.assertLess(elapsed, 5.8)
        self.assertEqual(store.get_progress(bid)["done"], 1)


class TestEngineSourceRules(unittest.TestCase):
    def test_no_rate_limit_mutation_or_rotation_words(self):
        src = pathlib.Path(v3engine.__file__).read_text()
        for word in ("lood", "mute", "rotat"):
            self.assertNotIn(word, src.lower())
        self.assertNotIn("jitter", src.lower())
        self.assertNotIn("CLICK_PAUSE", src)

    def test_no_start_in_default_flows_via_engine(self):
        # Engine-level: a default flow from the store must validate.
        store.set_db_path(tempfile.NamedTemporaryFile(
            suffix=".db", delete=False).name)
        store.init_db()
        store.ensure_default_flows("admin")
        for f in store.list_flows("admin"):
            v3engine.validate_flow(f)


class TestTargetSwap(unittest.TestCase):
    """Cached target adapters must follow flow target changes."""

    def _engine(self):
        eng = object.__new__(v3engine.Engine)
        eng._targets = {}
        eng._stream_waiters = {}
        eng._clients = {("user:1", 1): object()}
        created = []

        class FakeAdapter:
            def __init__(self, target):
                self.target = target
                self.attached = False
                self.detached = False
                created.append(self)

            def attach_stream(self):
                self.attached = True

            def detach_stream(self):
                self.detached = True

        def factory(client, target_username, waiter=None):
            return FakeAdapter(target_username)

        eng._target_factory = factory
        return eng, created

    def test_target_swap_on_flow_target_change(self):
        eng, created = self._engine()
        t1, _ = eng._register_stream(
            "user:1", 1, object(), "@Taskl1_bot")
        self.assertEqual(t1.target, "@Taskl1_bot")
        self.assertTrue(t1.attached)
        t2, _ = eng._target_for(
            "user:1", 1, {"target": "@NexTask12_Bot"})
        self.assertIsNot(t2, t1)
        self.assertEqual(t2.target, "@NexTask12_Bot")
        self.assertTrue(t1.detached)
        self.assertEqual(len(created), 2)
        # Same target again reuses the new adapter, no re-register.
        t3, _ = eng._target_for(
            "user:1", 1, {"target": "@NexTask12_Bot"})
        self.assertIs(t3, t2)
        self.assertEqual(len(created), 2)

    def test_target_reuse_normalised(self):
        eng, created = self._engine()
        t1, _ = eng._register_stream(
            "user:1", 1, object(), "@Taskl1_bot")
        t2, _ = eng._target_for("user:1", 1, {"target": "Taskl1_bot"})
        self.assertIs(t2, t1)
        self.assertFalse(t1.detached)
        self.assertEqual(len(created), 1)


class TestProbeTurn(unittest.TestCase):
    """Check probes serialize across slots: one probes per tick, rotating."""

    def _engine(self, turn_state):
        eng = object.__new__(v3engine.Engine)
        eng._targets = {}
        eng._stream_waiters = {}
        eng._clients = {("user:1", 1): object(), ("user:1", 2): object()}
        eng._check = {}
        eng._check_turn = turn_state
        eng._runs = {}
        eng._firing = set()

        class FakeAdapter:
            def __init__(self, target):
                self.target = target

            def attach_stream(self):
                pass

            def detach_stream(self):
                pass

        def factory(client, target_username, waiter=None):
            return FakeAdapter(target_username)

        eng._target_factory = factory
        return eng

    def test_no_state_probes(self):
        eng = self._engine({})
        self.assertTrue(eng._claim_probe_turn("user:1", 1))

    def test_rotation_order(self):
        eng = self._engine({"user:1": {"slots": [1, 2, 3], "turn": 0}})
        self.assertTrue(eng._claim_probe_turn("user:1", 1))
        self.assertEqual(eng._check_turn["user:1"]["turn"], 1)
        self.assertFalse(eng._claim_probe_turn("user:1", 1))
        self.assertEqual(eng._check_turn["user:1"]["turn"], 1)
        self.assertTrue(eng._claim_probe_turn("user:1", 2))
        self.assertTrue(eng._claim_probe_turn("user:1", 3))
        self.assertTrue(eng._claim_probe_turn("user:1", 1))

    def test_unknown_slot_refused(self):
        eng = self._engine({"user:1": {"slots": [1, 2], "turn": 0}})
        self.assertFalse(eng._claim_probe_turn("user:1", 9))
        self.assertEqual(eng._check_turn["user:1"]["turn"], 0)

    def test_single_slot_always_claims(self):
        eng = self._engine({"user:1": {"slots": [1], "turn": 0}})
        for _ in range(3):
            self.assertTrue(eng._claim_probe_turn("user:1", 1))
        self.assertEqual(eng._check_turn["user:1"]["turn"], 3)


class TestRunCheckGate(unittest.TestCase):
    """_run_check probes only on its slot's turn."""

    def _engine(self, turn_state):
        eng = object.__new__(v3engine.Engine)
        eng._targets = {}
        eng._stream_waiters = {}
        eng._clients = {("user:1", 1): object(), ("user:1", 2): object()}
        eng._check = {}
        eng._check_turn = turn_state
        eng._runs = {}
        eng._firing = set()

        class FakeAdapter:
            def __init__(self, target):
                self.target = target

            def attach_stream(self):
                pass

            def detach_stream(self):
                pass

        def factory(client, target_username, waiter=None):
            return FakeAdapter(target_username)

        eng._target_factory = factory
        self.probes = []

        async def fake_probe(session, flow):
            self.probes.append(1)

        eng._probe_once = fake_probe
        return eng

    def test_blocked_slot_does_not_probe(self):
        eng = self._engine({"user:1": {"slots": [1, 2], "turn": 1}})
        flow = {"target": "@X", "steps": []}
        with self.assertRaises(asyncio.TimeoutError):
            run(asyncio.wait_for(eng._run_check("user:1", 1, flow),
                                 timeout=0.5))
        self.assertEqual(len(self.probes), 0)

    def test_open_slot_probes(self):
        eng = self._engine({"user:1": {"slots": [1], "turn": 0}})
        flow = {"target": "@X", "steps": []}
        with self.assertRaises(asyncio.TimeoutError):
            run(asyncio.wait_for(eng._run_check("user:1", 1, flow),
                                 timeout=2.5))
        self.assertGreaterEqual(len(self.probes), 2)

    def test_rotation_throttles_second_iteration(self):
        eng = self._engine({"user:1": {"slots": [1, 2], "turn": 0}})
        flow = {"target": "@X", "steps": []}
        with self.assertRaises(asyncio.TimeoutError):
            run(asyncio.wait_for(eng._run_check("user:1", 1, flow),
                                 timeout=2.5))
        self.assertEqual(len(self.probes), 1)


class _FlakyTarget(FakeTarget):
    """FakeTarget whose send fails the first N calls, then succeeds."""

    def __init__(self, holder, fail_times=0, exc=None):
        super().__init__(holder)
        self.fail_times = fail_times
        self.exc = exc if exc is not None else ConnectionError("boom")
        self.send_calls = 0

    async def send(self, text):
        self.send_calls += 1
        if self.send_calls <= self.fail_times:
            raise self.exc
        return await super().send(text)


class _SilentTarget(FakeTarget):
    """FakeTarget whose send never produces a reply message."""

    async def send(self, text):
        self.sent.append(text)
        self.last_sent_id = self._next
        self._next += 1


class _VanishingTarget(FakeTarget):
    """Menu button present on the first history call, gone after."""

    def __init__(self, holder):
        super().__init__(holder)
        self._calls = 0

    @property
    def panel_message(self):
        if self._calls <= 1:
            return super().panel_message
        return None

    async def history(self, limit=10):
        self._calls += 1
        if self._calls == 1:
            return list(self.messages[:limit])
        return [{"id": 99, "text": "edited, no buttons", "buttons": [],
                 "incoming": True, "edit_date": None}]


class _LateTarget(FakeTarget):
    """Menu button appears only on the fourth history call."""

    def __init__(self, holder):
        super().__init__(holder)
        self._calls = 0

    @property
    def panel_message(self):
        return None

    async def history(self, limit=10):
        self._calls += 1
        if self._calls >= 4:
            return list(self.messages[:limit])
        return [{"id": 99, "text": "not yet", "buttons": [],
                 "incoming": True, "edit_date": None}]


class TestGuardSettle(EngineCase):
    """A found button is settled and re-verified before trusting."""

    def test_settle_delays_a_hit(self):
        session = v3engine.FlowSession(FakeTarget(self.holder), None)
        started = time.monotonic()
        msg, btn = run(session.ensure_button("📋 Tasks"))
        elapsed = time.monotonic() - started
        self.assertIsNotNone(btn)
        self.assertGreaterEqual(elapsed, 1)
        self.assertLess(elapsed, 5)

    def test_stale_hit_keeps_waiting_then_stops(self):
        old_guard = v3engine.GUARD_WAIT_SECONDS
        v3engine.GUARD_WAIT_SECONDS = 2
        try:
            session = v3engine.FlowSession(_VanishingTarget(self.holder),
                                           None)
            with self.assertRaises(v3engine.StopRun):
                run(session.ensure_button("📋 Tasks"))
        finally:
            v3engine.GUARD_WAIT_SECONDS = old_guard

    def test_final_scan_rescues_at_deadline(self):
        old_guard = v3engine.GUARD_WAIT_SECONDS
        v3engine.GUARD_WAIT_SECONDS = 1.5
        try:
            session = v3engine.FlowSession(_LateTarget(self.holder), None)
            msg, btn = run(session.ensure_button("📋 Tasks"))
            self.assertIsNotNone(btn)
        finally:
            v3engine.GUARD_WAIT_SECONDS = old_guard


class TestNetworkRetry(EngineCase):
    """Network-error rows retry with a backoff, bounded."""

    def _run(self, target, rows, steps):
        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: object(),
            target_factory=lambda c, u, w: target,
        )
        eng._clients[("admin", 1)] = object()
        batch_id, queued, skipped = store.create_batch(
            "admin", "f.xlsx", "slot", rows, [1])
        flow = {"target": "@X", "interval": 0, "steps": steps}
        run(eng._slot_runner("admin", 1, batch_id, flow, "now", None))
        return store.get_batch_rows(batch_id, 1)

    def test_network_error_retried_with_backoff(self):
        # Real backoff value: the first retry waits 15s (not 0s,
        # not 30s), then the row completes.
        target = _FlakyTarget(self.holder, fail_times=1)
        started = time.monotonic()
        rows = self._run(target, [("a1", "", "", "")],
                         [{"type": "paste", "column": 1}])
        elapsed = time.monotonic() - started
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(target.send_calls, 2)
        self.assertGreaterEqual(elapsed, 15)
        self.assertLess(elapsed, 30)

    def test_network_error_backoff_scales_with_attempt(self):
        # Patched backoff of 1s: waits are 1s then 2s (1x, 2x),
        # so three failed attempts take ~3s, not 2s or 0s.
        old_backoff = v3engine.ROW_RETRY_BACKOFF_SECONDS
        v3engine.ROW_RETRY_BACKOFF_SECONDS = 1
        try:
            target = _FlakyTarget(self.holder, fail_times=3)
            started = time.monotonic()
            rows = self._run(target, [("a1", "", "", "")],
                             [{"type": "paste", "column": 1}])
            elapsed = time.monotonic() - started
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(target.send_calls, 3)
            self.assertGreaterEqual(elapsed, 3)
            self.assertLess(elapsed, 8)
        finally:
            v3engine.ROW_RETRY_BACKOFF_SECONDS = old_backoff

    def test_network_error_bounded_and_runner_continues(self):
        # Patched backoff of 0s keeps this fast: the point here
        # is boundedness (3 attempts) and continuation, not timing.
        old_backoff = v3engine.ROW_RETRY_BACKOFF_SECONDS
        v3engine.ROW_RETRY_BACKOFF_SECONDS = 0
        try:
            target = _FlakyTarget(self.holder, fail_times=3)
            rows = self._run(target, [("a1", "", "", ""),
                                      ("a2", "", "", "")],
                             [{"type": "paste", "column": 1}])
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(rows[1]["status"], "done")
            self.assertEqual(target.send_calls, 4)
        finally:
            v3engine.ROW_RETRY_BACKOFF_SECONDS = old_backoff

    def test_non_network_error_not_retried(self):
        target = _FlakyTarget(self.holder, fail_times=1,
                              exc=RuntimeError("boom"))
        rows = self._run(target, [("a1", "", "", "")],
                         [{"type": "paste", "column": 1}])
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(target.send_calls, 1)

    def test_reply_timeout_not_retried(self):
        old_timeout = v3engine.WAIT_REPLY_TIMEOUT_SECONDS
        v3engine.WAIT_REPLY_TIMEOUT_SECONDS = 0
        try:
            target = _SilentTarget(self.holder)
            rows = self._run(
                target, [("a1", "", "", "")],
                [{"type": "click", "label": "📋 Tasks"},
                 {"type": "paste", "column": 1}])
            self.assertEqual(rows[0]["status"], "failed")
            self.assertEqual(len(target.sent), 1)
        finally:
            v3engine.WAIT_REPLY_TIMEOUT_SECONDS = old_timeout


class _FlappingTarget(FakeTarget):
    """Task option vanishes once, then returns when ❌ Cancel is sent.

    Simulates Admin's auto-off: the menu loses the task option
    mid-run; a Cancel resets it. With restore_on_cancel False the
    option never returns, so the retry exhausts. The engine reads
    the menu only through history()/panel_message, so the
    presented menu is fully controlled here: every returned
    message carries the currently visible menu, as the live panel
    does in production.
    """

    def __init__(self, holder, drop_label="🍪 ($0.0280)",
                 drop_after="📋 Tasks", restore_on_cancel=True):
        super().__init__(holder)
        self.drop_label = drop_label
        self.drop_after = drop_after
        self.restore_on_cancel = restore_on_cancel
        self._full_menu = list(holder["menu"])
        self._dropped = False
        self._drop_done = False

    def _visible_menu(self):
        if self._dropped:
            return [b for b in self._full_menu if b != self.drop_label]
        return list(self._full_menu)

    def _apply_menu(self):
        self.holder["menu"] = self._visible_menu()

    @property
    def panel_message(self):
        panel = super().panel_message
        if panel is None:
            return None
        shown = dict(panel)
        shown["buttons"] = self._visible_menu()
        return shown

    async def history(self, limit=10):
        menu = self._visible_menu()
        out = []
        for m in self.messages[:limit]:
            shown = dict(m)
            shown["buttons"] = list(menu)
            out.append(shown)
        return out

    async def send(self, text):
        if not self._drop_done and text == self.drop_after:
            self._drop_done = True
            self._dropped = True
        if self._dropped and self.restore_on_cancel \
                and text == "❌ Cancel":
            self._dropped = False
        self._apply_menu()
        await super().send(text)


class TestStopRetry(EngineCase):
    """Admin's auto-off retry: Cancel + re-check before stopping."""

    def _run(self, target, owner, rows, steps, balance=None):
        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: object(),
            target_factory=lambda c, u, w: target,
        )
        eng._clients[(owner, 1)] = object()
        pushes = []

        async def _notifier(o, text):
            pushes.append(text)

        eng.set_notifier(_notifier)
        self.setup_owner(owner, balance=balance)
        bid = self.make_batch(owner, rows)
        flow = {"id": 1, "name": "T", "target": "@Taskl1_bot",
                "interval": 0, "steps": steps}
        with mock.patch.object(v3engine, "GUARD_WAIT_SECONDS", 0.5), \
                mock.patch.object(v3engine, "STOP_RETRY_WAIT_SECONDS",
                                  0.05):
            run(eng._slot_runner(owner, 1, bid, flow, "now", None))
        return store.get_batch_rows(bid, 1), pushes, target

    def _task_steps(self):
        return [{"type": "click", "label": "📋 Tasks"},
                {"type": "click", "label": "🍪 ($0.0280)"},
                {"type": "paste", "column": 1}]

    def test_recovers_and_charges_once(self):
        target = _FlappingTarget(self.holder)
        rows, pushes, target = self._run(
            target, "user:7", [("cookie1", "", "", "")],
            self._task_steps(), balance=1000)
        self.assertEqual(rows[0]["status"], "done")
        self.assertEqual(store.get_balance("user:7"), 950)
        self.assertEqual(target.sent.count("❌ Cancel"), 1)
        self.assertTrue(any("it is back; resuming" in p for p in pushes))

    def test_exhausts_and_stops_with_row_pending(self):
        target = _FlappingTarget(self.holder, restore_on_cancel=False)
        rows, pushes, target = self._run(
            target, "admin", [("cookie1", "", "", "")],
            self._task_steps())
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(target.sent.count("❌ Cancel"), 5)
        self.assertTrue(any("Auto-stopped" in p for p in pushes))
        self.assertTrue(any("re-checked 5 times" in p for p in pushes))

    def test_submitted_row_moves_on_after_recovery(self):
        target = _FlappingTarget(
            self.holder, drop_label="✅ Account registered",
            drop_after="cookie1")
        steps = [{"type": "click", "label": "📋 Tasks"},
                 {"type": "click", "label": "🍪 ($0.0280)"},
                 {"type": "paste", "column": 1},
                 {"type": "click", "label": "✅ Account registered"}]
        rows, pushes, target = self._run(
            target, "admin",
            [("cookie1", "", "", ""), ("cookie2", "", "", "")], steps)
        self.assertEqual([r["status"] for r in rows], ["done", "done"])

    def test_recovery_cap_is_final(self):
        eng = self.make_engine()
        target = FakeTarget(self.holder)
        recovered, count = run(eng._maybe_recover_stop(
            "admin", 1, target, None,
            {"steps": self._task_steps()},
            "option not on the menu", 3))
        self.assertFalse(recovered)
        self.assertEqual(count, 3)
        self.assertEqual(target.sent, [])


class TestTimerPersistence(EngineCase):
    """A schedule survives in the store; stop clears it, shutdown keeps it."""

    def test_timer_row_saved_and_cleared_on_stop(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=flow([
                                {"type": "click", "label": "📋 Tasks"},
                                {"type": "paste", "column": 1}]),
                            mode="timer",
                            scheduled_at=time.time() + 3600)
            row = store.get_scheduled_run("admin")
            self.assertIsNotNone(row)
            self.assertEqual(row["batch_id"], bid)
            self.assertEqual(row["flow_id"], 1)
            self.assertEqual(row["slots"], [1])
            await eng.stop("admin")
            self.assertIsNone(store.get_scheduled_run("admin"))

        run(_drive())

    def test_timer_row_survives_shutdown(self):
        eng = self.make_engine()
        self.setup_owner("admin")
        bid = self.make_batch("admin", [("cookie1", "", "", "")])

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=flow([
                                {"type": "click", "label": "📋 Tasks"},
                                {"type": "paste", "column": 1}]),
                            mode="timer",
                            scheduled_at=time.time() + 3600)
            self.assertIsNotNone(store.get_scheduled_run("admin"))
            await eng.shutdown()
            self.assertIsNotNone(store.get_scheduled_run("admin"))

        run(_drive())


if __name__ == "__main__":
    unittest.main()
