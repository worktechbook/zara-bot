"""v3 handler tests: spec sections 2.5, 2.6, 2.7.

Pure-function, build-smoke, and fake-update handler tests — no
Telegram network. PTB dispatch itself is exercised by construction
(build_application) and by the conversation fallback inspection below.
"""

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from v3 import billing as v3billing
from v3 import bot as v3bot
from v3 import config as v3config
from v3 import control_bot
from v3 import engine as v3engine
from v3 import store


class HandlerCase(unittest.TestCase):
    def setUp(self):
        self._orig = store.db_path()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        store.set_db_path(self._tmp.name)
        store.init_db()
        control_bot.ACTIVE_BATCH.clear()

    def tearDown(self):
        store.set_db_path(self._orig)
        pathlib.Path(self._tmp.name).unlink(missing_ok=True)
        control_bot.ACTIVE_BATCH.clear()


class TestFlowBlock(HandlerCase):
    def test_valid_block(self):
        draft = control_bot.parse_flow_block(
            "Flow: My Flow\nTarget: @ExampleBot\nInterval: 30 seconds\n"
            "1. Click: 📋 Tasks\n2. Click: 🍪 ($0.0280)\n"
            "3. Paste Worker Name\n4. Paste Worker Name 2\n"
            "5. Wait: two seconds\n")
        self.assertEqual(draft["name"], "My Flow")
        self.assertEqual(draft["target"], "@ExampleBot")
        self.assertEqual(draft["interval"], 30)
        self.assertEqual(draft["steps"][0],
                         {"type": "click", "label": "📋 Tasks"})
        self.assertEqual(draft["steps"][2],
                         {"type": "paste", "column": 1})
        self.assertEqual(draft["steps"][3],
                         {"type": "paste", "column": 2})
        self.assertEqual(draft["steps"][4],
                         {"type": "wait", "seconds": 2})

    def test_start_step_rejected_never_inserted(self):
        with self.assertRaises(ValueError):
            control_bot.parse_flow_block(
                "Flow: F\nTarget: @B\nInterval: 27\n"
                "1. Send: /start\n2. Click: 📋 Tasks\n"
                "3. Paste Worker Name\n")
        draft = control_bot.parse_flow_block(
            "Flow: F\nTarget: @B\nInterval: 27\n"
            "1. Click: 📋 Tasks\n2. Paste Worker Name\n")
        self.assertNotEqual(draft["steps"][0],
                            {"type": "send", "text": "/start"})

    def test_rejections(self):
        base = "Flow: F\nTarget: @B\nInterval: 27\n"
        with self.assertRaises(ValueError):
            control_bot.parse_flow_block(
                base + "1. Click: 📋 Tasks\n")  # no paste
        with self.assertRaises(ValueError):
            control_bot.parse_flow_block(
                "Flow: F\nTarget: @B\nInterval: 0\n"
                "1. Paste Worker Name\n")
        with self.assertRaises(ValueError):
            control_bot.parse_flow_block(
                base + "1. Teleport: nowhere\n2. Paste Worker Name\n")
        with self.assertRaises(ValueError):
            control_bot.parse_flow_block(
                "Target: @B\nInterval: 27\n1. Paste Worker Name\n")
        with self.assertRaises(ValueError):
            control_bot.parse_flow_block(
                base + "1. Wait: someday\n2. Paste Worker Name\n")


class TestFormatAndWait(unittest.TestCase):
    def test_format_flow(self):
        text = v3bot.format_flow({
            "name": "F", "target": "@B", "interval": 27,
            "steps": [{"type": "click", "label": "📋 Tasks"},
                      {"type": "paste", "column": 2},
                      {"type": "wait", "seconds": 3},
                      {"type": "send", "text": "hi"}]})
        self.assertIn("1. Click: 📋 Tasks", text)
        self.assertIn("2. Paste Worker Name 2", text)
        self.assertIn("3. Wait: 3 seconds", text)
        self.assertIn("4. Send: hi", text)

    def test_parse_wait_seconds(self):
        self.assertEqual(v3bot.parse_wait_seconds("5"), 5)
        self.assertEqual(v3bot.parse_wait_seconds("five seconds"), 5)
        self.assertEqual(v3bot.parse_wait_seconds("twenty"), 20)
        self.assertIsNone(v3bot.parse_wait_seconds("someday"))

    def test_ui_labels_safe(self):
        for label in v3bot.UI_LABELS.values():
            self.assertNotIn(label, v3bot.FORBIDDEN_UI_LABELS)
        self.assertEqual(v3bot.UI_LABELS["run"], "🚀 Run batch")


