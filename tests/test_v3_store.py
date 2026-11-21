"""v3 store tests: full spec section 2.3 behaviour.

Path-based store module (no connection objects). Each test uses its
own temp DB file and restores the previous path afterwards.
"""

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from v3 import store


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._orig = store.db_path()
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        store.set_db_path(self._tmp.name)
        store.init_db()

    def tearDown(self):
        store.set_db_path(self._orig)
        pathlib.Path(self._tmp.name).unlink(missing_ok=True)


def rows(*cols_a):
    return [(a, "b-" + a, "c-" + a, "d-" + a) for a in cols_a]


class TestDefaults(StoreCase):
    def test_default_flows_seed_once_with_no_start_step(self):
        store.ensure_default_flows("admin")
        store.ensure_default_flows("admin")
        flows = store.list_flows("admin")
        self.assertEqual(len(flows), 2)
        names = {f["name"] for f in flows}
        self.assertEqual(names, {"COOKIES NO MAIL", "2FA COOKIES"})
        for f in flows:
            self.assertEqual(f["target"], "@Taskl1_bot")
            self.assertEqual(f["interval"], 27)
            for step in f["steps"]:
                self.assertFalse(
                    step.get("type") == "send"
                    and (step.get("text") or "").strip() == "/start")
            self.assertEqual(f["steps"][0],
                             {"type": "click", "label": "📋 Tasks"})
        active = store.get_active_flow("admin")
        self.assertEqual(active["name"], "COOKIES NO MAIL")

    def test_flow_crud_and_same_id_edit(self):
        fid = store.save_flow("user:1", "F", "@Bot", 30,
                              [{"type": "click", "label": "📋 Tasks"},
                               {"type": "paste", "column": 1}])
        flow = store.get_flow("user:1", fid)
        self.assertEqual(flow["interval"], 30)
        self.assertTrue(store.update_flow_steps(
            "user:1", fid,
            [{"type": "click", "label": "📋 Tasks"},
             {"type": "paste", "column": 2}]))
        self.assertEqual(store.get_flow("user:1", fid)["steps"][1]["column"], 2)
        self.assertFalse(store.update_flow_steps("user:1", 99999, []))
        store.set_active_flow("user:1", fid)
        self.assertEqual(store.get_active_flow("user:1")["id"], fid)
        store.delete_flow("user:1", fid)
        self.assertIsNone(store.get_flow("user:1", fid))


class TestBatches(StoreCase):
    def test_dedupe_only_inside_one_upload(self):
        bid, queued, skipped = store.create_batch(
            "admin", "a.xlsx", "shared", rows("x", "X ", "y"), [1, 2])
        self.assertEqual((queued, skipped), (2, 1))
        # Fresh upload re-queues everything, including past rows.
        bid2, queued2, skipped2 = store.create_batch(
            "admin", "a.xlsx", "shared", rows("x", "y"), [1])
        self.assertEqual((queued2, skipped2), (2, 0))
        self.assertNotEqual(bid, bid2)

    def test_round_robin_dealing_and_progress(self):
        bid, _, _ = store.create_batch(
            "admin", "a.xlsx", "shared", rows("a", "b", "c", "d", "e"), [1, 2])
        p = store.get_progress(bid)
        self.assertEqual(p["total"], 5)
        self.assertEqual(p["per_slot"][1]["total"], 3)
        self.assertEqual(p["per_slot"][2]["total"], 2)
        self.assertEqual(p["pending"], 5)
        slot1 = store.get_batch_rows(bid, slot=1, status="pending")
        self.assertEqual([r["col_a"] for r in slot1], ["a", "c", "e"])
        store.update_row(slot1[0]["id"], "done")
        p = store.get_progress(bid)
        self.assertEqual((p["done"], p["pending"]), (1, 4))
        # attempts=None increments attempts by 1.
        row = store.get_batch_rows(bid, slot=1)[0]
        self.assertEqual(row["attempts"], 1)
        store.update_row(row["id"], "failed", attempts=5)
        row = store.get_batch_rows(bid, slot=1)[0]
        self.assertEqual(row["attempts"], 5)

    def test_active_batch_pointer_and_backfill(self):
        bid, _, _ = store.create_batch(
            "admin", "a.xlsx", "shared", rows("a"), [1])
        self.assertIsNone(store.get_active_batch("admin"))
        store.set_active_batch("admin", bid)
        self.assertEqual(store.get_active_batch("admin"), bid)
        # Backfill: most recent batch with pending rows.
        bid2, _, _ = store.create_batch(
            "admin", "b.xlsx", "shared", rows("z"), [1])
        self.assertEqual(store.latest_pending_batch("admin"), bid2)

    def test_redistribute_pending_only(self):
        bid, _, _ = store.create_batch(
            "admin", "a.xlsx", "shared", rows("a", "b", "c", "d"), [1, 2])
        # Slot 1: a, c. Slot 2: b, d. Mark b done; only d may move.
        slot2 = store.get_batch_rows(bid, slot=2)
        store.update_row(slot2[0]["id"], "done")
        moved = store.redistribute_pending(bid, [2], [1])
        self.assertEqual(moved, 1)
        p = store.get_progress(bid)
        self.assertEqual(p["per_slot"][1]["total"], 3)
        self.assertEqual(p["per_slot"][2]["total"], 1)
        self.assertEqual(p["per_slot"][2]["done"], 1)


