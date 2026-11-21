"""v3 SQLite store. Fresh implementation of the v2 schema and semantics.

Owners are plain strings: "admin" or "user:<telegram id>".
Flows store steps as JSON lists of dicts:
  {"type": "send", "text": "..."}
  {"type": "click", "label": "..."}
  {"type": "wait", "seconds": N}
  {"type": "paste", "column": 1-4}
A "/start" send step is never valid in v3 (see engine.validate_flow);
the defaults below therefore start with a click on 📋 Tasks.

Connections are opened per call from the module DB path (set with
set_db_path, default config.DB_PATH), so tests pointing the path at
a temp file get an isolated DB.
"""

import json
import os
import sqlite3
import time

import v3.config as config

_DB_PATH = config.DB_PATH


def set_db_path(path):
    global _DB_PATH
    _DB_PATH = path


def db_path():
    return _DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS gate_msgs (
    owner TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS board_msgs (
    owner TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, msg_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    owner TEXT NOT NULL, slot INTEGER NOT NULL, name TEXT, phone TEXT,
    session TEXT, status TEXT NOT NULL DEFAULT 'empty',
    PRIMARY KEY (owner, slot));
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
    name TEXT NOT NULL, target TEXT NOT NULL, interval INTEGER NOT NULL,
    steps TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS user_settings (
    owner TEXT NOT NULL, key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (owner, key));
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
    filename TEXT NOT NULL, mode TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL,
    slot INTEGER NOT NULL, row_index INTEGER NOT NULL,
    col_a TEXT, col_b TEXT, col_c TEXT, col_d TEXT,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_queue_batch ON queue (batch_id);
CREATE TABLE IF NOT EXISTS active_batches (
    owner TEXT PRIMARY KEY, batch_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scheduled_runs (
    owner TEXT PRIMARY KEY, batch_id INTEGER NOT NULL,
    flow_id INTEGER NOT NULL, slots TEXT NOT NULL,
    scheduled_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS credits (
    owner TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
    delta INTEGER NOT NULL, kind TEXT NOT NULL, ref TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ledger_owner ON ledger (owner);
"""


def _connect():
    parent = os.path.dirname(os.path.abspath(_DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(_DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


# Columns the rebuilt schema needs that a Phase 1/2 database lacks.
# CREATE TABLE IF NOT EXISTS never alters an existing table, so
# without this a redeploy over the old database crashes on the
# first write (seen live 2026-11-01: /start died on
# "table flows has no column named interval"). Leftover Phase 1/2
# columns (flows.interval_seconds, batches.slot/flow_id) are
# harmless: every read uses sqlite3.Row by column name.
_MIGRATIONS = {
    "flows": {
        "interval": "INTEGER NOT NULL DEFAULT 27",
        "created_at": "INTEGER NOT NULL DEFAULT 0",
    },
    "batches": {
        "mode": "TEXT NOT NULL DEFAULT 'shared'",
        "created_at": "INTEGER NOT NULL DEFAULT 0",
    },
    "queue": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
    },
}

# Legacy columns the new code never writes but that block inserts
# because they are NOT NULL without a default (flows.interval_seconds
# is the live example: new inserts omit it, so the row is refused).
# Nullable leftovers (batches.slot/flow_id) are left alone.
_MIGRATION_DROPS = {
    "flows": ["interval_seconds"],
    "batches": ["slot", "flow_id"],
}


def init_db():
    con = _connect()
    try:
        con.executescript(_SCHEMA)
        for table, columns in _MIGRATIONS.items():
            existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in existing:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        for table, columns in _MIGRATION_DROPS.items():
            existing = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name in columns:
                if name in existing:
                    con.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
        con.commit()
    finally:
        con.close()


def _now():
    return int(time.time())


# -------------------------------------------------------------- accounts

_ACCOUNT_FIELDS = ("name", "phone", "session", "status")


def get_accounts(owner):
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM accounts WHERE owner=? ORDER BY slot",
            (owner,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_account(owner, slot):
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM accounts WHERE owner=? AND slot=?",
            (owner, slot)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def upsert_account(owner, slot, **fields):
    """Only name/phone/session/status may be written."""
    clean = {k: v for k, v in fields.items() if k in _ACCOUNT_FIELDS}
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM accounts WHERE owner=? AND slot=?",
            (owner, slot)).fetchone()
        if row is None:
            data = {"name": None, "phone": None, "session": None,
                    "status": "empty"}
            data.update(clean)
            con.execute(
                "INSERT INTO accounts (owner, slot, name, phone, session, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (owner, slot, data["name"], data["phone"], data["session"],
                 data["status"]))
        elif clean:
            assignments = ", ".join(f"{k}=?" for k in clean)
            con.execute(
                f"UPDATE accounts SET {assignments} WHERE owner=? AND slot=?",
                (*clean.values(), owner, slot))
        con.commit()
    finally:
        con.close()


def remove_account(owner, slot):
    con = _connect()
    try:
        con.execute("DELETE FROM accounts WHERE owner=? AND slot=?",
                    (owner, slot))
        con.commit()
    finally:
        con.close()


# ----------------------------------------------------------------- flows

DEFAULT_FLOWS = [
    {
        "name": "COOKIES NO MAIL",
        "target": "@Taskl1_bot",
        "interval": config.DEFAULT_FLOW_INTERVAL_SECONDS,
        "steps": [
            {"type": "click", "label": "📋 Tasks"},
            {"type": "click", "label": "🍪 ($0.0400)"},
            {"type": "click", "label": "🌟 Create FB (No mail) ($0.0300)"},
            {"type": "click", "label": "▶️ Start"},
            {"type": "paste", "column": 1},
            {"type": "click", "label": "✅ Account registered"},
        ],
    },
    {
        "name": "2FA COOKIES",
        "target": "@Taskl1_bot",
        "interval": config.DEFAULT_FLOW_INTERVAL_SECONDS,
        "steps": [
            {"type": "click", "label": "📋 Tasks"},
            {"type": "click", "label": "🍪 ($0.0400)"},
            {"type": "click", "label": "🌟 Create FB (No mail) ($0.0300)"},
            {"type": "click", "label": "▶️ Start"},
            {"type": "paste", "column": 2},
            {"type": "paste", "column": 1},
            {"type": "click", "label": "✅ Account registered"},
        ],
    },
]


def _flow_dict(row):
    flow = dict(row)
    flow["steps"] = json.loads(flow["steps"])
    flow["interval_seconds"] = flow["interval"]
    return flow


def save_flow(owner, name, target, interval, steps):
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO flows (owner, name, target, interval, steps, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (owner, name, target, interval, json.dumps(steps), _now()))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_flows(owner):
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM flows WHERE owner=? ORDER BY id", (owner,)).fetchall()
        return [_flow_dict(r) for r in rows]
    finally:
        con.close()


def get_flow(owner, flow_id):
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM flows WHERE owner=? AND id=?",
            (owner, flow_id)).fetchone()
        return _flow_dict(row) if row else None
    finally:
        con.close()


def delete_flow(owner, flow_id):
    con = _connect()
    try:
        cur = con.execute(
            "DELETE FROM flows WHERE owner=? AND id=?", (owner, flow_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def update_flow_steps(owner, flow_id, steps):
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE flows SET steps=? WHERE owner=? AND id=?",
            (json.dumps(steps), owner, flow_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def set_active_flow(owner, flow_id):
    con = _connect()
    try:
        con.execute(
            "INSERT INTO user_settings (owner, key, value) VALUES (?, 'active_flow', ?)"
            " ON CONFLICT(owner, key) DO UPDATE SET value=excluded.value",
            (owner, str(flow_id)))
        con.commit()
    finally:
        con.close()


def get_active_flow(owner):
    con = _connect()
    try:
        row = con.execute(
            "SELECT value FROM user_settings WHERE owner=? AND key='active_flow'",
            (owner,)).fetchone()
        if row is None:
            return None
        try:
            return get_flow(owner, int(row["value"]))
        except (TypeError, ValueError):
            return None
    finally:
        con.close()


def ensure_default_flows(owner):
    """Seed any missing default by name; activate the first default
    when the owner has no active flow. Idempotent."""
    existing = {f["name"] for f in list_flows(owner)}
    for default in DEFAULT_FLOWS:
        if default["name"] not in existing:
            save_flow(owner, default["name"], default["target"],
                      default["interval"], default["steps"])
    if get_active_flow(owner) is None:
        flows = list_flows(owner)
        if flows:
            set_active_flow(owner, flows[0]["id"])


# --------------------------------------------------------- batches/queue

def create_batch(owner, filename, mode, rows, slots):
    """Queue one upload fresh from row 1.

    rows: list of (col_a, col_b, col_c, col_d) tuples.
    Dedupe is ONLY inside this upload, on col_a stripped + lowered.
    Rows are dealt round-robin across slots.
    Returns (batch_id, queued, skipped).
    """
    if not slots:
        raise ValueError("create_batch needs at least one slot")
    seen = set()
    fresh = []
    skipped = 0
    for row in rows:
        key = (row[0] or "").strip().lower()
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        fresh.append(row)
    con = _connect()
    try:
        cur = con.execute(
            "INSERT INTO batches (owner, filename, mode, created_at)"
            " VALUES (?, ?, ?, ?)", (owner, filename, mode, _now()))
        batch_id = cur.lastrowid
        for i, row in enumerate(fresh):
            slot = slots[i % len(slots)]
            con.execute(
                "INSERT INTO queue (batch_id, slot, row_index,"
                " col_a, col_b, col_c, col_d) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, slot, i, row[0], row[1], row[2], row[3]))
        con.commit()
        return batch_id, len(fresh), skipped
    finally:
        con.close()


def get_batch(batch_id):
    con = _connect()
    try:
        row = con.execute("SELECT * FROM batches WHERE id=?",
                          (batch_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def set_active_batch(owner, batch_id):
    con = _connect()
    try:
        con.execute(
            "INSERT INTO active_batches (owner, batch_id) VALUES (?, ?)"
            " ON CONFLICT(owner) DO UPDATE SET batch_id=excluded.batch_id",
            (owner, batch_id))
        con.commit()
    finally:
        con.close()


def get_active_batch(owner):
    con = _connect()
    try:
        row = con.execute(
            "SELECT batch_id FROM active_batches WHERE owner=?",
            (owner,)).fetchone()
        return row["batch_id"] if row else None
    finally:
        con.close()


def latest_pending_batch(owner):
    """Most recent batch of this owner with at least one pending row."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT b.id FROM batches b JOIN queue q ON q.batch_id=b.id"
            " WHERE b.owner=? AND q.status='pending'"
            " ORDER BY b.id DESC LIMIT 1", (owner,)).fetchone()
        return row["id"] if row else None
    finally:
        con.close()


def save_scheduled_run(owner, batch_id, flow_id, slots, scheduled_at):
    """Persist one pending timer per owner (one run per owner, as
    in Engine._runs). INSERT OR REPLACE: a new schedule replaces
    the old one. The row is the schedule's only durable record:
    the engine deletes it when the run is consumed, cancelled, or
    dropped, and boot restore re-starts whatever row survives."""
    con = _connect()
    try:
        con.execute(
            "INSERT INTO scheduled_runs (owner, batch_id, flow_id,"
            " slots, scheduled_at, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(owner) DO UPDATE SET batch_id=excluded.batch_id,"
            " flow_id=excluded.flow_id, slots=excluded.slots,"
            " scheduled_at=excluded.scheduled_at,"
            " created_at=excluded.created_at",
            (owner, batch_id, flow_id,
             json.dumps([int(s) for s in slots]),
             int(scheduled_at), int(time.time())))
        con.commit()
    finally:
        con.close()


def _scheduled_dict(row):
    d = dict(row)
    try:
        d["slots"] = [int(s) for s in json.loads(d["slots"])]
    except (TypeError, ValueError):
        d["slots"] = []
    return d


def get_scheduled_run(owner):
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM scheduled_runs WHERE owner=?",
            (owner,)).fetchone()
        return _scheduled_dict(row) if row else None
    finally:
        con.close()


def list_scheduled_runs():
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM scheduled_runs").fetchall()
        return [_scheduled_dict(r) for r in rows]
    finally:
        con.close()


def delete_scheduled_run(owner):
    con = _connect()
    try:
        cur = con.execute(
            "DELETE FROM scheduled_runs WHERE owner=?", (owner,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def get_batch_rows(batch_id, slot=None, status=None):
    con = _connect()
    try:
        query = "SELECT * FROM queue WHERE batch_id=?"
        params = [batch_id]
        if slot is not None:
            query += " AND slot=?"
            params.append(slot)
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY row_index"
        rows = con.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def update_row(row_id, status, attempts=None):
    """Set a row's status. attempts=None also increments attempts by 1."""
    con = _connect()
    try:
        if attempts is None:
            con.execute(
                "UPDATE queue SET status=?, attempts=attempts+1 WHERE id=?",
                (status, row_id))
        else:
            con.execute(
                "UPDATE queue SET status=?, attempts=? WHERE id=?",
                (status, attempts, row_id))
        con.commit()
    finally:
        con.close()


def get_progress(batch_id):
    con = _connect()
    try:
        rows = con.execute(
            "SELECT slot, status, COUNT(*) AS n FROM queue"
            " WHERE batch_id=? GROUP BY slot, status", (batch_id,)).fetchall()
        per_slot = {}
        total = done = failed = 0
        for r in rows:
            slot = r["slot"]
            per_slot.setdefault(slot, {"total": 0, "done": 0, "failed": 0})
            per_slot[slot]["total"] += r["n"]
            if r["status"] == "done":
                per_slot[slot]["done"] += r["n"]
                done += r["n"]
            elif r["status"] == "failed":
                per_slot[slot]["failed"] += r["n"]
                failed += r["n"]
            total += r["n"]
        for slot in per_slot:
            s = per_slot[slot]
            s["pending"] = s["total"] - s["done"] - s["failed"]
        return {"total": total, "done": done, "failed": failed,
                "pending": total - done - failed, "per_slot": per_slot}
    finally:
        con.close()


def redistribute_pending(batch_id, from_slots, to_slots):
    """Move ONLY pending rows off dead slots, round-robin, in
    row_index order. Done/failed rows stay pinned. Returns count."""
    if not to_slots:
        return 0
    from_set = set(from_slots)
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, slot FROM queue WHERE batch_id=? AND status='pending'"
            " ORDER BY row_index", (batch_id,)).fetchall()
        moved = 0
        for row in rows:
            if row["slot"] in from_set:
                new_slot = to_slots[moved % len(to_slots)]
                con.execute("UPDATE queue SET slot=? WHERE id=?",
                            (new_slot, row["id"]))
                moved += 1
        con.commit()
        return moved
    finally:
        con.close()


# ------------------------------------------------------- gate/board msgs

def _set_msg(table, owner, chat_id, msg_id):
    con = _connect()
    try:
        con.execute(
            f"INSERT INTO {table} (owner, chat_id, msg_id) VALUES (?, ?, ?)"
            f" ON CONFLICT(owner) DO UPDATE SET chat_id=excluded.chat_id,"
            f" msg_id=excluded.msg_id",
            (owner, chat_id, msg_id))
        con.commit()
    finally:
        con.close()


def _get_msg(table, owner):
    con = _connect()
    try:
        row = con.execute(f"SELECT * FROM {table} WHERE owner=?",
                          (owner,)).fetchone()
        return {"chat_id": row["chat_id"], "msg_id": row["msg_id"]} if row else None
    finally:
        con.close()


def _clear_msg(table, owner):
    con = _connect()
    try:
        con.execute(f"DELETE FROM {table} WHERE owner=?", (owner,))
        con.commit()
    finally:
        con.close()


def set_gate_message(owner, chat_id, msg_id):
    _set_msg("gate_msgs", owner, chat_id, msg_id)


def get_gate_message(owner):
    return _get_msg("gate_msgs", owner)


def clear_gate_message(owner):
    _clear_msg("gate_msgs", owner)


def set_board_message(owner, chat_id, msg_id):
    _set_msg("board_msgs", owner, chat_id, msg_id)


def get_board_message(owner):
    return _get_msg("board_msgs", owner)


def clear_board_message(owner):
    _clear_msg("board_msgs", owner)


# ---------------------------------------------------------------- billing

def get_balance(owner):
    con = _connect()
    try:
        row = con.execute("SELECT balance FROM credits WHERE owner=?",
                          (owner,)).fetchone()
        return row["balance"] if row else 0
    finally:
        con.close()


def adjust_balance(owner, delta, kind, ref=""):
    """Atomic credit-row update plus a ledger row. Returns new balance."""
    con = _connect()
    try:
        con.execute(
            "INSERT INTO credits (owner, balance) VALUES (?, ?)"
            " ON CONFLICT(owner) DO UPDATE SET balance=balance+excluded.balance",
            (owner, delta))
        con.execute(
            "INSERT INTO ledger (owner, delta, kind, ref, created_at)"
            " VALUES (?, ?, ?, ?, ?)", (owner, delta, kind, ref, _now()))
        row = con.execute("SELECT balance FROM credits WHERE owner=?",
                          (owner,)).fetchone()
        con.commit()
        return row["balance"]
    finally:
        con.close()


def list_balances():
    con = _connect()
    try:
        rows = con.execute(
            "SELECT owner, balance FROM credits ORDER BY balance ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_ledger(owner, limit=50):
    con = _connect()
    try:
        rows = con.execute(
            "SELECT delta, kind, ref, created_at AS at FROM ledger"
            " WHERE owner=? ORDER BY id DESC LIMIT ?", (owner, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def reserve(owner, amount, ref=""):
    """Guarded single UPDATE: concurrent slots cannot overdraw.
    Returns the new balance, or None when the wallet cannot cover it."""
    con = _connect()
    try:
        row = con.execute("SELECT balance FROM credits WHERE owner=?",
                          (owner,)).fetchone()
        if row is None:
            con.execute("INSERT INTO credits (owner, balance) VALUES (?, 0)",
                        (owner,))
            con.commit()
            return None
        cur = con.execute(
            "UPDATE credits SET balance=balance-? WHERE owner=? AND balance>=?",
            (amount, owner, amount))
        if cur.rowcount == 0:
            con.commit()
            return None
        con.execute(
            "INSERT INTO ledger (owner, delta, kind, ref, created_at)"
            " VALUES (?, ?, 'reserve', ?, ?)", (owner, -amount, ref, _now()))
        row = con.execute("SELECT balance FROM credits WHERE owner=?",
                          (owner,)).fetchone()
        con.commit()
        return row["balance"]
    finally:
        con.close()


def refund(owner, amount, ref=""):
    return adjust_balance(owner, amount, "refund", ref)
