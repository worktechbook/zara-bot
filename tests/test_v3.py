"""v3 Phase 1 tests, migrated to the rebuilt (v2-parity) APIs.

Step format is now v2-style {"type": ...} with click steps; the store
is path-based; the engine is the full Engine class. Assertions keep
the original Phase 1 intent: no /start, Tasks-first, interval-only
timing, no rate-limit handling, no-timeout waits, distinct UI labels,
and a no-header sheet parser.
"""

import asyncio
import inspect
import pathlib
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from v3 import bot as v3bot
from v3 import config as v3config
from v3 import engine as v3engine
from v3 import store as v3store


def run(coro):
    return asyncio.run(coro)


class FakeTarget:
    def __init__(self, menu):
        self.menu = list(menu)
        self.sent = []
        self.waiter = None
        self.messages = [{
            "id": 1, "text": "menu", "buttons": list(menu),
            "incoming": True, "edit_date": None,
        }]
        self._next = 2

    async def send(self, text):
        self.sent.append(text)
        self.messages.insert(0, {
            "id": self._next, "text": f"reply to {text}",
            "buttons": list(self.menu), "incoming": True, "edit_date": None,
        })
        self._next += 1
        if self.waiter is not None:
            self.waiter.wake()

    async def history(self, limit=10):
        return list(self.messages[:limit])


class FakeClient:
    async def start(self):
        pass

    async def get_me(self):
        return type("Me", (), {"name": "Worker"})()

    async def disconnect(self):
        pass


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._orig = v3store.db_path()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        v3store.set_db_path(self._tmp.name)
        v3store.init_db()

    def tearDown(self):
        v3store.set_db_path(self._orig)
        pathlib.Path(self._tmp.name).unlink(missing_ok=True)


def task_flow():
    return {"id": 1, "name": "test", "target": "@Taskl1_bot", "interval": 0,
            "steps": [
                {"type": "click", "label": "📋 Tasks"},
                {"type": "click", "label": "🍪 ($0.0280)"},
                {"type": "click",
                 "label": "🌟2FA:Create FB (No mail) ($0.0280)"},
                {"type": "paste", "column": 1},
                {"type": "paste", "column": 2},
            ]}


class TestNoStart(StoreCase):
    def test_flow_with_start_is_rejected(self):
        bad = {"steps": [{"type": "send", "text": "/start"},
                         {"type": "click", "label": "📋 Tasks"},
                         {"type": "paste", "column": 1}]}
        with self.assertRaises(ValueError):
            v3engine.validate_flow(bad)
        with self.assertRaises(ValueError):
            v3bot.record_step("/start")

    def test_flow_starting_with_tasks_runs_and_never_sends_start(self):
        v3store.upsert_account("admin", 1, name="W", phone="+1",
                               status="active")
        bid, _, _ = v3store.create_batch(
            "admin", "test.xlsx", "shared", [("cookie1", "2fa1", "", "")],
            [1])
        holder = {"menu": ["📋 Tasks", "🍪 ($0.0280)",
                           "🌟2FA:Create FB (No mail) ($0.0280)"]}
        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: FakeClient(),
            target_factory=lambda c, u, w: FakeTarget(holder["menu"]),
        )

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            deadline = time.time() + 10
            while time.time() < deadline:
                if "admin" not in eng._runs:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual(v3store.get_progress(bid)["done"], 1)

        run(_drive())
        target = eng._targets[("admin", 1)]
        self.assertNotIn("/start", target.sent)
        self.assertEqual(target.sent[0], "📋 Tasks")

    def test_missing_pre_paste_option_stops_and_row_stays_pending(self):
        v3store.upsert_account("admin", 1, name="W", phone="+1",
                               status="active")
        bid, _, _ = v3store.create_batch(
            "admin", "test.xlsx", "shared", [("cookie1", "", "", "")], [1])
        holder = {"menu": ["📋 Tasks", "🍪 ($0.0300)"]}
        eng = v3engine.Engine(
            client_factory=lambda o, s, phone=None: FakeClient(),
            target_factory=lambda c, u, w: FakeTarget(holder["menu"]),
        )

        async def _drive():
            await eng.start(owner="admin", slots=[1], batch_id=bid,
                            flow=task_flow(), mode="now")
            deadline = time.time() + 10
            while time.time() < deadline:
                if "admin" not in eng._runs:
                    break
                await asyncio.sleep(0.05)

        run(_drive())
        progress = v3store.get_progress(bid)
        self.assertEqual((progress["pending"], progress["done"]), (1, 0))