class TestInstructions(unittest.TestCase):
    def test_nine_pages_and_hard_rules(self):
        pages = control_bot.PAGES
        self.assertEqual(len(pages), 9)
        joined = "\n".join(pages)
        self.assertNotIn("Step 1 is Send /start", joined)
        self.assertNotIn("Send: /start", joined)
        self.assertNotIn("every 60 seconds", joined)
        self.assertIn("📋 Tasks", joined)
        self.assertIn("৳0.50", joined)


class TestGateAndBoard(HandlerCase):
    def test_board_allowed_by_balance_bypass_admin(self):
        orig_admin = set(v3config.ADMIN_IDS)
        orig_bypass = set(v3config.BILLING_BYPASS_IDS)
        try:
            v3config.ADMIN_IDS = {123}
            v3config.BILLING_BYPASS_IDS = {456}
            self.assertTrue(control_bot._board_allowed("admin"))
            self.assertTrue(control_bot._board_allowed("user:456"))
            self.assertFalse(control_bot._board_allowed("user:789"))
            store.adjust_balance("user:789", 100, "topup")
            self.assertTrue(control_bot._board_allowed("user:789"))
            from types import SimpleNamespace
            upd = SimpleNamespace(
                effective_user=SimpleNamespace(id=123))
            self.assertEqual(control_bot.owner_of(upd), "admin")
            upd2 = SimpleNamespace(
                effective_user=SimpleNamespace(id=789))
            self.assertEqual(control_bot.owner_of(upd2), "user:789")
        finally:
            v3config.ADMIN_IDS = orig_admin
            v3config.BILLING_BYPASS_IDS = orig_bypass

    def test_resolve_active_batch_cache_pointer_backfill(self):
        bid, _, _ = store.create_batch(
            "admin", "a.xlsx", "shared",
            [("a", "", "", "")], [1])
        self.assertEqual(
            control_bot.resolve_active_batch("admin", 111), bid)
        bid2, _, _ = store.create_batch(
            "admin", "b.xlsx", "shared",
            [("b", "", "", "")], [1])
        store.set_active_batch("admin", bid2)
        # A different user has no cache entry, so the pointer wins.
        self.assertEqual(
            control_bot.resolve_active_batch("admin", 222), bid2)

    def test_edit_conversation_has_repress_fallback(self):
        fallbacks = control_bot.EDIT_CONVERSATION.fallbacks
        patterns = [str(getattr(h, "pattern", "")) for h in fallbacks]
        self.assertTrue(any("saved:edit" in p for p in patterns),
                        f"fallbacks lack saved:edit: {patterns}")

    def test_build_application_smoke(self):
        saved_env = {}
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
            if key in os.environ:
                saved_env[key] = os.environ.pop(key)
        try:
            app = control_bot.build_application(
                "123456:" + "ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                db_path=self._tmp.name, engine=v3engine.Engine())
            total = sum(len(v) for v in app.handlers.values())
            self.assertGreaterEqual(total, 20)
        finally:
            os.environ.update(saved_env)


class TestBillingPolicy(unittest.TestCase):
    def test_kind_labels(self):
        self.assertEqual(v3billing.kind_label("topup"), "Top-up")
        self.assertEqual(v3billing.kind_label("deduct"), "ID submitted")
        self.assertEqual(v3billing.kind_label("adjust"), "Correction")
        self.assertEqual(v3billing.kind_label("reserve"), "Reserved")
        self.assertEqual(v3billing.kind_label("refund"), "Refunded")
        self.assertEqual(v3billing.kind_label("mystery"), "mystery")

    def test_billable(self):
        old = v3config.BILLING_BYPASS_IDS
        v3config.BILLING_BYPASS_IDS = {100001}
        try:
            self.assertFalse(v3billing.billable("admin"))
            self.assertFalse(v3billing.billable("user:100001"))
            self.assertTrue(v3billing.billable("user:999999"))
            self.assertTrue(v3billing.billable("user:abc"))
            self.assertTrue(v3billing.billable("someone"))
        finally:
            v3config.BILLING_BYPASS_IDS = old

    def test_format_bdt(self):
        self.assertEqual(v3billing.format_bdt(0), "৳0.00")
        self.assertEqual(v3billing.format_bdt(50), "৳0.50")
        self.assertEqual(v3billing.format_bdt(125), "৳1.25")
        self.assertEqual(v3billing.format_bdt(-50), "-৳0.50")

    def test_ids_left(self):
        self.assertEqual(v3config.COST_PER_ROW_POISHA, 50)
        self.assertEqual(v3billing.ids_left(0), 0)
        self.assertEqual(v3billing.ids_left(49), 0)
        self.assertEqual(v3billing.ids_left(50), 1)
        self.assertEqual(v3billing.ids_left(125), 2)

    def test_topup_instructions(self):
        text = v3billing.topup_instructions(999999)
        self.assertIn(v3billing.BKASH_NAGAD_NUMBER, text)
        self.assertIn(v3billing.BINANCE_PAY_ID, text)
        self.assertIn("999999", text)
        self.assertIn("Minimum top-up", text)

    def test_buy_subscription_text(self):
        text = v3billing.buy_subscription_text(999999)
        self.assertIn("0.50", text)
        self.assertIn(v3billing.BKASH_NAGAD_NUMBER, text)
        self.assertIn(v3billing.BINANCE_PAY_ID, text)
        self.assertIn("999999", text)

    def test_gate_text(self):
        text = v3billing.gate_text()
        self.assertIn("subscription", text)
        self.assertIn("0.50", text)


class TestBotSurface(unittest.TestCase):
    def test_back_button(self):
        markup = control_bot.back_button("x:y")
        self.assertEqual(len(markup), 1)
        self.assertEqual(markup[0][0].text, "⬅️ Back")
        self.assertEqual(markup[0][0].callback_data, "x:y")

    def test_gate_markup(self):
        markup = control_bot.gate_markup()
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0].callback_data, control_bot.CB_HOW)
        self.assertEqual(buttons[1].callback_data, control_bot.CB_BUY)
        self.assertEqual(buttons[2].url, control_bot.SUPPORT_URL)

    def test_gate_back_markup(self):
        markup = control_bot._gate_back_markup()
        buttons = [b for row in markup.inline_keyboard for b in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0].callback_data, control_bot.CB_BACK)

    def test_conversations_have_start_fallback(self):
        for conv in (control_bot._login_conversation(),
                     control_bot._stepbystep_conversation(),
                     control_bot._timer_conversation()):
            self.assertTrue(
                any("start" in getattr(h, "commands", ())
                    for h in conv.fallbacks),
                f"{conv.name} lacks a /start fallback")