class TestBillingStorage(StoreCase):
    def test_reserve_guarded_no_overdraw_and_refund(self):
        self.assertEqual(store.get_balance("user:9"), 0)
        store.adjust_balance("user:9", 120, "topup", ref="t")
        self.assertEqual(store.reserve("user:9", 50, ref="row"), 70)
        self.assertIsNone(store.reserve("user:9", 100, ref="row"))
        self.assertEqual(store.get_balance("user:9"), 70)
        self.assertEqual(store.refund("user:9", 50, ref="row"), 120)
        ledger = store.get_ledger("user:9")
        kinds = [e["kind"] for e in ledger]
        self.assertEqual(kinds[0], "refund")
        self.assertIn("reserve", kinds)
        self.assertIn("topup", kinds)

    def test_list_balances_emptiest_first(self):
        store.adjust_balance("user:2", 500, "topup")
        store.adjust_balance("user:1", 100, "topup")
        wallets = store.list_balances()
        self.assertEqual([w["owner"] for w in wallets],
                         ["user:1", "user:2"])

    def test_gate_board_messages_and_accounts(self):
        store.set_gate_message("user:1", 111, 222)
        self.assertEqual(store.get_gate_message("user:1"),
                         {"chat_id": 111, "msg_id": 222})
        store.clear_gate_message("user:1")
        self.assertIsNone(store.get_gate_message("user:1"))
        store.set_board_message("user:1", 111, 333)
        self.assertEqual(store.get_board_message("user:1")["msg_id"], 333)
        store.upsert_account("admin", 1, name="W", phone="+1",
                             status="active", bogus="ignored")
        acc = store.get_account("admin", 1)
        self.assertEqual(acc["name"], "W")
        self.assertNotIn("bogus", acc)
        self.assertEqual([a["slot"] for a in store.get_accounts("admin")], [1])
        store.remove_account("admin", 1)
        self.assertIsNone(store.get_account("admin", 1))





class TestPhase12Migration(unittest.TestCase):
    """A database created by the Phase 1/2 store must keep working.

    Regression for the live 2026-11-01 failure: /start died on
    "table flows has no column named interval" because init_db
    only creates missing tables and never alters existing ones.
    """

    def test_old_schema_migrates_and_new_writes_work(self):
        import sqlite3

        orig = store.db_path()
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            con = sqlite3.connect(tmp.name)
            con.executescript("""
                CREATE TABLE flows (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                    name TEXT NOT NULL, target TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL, steps TEXT NOT NULL);
                CREATE TABLE batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                    filename TEXT NOT NULL, slot INTEGER NOT NULL,
                    flow_id INTEGER NOT NULL);
                CREATE TABLE queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
                    slot INTEGER NOT NULL, row_index INTEGER NOT NULL,
                    col_a TEXT, col_b TEXT, col_c TEXT, col_d TEXT,
                    status TEXT NOT NULL DEFAULT 'pending');
            """)
            con.commit()
            con.close()
            store.set_db_path(tmp.name)
            store.init_db()  # Must add interval/created_at/mode/attempts.
            store.ensure_default_flows("admin")
            self.assertEqual(len(store.list_flows("admin")), 2)
            bid, queued, skipped = store.create_batch(
                "admin", "old.xlsx", "shared", [("a1", "b1", "", "")], [1])
            self.assertEqual((queued, skipped), (1, 0))
            row = store.get_batch_rows(bid)[0]
            store.update_row(row["id"], "done")
            self.assertEqual(store.get_progress(bid)["done"], 1)
        finally:
            store.set_db_path(orig)
            pathlib.Path(tmp.name).unlink(missing_ok=True)


class TestLedger(StoreCase):
    def test_topup_and_deduct_appear_newest_first(self):
        self.assertEqual(store.adjust_balance("user:1", 1000, "topup"), 1000)
        self.assertEqual(store.adjust_balance("user:1", -50, "deduct"), 950)
        self.assertEqual(store.get_balance("user:1"), 950)
        entries = store.get_ledger("user:1")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["delta"], -50)
        self.assertEqual(entries[0]["kind"], "deduct")
        self.assertEqual(entries[1]["delta"], 1000)
        self.assertEqual(entries[1]["kind"], "topup")

    def test_ledger_is_per_owner(self):
        store.adjust_balance("user:1", 100, "topup")
        store.adjust_balance("user:2", 200, "topup")
        self.assertEqual(len(store.get_ledger("user:1")), 1)
        self.assertEqual(len(store.get_ledger("user:2")), 1)
        self.assertEqual(store.get_ledger("user:3"), [])


class TestScheduledRuns(StoreCase):
    def test_save_get_list_delete_round_trip(self):
        self.assertIsNone(store.get_scheduled_run("admin"))
        store.save_scheduled_run("admin", 7, 3, [1, 2], 1780000000)
        row = store.get_scheduled_run("admin")
        self.assertEqual(row["batch_id"], 7)
        self.assertEqual(row["flow_id"], 3)
        self.assertEqual(row["slots"], [1, 2])
        self.assertEqual(row["scheduled_at"], 1780000000)
        store.save_scheduled_run("admin", 8, 3, [1], 1780000100)
        self.assertEqual(len(store.list_scheduled_runs()), 1)
        self.assertEqual(store.get_scheduled_run("admin")["batch_id"], 8)
        self.assertTrue(store.delete_scheduled_run("admin"))
        self.assertIsNone(store.get_scheduled_run("admin"))
        self.assertFalse(store.delete_scheduled_run("admin"))
        self.assertEqual(store.list_scheduled_runs(), [])


if __name__ == "__main__":
    unittest.main()