class TestTimingAndWorkarounds(unittest.TestCase):
    def test_config_has_no_other_time_constant(self):
        names = [n for n in dir(v3config) if not n.startswith("_")]
        bad = [n for n in names
               if any(k in n.upper() for k in
                      ("TIMEOUT", "WAIT", "PAUSE", "TICK", "POLL", "MUTE", "RETRY"))]
        self.assertEqual(bad, [])
        self.assertIn("DEFAULT_FLOW_INTERVAL_SECONDS", names)

    def test_engine_has_no_rate_limit_handling(self):
        src = pathlib.Path(v3engine.__file__).read_text()
        for word in ("lood", "mute", "rotat"):
            self.assertNotIn(word, src.lower())

    def test_wait_reply_has_no_timeout_parameter(self):
        # Retargeted 2026-11-01: StreamWaiter.wait_reply was removed
        # as dead code; the live waiter is FlowSession.wait_reply.
        params = inspect.signature(
            v3engine.FlowSession.wait_reply).parameters
        self.assertNotIn("timeout", params)
        self.assertNotIn("seconds", params)

    def test_config_boots_on_bot_token_alone(self):
        old_token = v3config.BOT_TOKEN
        old_id, old_hash = v3config.API_ID, v3config.API_HASH
        try:
            v3config.BOT_TOKEN = "x"
            v3config.API_ID, v3config.API_HASH = None, ""
            self.assertEqual(v3config.validate(), [])
            v3config.BOT_TOKEN = ""
            self.assertEqual(v3config.validate(), ["missing V3_BOT_TOKEN"])
        finally:
            v3config.BOT_TOKEN = old_token
            v3config.API_ID, v3config.API_HASH = old_id, old_hash

    def test_engine_sleeps_only_for_interval_wait_timer(self):
        src = pathlib.Path(v3engine.__file__).read_text()
        self.assertNotIn("jitter", src.lower())
        self.assertNotIn("CLICK_PAUSE", src)
        # Wait steps, interval, timer, no-waiter polls, the
        # user-ordered 2026-11-01 reread wait, the user-ordered
        # 2026-11-01 inter-step pauses (run loop + check loop), the
        # user-ordered 2026-11-01 check waits (watching + failed),
        # the user-ordered 2026-11-01 probe-turn wait, the
        # user-ordered 2026-11-01 retry backoff, the
        # user-ordered 2026-11-01 guard settle, and the
        # user-ordered 2026-11-09 stop-retry wait.
        self.assertLessEqual(src.count("asyncio.sleep"), 14)


class TestUiCollision(unittest.TestCase):
    def test_ui_labels_do_not_collide_with_target_labels(self):
        for label in v3bot.UI_LABELS.values():
            self.assertNotEqual(label, "▶️ Start")
            self.assertNotIn(label, v3bot.FORBIDDEN_UI_LABELS)

    def test_flow_creation_records_start_button_as_a_step(self):
        step = v3bot.record_step("▶️ Start")
        self.assertEqual(step, {"type": "send", "text": "▶️ Start"})


class TestSheetAndStore(StoreCase):
    def test_store_round_trip(self):
        fid = v3store.save_flow(
            "user:1", "f", "@Taskl1_bot", 27,
            [{"type": "click", "label": "📋 Tasks"}])
        flow = v3store.get_flow("user:1", fid)
        self.assertEqual(flow["steps"][0]["label"], "📋 Tasks")
        self.assertEqual(flow["interval"], 27)


if __name__ == "__main__":
    unittest.main()