class _FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.markups = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class _FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class _FakeCallbackQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answered = False

    async def answer(self, **kwargs):
        self.answered = True


class _FakeUpdate:
    def __init__(self, user, message, callback_query=None):
        self.effective_user = user
        self.effective_message = message
        self.callback_query = callback_query


class _FakeContext:
    def __init__(self, engine):
        self.user_data = {}
        self.bot_data = {"engine": engine}


class _FakeEngine:
    def __init__(self):
        self.starts = []
        self.status_dict = {}
        self.start_result = (0, 0)

    async def start(self, **kwargs):
        self.starts.append(kwargs)
        return self.start_result

    def status(self, owner):
        return dict(self.status_dict)


@contextmanager
def _admin_ids(ids):
    old = getattr(v3config, "ADMIN_IDS", set())
    v3config.ADMIN_IDS = set(ids)
    try:
        yield
    finally:
        v3config.ADMIN_IDS = old


class TestTimer(HandlerCase):
    def setUp(self):
        super().setUp()
        self.board_shown = []

        async def _fake_show(update, context):
            self.board_shown.append((update, context))

        self._patch = mock.patch.object(control_bot, "show_board",
                                        new=_fake_show)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        super().tearDown()

    def _pending(self, owner):
        fid = store.save_flow(
            owner, "F", "@Bot", 30,
            [{"type": "click", "label": "📋 Tasks"},
             {"type": "paste", "column": 1}])
        return {"flow_id": fid, "slots": [1], "batch_id": 7}

    def test_future_schedules(self):
        update = _FakeUpdate(_FakeUser(999999),
                             _FakeMessage("2999-01-02 03:04"))
        owner = control_bot.owner_of(update)
        engine = _FakeEngine()
        context = _FakeContext(engine)
        context.user_data["pending_run"] = self._pending(owner)
        result = asyncio.run(control_bot.timer_datetime(update, context))
        self.assertEqual(result, control_bot.ConversationHandler.END)
        self.assertEqual(len(engine.starts), 1)
        self.assertEqual(engine.starts[0]["mode"], "timer")
        expected = int(datetime(2999, 1, 2, 3, 4,
                                tzinfo=ZoneInfo(v3config.TIMEZONE))
                       .timestamp())
        self.assertEqual(engine.starts[0]["scheduled_at"], expected)
        self.assertTrue(any("Scheduled" in r
                            for r in update.effective_message.replies))
        self.assertEqual(len(self.board_shown), 1)
        self.assertNotIn("pending_run", context.user_data)

    def test_past_rejected(self):
        update = _FakeUpdate(_FakeUser(999999),
                             _FakeMessage("2000-01-02 03:04"))
        owner = control_bot.owner_of(update)
        engine = _FakeEngine()
        context = _FakeContext(engine)
        context.user_data["pending_run"] = self._pending(owner)
        result = asyncio.run(control_bot.timer_datetime(update, context))
        self.assertEqual(result, control_bot.TIMER_DATETIME)
        self.assertEqual(len(engine.starts), 0)
        self.assertTrue(any("in the past" in r
                            for r in update.effective_message.replies))
        self.assertIn("pending_run", context.user_data)

    def test_bad_format_rejected(self):
        update = _FakeUpdate(_FakeUser(999999), _FakeMessage("tomorrow"))
        owner = control_bot.owner_of(update)
        engine = _FakeEngine()
        context = _FakeContext(engine)
        context.user_data["pending_run"] = self._pending(owner)
        result = asyncio.run(control_bot.timer_datetime(update, context))
        self.assertEqual(result, control_bot.TIMER_DATETIME)
        self.assertEqual(len(engine.starts), 0)
        self.assertTrue(any("Use the format" in r
                            for r in update.effective_message.replies))

    def test_run_timer_callback_does_not_cancel_pending(self):
        # A second Timer tap lands in _on_run_cb when the timer
        # conversation is already active; it must not discard the
        # pending run (seen live 2026-11-09).
        owner = control_bot.owner_of(
            _FakeUpdate(_FakeUser(999999), _FakeMessage("")))
        context = _FakeContext(_FakeEngine())
        context.user_data["pending_run"] = self._pending(owner)
        message = _FakeMessage("")
        query = _FakeCallbackQuery("run:timer", message)
        cb = _FakeUpdate(_FakeUser(999999), message, query)
        asyncio.run(control_bot._on_run_cb(cb, context))
        self.assertIn("pending_run", context.user_data)
        self.assertEqual(len(self.board_shown), 0)


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


class _FakeApp:
    def __init__(self, engine):
        self.bot_data = {"engine": engine}
        self.bot = _FakeBot()


class TestRestoreScheduledRuns(HandlerCase):
    def _owner(self, user_id=999999):
        with _admin_ids(set()):
            update = _FakeUpdate(_FakeUser(user_id), _FakeMessage(""))
            return control_bot.owner_of(update)

    def _flow_and_batch(self, owner):
        fid = store.save_flow(
            owner, "F", "@Bot", 30,
            [{"type": "click", "label": "📋 Tasks"},
             {"type": "paste", "column": 1}])
        bid, _, _ = store.create_batch(
            owner, "f.xlsx", "slot", [("a1", "", "", "")], [1])
        return fid, bid

    def test_restores_valid_row(self):
        owner = self._owner()
        fid, bid = self._flow_and_batch(owner)
        future = int(datetime(2999, 1, 2, 3, 4,
                              tzinfo=ZoneInfo(v3config.TIMEZONE))
                     .timestamp())
        store.save_scheduled_run(owner, bid, fid, [1], future)
        engine = _FakeEngine()
        app = _FakeApp(engine)
        asyncio.run(control_bot.restore_scheduled_runs(app))
        self.assertEqual(len(engine.starts), 1)
        self.assertEqual(engine.starts[0]["mode"], "timer")
        self.assertEqual(engine.starts[0]["batch_id"], bid)
        self.assertEqual(engine.starts[0]["scheduled_at"], future)
        self.assertEqual(app.bot.sent, [])

    def test_drops_row_with_missing_flow(self):
        owner = self._owner()
        _, bid = self._flow_and_batch(owner)
        future = int(datetime(2999, 1, 2, 3, 4,
                              tzinfo=ZoneInfo(v3config.TIMEZONE))
                     .timestamp())
        store.save_scheduled_run(owner, bid, 12345, [1], future)
        engine = _FakeEngine()
        app = _FakeApp(engine)
        asyncio.run(control_bot.restore_scheduled_runs(app))
        self.assertEqual(engine.starts, [])
        self.assertIsNone(store.get_scheduled_run(owner))
        self.assertTrue(any("dropped" in t for _, t in app.bot.sent))

    def test_past_schedule_starts_late_with_notice(self):
        owner = self._owner()
        fid, bid = self._flow_and_batch(owner)
        past = int(datetime(2000, 1, 2, 3, 4,
                            tzinfo=ZoneInfo(v3config.TIMEZONE))
                   .timestamp())
        store.save_scheduled_run(owner, bid, fid, [1], past)
        engine = _FakeEngine()
        app = _FakeApp(engine)
        asyncio.run(control_bot.restore_scheduled_runs(app))
        self.assertEqual(len(engine.starts), 1)
        self.assertTrue(any("started late" in t for _, t in app.bot.sent))


class TestCreditAndStatus(HandlerCase):
    def _update(self, user_id, text):
        return _FakeUpdate(_FakeUser(user_id), _FakeMessage(text))

    def test_credit_command_not_registered(self):
        # /credit was removed 2026-11-11 (temporary, the operator's order) so no
        # new paid user can be added; /credits and /ledger stay. Restoring
        # /credit must delete this test and restore the four /credit tests
        # — see credit-command-removed-2026-11-11.md.
        saved_env = {}
        for key in ("http_proxy", "https_proxy", "HTTP_PROXY",
                    "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
            if key in os.environ:
                saved_env[key] = os.environ.pop(key)
        try:
            app = control_bot.build_application(
                "123456:" + "ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
                db_path=self._tmp.name, engine=v3engine.Engine())
            commands = set()
            for handlers in app.handlers.values():
                for h in handlers:
                    commands.update(getattr(h, "commands", set()) or set())
            self.assertNotIn("credit", commands)
            self.assertIn("credits", commands)
            self.assertIn("ledger", commands)
        finally:
            os.environ.update(saved_env)

    def test_credits_empty(self):
        with _admin_ids({999999}):
            update = self._update(999999, "/credits")
            asyncio.run(control_bot.cmd_credits(update, _FakeContext(None)))
        self.assertTrue(any("No wallets yet." in r
                            for r in update.effective_message.replies))

    def test_credits_lists_emptiest_first(self):
        store.adjust_balance("user:111", 5000, "topup")
        store.adjust_balance("user:222", 1000, "topup")
        with _admin_ids({999999}):
            update = self._update(999999, "/credits")
            asyncio.run(control_bot.cmd_credits(update, _FakeContext(None)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("Wallets (emptiest first)", text)
        self.assertLess(text.index("222:"), text.index("111:"))

    def test_credits_single_user(self):
        store.adjust_balance("user:771234567", 2500, "topup")
        with _admin_ids({999999}):
            update = self._update(999999, "/credits 771234567")
            asyncio.run(control_bot.cmd_credits(update, _FakeContext(None)))
        self.assertTrue(any(
            "User 771234567: ৳25.00 (~50 IDs left)." in r
            for r in update.effective_message.replies))

    def test_ledger_entries(self):
        store.adjust_balance("user:771234567", 12000, "topup")
        store.adjust_balance("user:771234567", -50, "deduct")
        with _admin_ids({999999}):
            update = self._update(999999, "/ledger 771234567")
            asyncio.run(control_bot.cmd_ledger(update, _FakeContext(None)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("Ledger — 771234567", text)
        self.assertIn("Top-up", text)
        self.assertIn("ID submitted", text)
        self.assertIn("120.00", text)
        self.assertIn("0.50", text)

    def test_ledger_empty(self):
        with _admin_ids({999999}):
            update = self._update(999999, "/ledger 771234567")
            asyncio.run(control_bot.cmd_ledger(update, _FakeContext(None)))
        self.assertTrue(any("No ledger entries for 771234567." in r
                            for r in update.effective_message.replies))

    def test_ledger_bad_args(self):
        with _admin_ids({999999}):
            update = self._update(999999, "/ledger")
            asyncio.run(control_bot.cmd_ledger(update, _FakeContext(None)))
        self.assertTrue(any("Usage: /ledger" in r
                            for r in update.effective_message.replies))

    def test_balance_plain_user(self):
        store.adjust_balance("user:888888", 2500, "topup")
        with _admin_ids(set()):
            update = self._update(888888, "/balance")
            asyncio.run(control_bot.show_balance(update, _FakeContext(None)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("৳25.00 (~50 IDs left)", text)
        self.assertIn("Your Telegram ID: 888888", text)

    def test_balance_admin_unlimited(self):
        with _admin_ids({999999}):
            update = self._update(999999, "/balance")
            asyncio.run(control_bot.show_balance(update, _FakeContext(None)))
        self.assertTrue(any("Admin account — unlimited" in r
                            for r in update.effective_message.replies))

    def test_status_running(self):
        with _admin_ids(set()):
            update = self._update(888888, "/status")
            engine = _FakeEngine()
            engine.status_dict = {"active": True, "mode": "now"}
            asyncio.run(control_bot.show_status(
                update, _FakeContext(engine)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("DASHBOARD", text)
        self.assertIn("Worker: Running", text)

    def test_status_no_engine_no_batch(self):
        with _admin_ids(set()):
            update = self._update(888888, "/status")
            asyncio.run(control_bot.show_status(
                update, _FakeContext(None)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("DASHBOARD", text)
        self.assertIn("No active batch", text)

    def test_status_shows_running_batch_not_active_pointer(self):
        with _admin_ids(set()):
            update = self._update(888888, "/status")
            owner = control_bot.owner_of(update)
            store.save_flow(owner, "F", "@Bot", 30,
                            [{"type": "click", "label": "📋 Tasks"},
                             {"type": "paste", "column": 1}])
            running_bid, _, _ = store.create_batch(
                owner, "run.xlsx", "slot",
                [("a1", "", "", ""), ("a2", "", "", ""),
                 ("a3", "", "", "")], [1])
            pointer_bid, _, _ = store.create_batch(
                owner, "ptr.xlsx", "slot", [("b1", "", "", "")], [1])
            store.set_active_batch(owner, pointer_bid)
            engine = _FakeEngine()
            engine.status_dict = {"active": True, "mode": "now",
                                  "batch_id": running_bid,
                                  "flow_name": "F"}
            asyncio.run(control_bot.show_status(
                update, _FakeContext(engine)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("Total: 3", text)
        self.assertNotIn("Total: 1", text)

    def test_status_shows_persisted_schedule(self):
        with _admin_ids(set()):
            update = self._update(888888, "/status")
            owner = control_bot.owner_of(update)
            fid = store.save_flow(owner, "F", "@Bot", 30,
                                  [{"type": "click", "label": "📋 Tasks"},
                                   {"type": "paste", "column": 1}])
            bid, _, _ = store.create_batch(
                owner, "f.xlsx", "slot", [("a1", "", "", "")], [1])
            future = int(datetime(2999, 1, 2, 3, 4,
                                  tzinfo=ZoneInfo(v3config.TIMEZONE))
                         .timestamp())
            store.save_scheduled_run(owner, bid, fid, [1], future)
            engine = _FakeEngine()
            engine.status_dict = {"active": False, "mode": None,
                                  "batch_id": None, "flow_name": None,
                                  "scheduled_at": None, "check": None,
                                  "stop_reason": None}
            asyncio.run(control_bot.show_status(
                update, _FakeContext(engine)))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("Worker: Stopped", text)
        self.assertIn("🕒 Scheduled for 2999-01-02 03:04", text)
        self.assertIn("flow 'F'", text)

    def test_status_engine_error_shows_warning(self):
        class _BrokenEngine:
            def status(self, owner):
                raise RuntimeError("boom")

        with _admin_ids(set()):
            update = self._update(888888, "/status")
            asyncio.run(control_bot.show_status(
                update, _FakeContext(_BrokenEngine())))
        text = "\n".join(update.effective_message.replies)
        self.assertIn("Worker: Stopped", text)
        self.assertIn("Could not read the live worker state", text)


class TestEditAndRun(HandlerCase):
    def setUp(self):
        super().setUp()
        self.board_shown = []

        async def _fake_show(update, context):
            self.board_shown.append((update, context))

        self._patch = mock.patch.object(control_bot, "show_board",
                                        new=_fake_show)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        super().tearDown()

    def _owner_update(self, user_id=999999):
        with _admin_ids(set()):
            update = _FakeUpdate(_FakeUser(user_id), _FakeMessage(""))
            owner = control_bot.owner_of(update)
        return owner, update

    def _flow(self, owner, steps=None):
        if steps is None:
            steps = [{"type": "click", "label": "📋 Tasks"},
                     {"type": "paste", "column": 1}]
        return store.save_flow(owner, "F", "@Bot", 30, steps)

    def _cb_update(self, user_id, data):
        message = _FakeMessage("")
        query = _FakeCallbackQuery(data, message)
        return _FakeUpdate(_FakeUser(user_id), message, query)

    def test_start_prompt_success(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner)
            store.set_active_flow(owner, fid)
            store.upsert_account(owner, 1, name="W1")
            bid, _, _ = store.create_batch(
                owner, "f.xlsx", "slot", [("a1", "", "", "")], [1])
            control_bot.ACTIVE_BATCH[999999] = bid
            context = _FakeContext(_FakeEngine())
            asyncio.run(control_bot.show_start_prompt(update, context))
        pending = context.user_data.get("pending_run")
        self.assertIsNotNone(pending)
        self.assertEqual(pending["flow_id"], fid)
        self.assertEqual(pending["batch_id"], bid)
        self.assertEqual([int(s) for s in pending["slots"]], [1])
        self.assertTrue(any("Run 1 worker(s)" in r
                            for r in update.effective_message.replies))
        self.assertTrue(any("Flow: F → @Bot (30s)" in r
                            for r in update.effective_message.replies))
        self.assertTrue(any("Account 1 (W1)" in r
                            for r in update.effective_message.replies))
        markup = update.effective_message.markups[0]
        datas = {b.callback_data for row in markup.inline_keyboard
                 for b in row}
        self.assertEqual(datas, {"run:check", "run:now", "run:timer",
                                 "run:cancel"})

    def test_start_prompt_missing_flow(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            asyncio.run(control_bot.show_start_prompt(
                update, _FakeContext(_FakeEngine())))
        self.assertTrue(any("No active flow" in r
                            for r in update.effective_message.replies))

    def test_run_cb_now(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner)
            bid, _, _ = store.create_batch(
                owner, "f.xlsx", "slot", [("a1", "", "", "")], [1])
            engine = _FakeEngine()
            engine.start_result = ([], 0)
            context = _FakeContext(engine)
            context.user_data["pending_run"] = {
                "flow_id": fid, "batch_id": bid, "slots": [1]}
            cb = self._cb_update(999999, "run:now")
            asyncio.run(control_bot._on_run_cb(cb, context))
        self.assertTrue(any("Started 1 worker(s) with flow 'F'." in r
                            for r in cb.callback_query.message.replies))
        self.assertEqual(len(engine.starts), 1)
        self.assertEqual(engine.starts[0]["mode"], "now")
        self.assertNotIn("pending_run", context.user_data)
        self.assertEqual(len(self.board_shown), 1)

    def test_run_cb_cancel(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner)
            engine = _FakeEngine()
            context = _FakeContext(engine)
            context.user_data["pending_run"] = {
                "flow_id": fid, "batch_id": 7, "slots": [1]}
            cb = self._cb_update(999999, "run:cancel")
            asyncio.run(control_bot._on_run_cb(cb, context))
        self.assertTrue(any("Start cancelled." in r
                            for r in cb.callback_query.message.replies))
        self.assertEqual(len(engine.starts), 0)
        self.assertNotIn("pending_run", context.user_data)
        self.assertEqual(len(self.board_shown), 1)

    def test_run_cb_no_pending(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            engine = _FakeEngine()
            context = _FakeContext(engine)
            cb = self._cb_update(999999, "run:now")
            asyncio.run(control_bot._on_run_cb(cb, context))
        self.assertTrue(any("Nothing to start" in r
                            for r in cb.callback_query.message.replies))
        self.assertEqual(len(engine.starts), 0)

    def test_edit_entry(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner)
            context = _FakeContext(_FakeEngine())
            cb = self._cb_update(999999, f"saved:edit:{fid}")
            result = asyncio.run(control_bot._edit_entry(cb, context))
        self.assertEqual(result, control_bot.EDIT_PICK)
        self.assertEqual(context.user_data["edit_flow_id"], fid)
        self.assertEqual(len(context.user_data["edit_steps"]), 2)
        self.assertTrue(any(f"Editing: F (ID: {fid})" in r
                            for r in cb.callback_query.message.replies))
        markup = cb.callback_query.message.markups[0]
        datas = [b.callback_data for row in markup.inline_keyboard
                 for b in row]
        self.assertEqual(datas, ["sedit:0", "sedit:1", "sedit:cancel"])

    def test_edit_entry_missing_flow(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            context = _FakeContext(_FakeEngine())
            cb = self._cb_update(999999, "saved:edit:999")
            result = asyncio.run(control_bot._edit_entry(cb, context))
        self.assertEqual(result, control_bot.ConversationHandler.END)
        self.assertTrue(any("Flow not found." in r
                            for r in cb.callback_query.message.replies))

    def test_edit_pick_click(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_steps"] = [
                {"type": "click", "label": "📋 Tasks"}]
            cb = self._cb_update(999999, "sedit:0")
            result = asyncio.run(control_bot._edit_pick(cb, context))
        self.assertEqual(result, control_bot.EDIT_VALUE)
        self.assertEqual(context.user_data["edit_idx"], 0)
        self.assertTrue(any("Step 1:" in r and "new button label" in r
                            for r in cb.callback_query.message.replies))

    def test_edit_pick_cancel(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_flow_id"] = 3
            context.user_data["edit_steps"] = [
                {"type": "click", "label": "📋 Tasks"}]
            cb = self._cb_update(999999, "sedit:cancel")
            result = asyncio.run(control_bot._edit_pick(cb, context))
        self.assertEqual(result, control_bot.ConversationHandler.END)
        self.assertNotIn("edit_flow_id", context.user_data)
        self.assertTrue(any("Edit cancelled." in r
                            for r in cb.callback_query.message.replies))
        self.assertEqual(len(self.board_shown), 1)

    def test_edit_pick_out_of_range(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_steps"] = [
                {"type": "click", "label": "📋 Tasks"}]
            cb = self._cb_update(999999, "sedit:9")
            result = asyncio.run(control_bot._edit_pick(cb, context))
        self.assertEqual(result, control_bot.EDIT_PICK)
        self.assertTrue(any("That step no longer exists." in r
                            for r in cb.callback_query.message.replies))

    def test_edit_value_click(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner)
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_flow_id"] = fid
            context.user_data["edit_idx"] = 0
            context.user_data["edit_steps"] = [
                {"type": "click", "label": "📋 Tasks"},
                {"type": "paste", "column": 1}]
            update.effective_message.text = "New Label"
            result = asyncio.run(control_bot._edit_value(update, context))
        self.assertEqual(result, control_bot.ConversationHandler.END)
        flow = store.get_flow(owner, fid)
        self.assertEqual(flow["steps"][0]["label"], "New Label")
        self.assertTrue(any("Step 1 updated in F" in r
                            for r in update.effective_message.replies))
        self.assertNotIn("edit_flow_id", context.user_data)
        self.assertEqual(len(self.board_shown), 1)

    def test_edit_value_send_rejects_start(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner, [{"type": "send", "text": "hi"}])
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_flow_id"] = fid
            context.user_data["edit_idx"] = 0
            context.user_data["edit_steps"] = [{"type": "send",
                                                "text": "hi"}]
            update.effective_message.text = "/start"
            result = asyncio.run(control_bot._edit_value(update, context))
        self.assertEqual(result, control_bot.EDIT_VALUE)
        flow = store.get_flow(owner, fid)
        self.assertEqual(flow["steps"][0]["text"], "hi")
        self.assertTrue(any("do not include /start" in r
                            for r in update.effective_message.replies))

    def test_edit_value_paste_column(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner)
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_flow_id"] = fid
            context.user_data["edit_idx"] = 1
            context.user_data["edit_steps"] = [
                {"type": "click", "label": "📋 Tasks"},
                {"type": "paste", "column": 1}]
            update.effective_message.text = "B"
            result = asyncio.run(control_bot._edit_value(update, context))
        self.assertEqual(result, control_bot.ConversationHandler.END)
        flow = store.get_flow(owner, fid)
        self.assertEqual(flow["steps"][1]["column"], 2)

    def test_edit_value_wait(self):
        with _admin_ids(set()):
            owner, update = self._owner_update()
            fid = self._flow(owner, [{"type": "wait", "seconds": 3}])
            context = _FakeContext(_FakeEngine())
            context.user_data["edit_flow_id"] = fid
            context.user_data["edit_idx"] = 0
            context.user_data["edit_steps"] = [{"type": "wait",
                                                "seconds": 3}]
            update.effective_message.text = "5"
            result = asyncio.run(control_bot._edit_value(update, context))
        self.assertEqual(result, control_bot.ConversationHandler.END)
        flow = store.get_flow(owner, fid)
        self.assertEqual(flow["steps"][0]["seconds"], 5)


if __name__ == "__main__":
    unittest.main()
