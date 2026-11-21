"""v3 entry point. Reads configuration from a .env file only.

Required in .env (mode 600, never committed, never printed):
  V3_BOT_TOKEN=...        control bot token, from BotFather
  V3_API_ID=...           Pyrogram API id, for worker accounts
  V3_API_HASH=...         Pyrogram API hash, for worker accounts
  V3_ADMIN_IDS=...        comma-separated Telegram user ids, optional
  V3_DB_PATH=...          SQLite path, default data/v3.db

Without V3_BOT_TOKEN the process exits with code 2 and starts
nothing. A missing token is never filled from chat, memory, or a
default. The API id/hash are not boot requirements: they are
needed only when linking a worker account.
"""

import asyncio
import logging
import os
import socket
import sys

log = logging.getLogger("tectonic-v3")


def load_env(path):
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _sd_notify(message):
    """Send one sd_notify message, best-effort. No-op without systemd."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    try:
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(addr)
            sock.sendall(message.encode())
        finally:
            sock.close()
    except Exception:  # noqa: BLE001 — watchdog notify is best-effort
        pass


def main():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = load_env(os.path.join(base, ".env"))
    for key, value in env.items():
        os.environ.setdefault(key, value)
    token = os.environ.get("V3_BOT_TOKEN", "").strip()
    if not token:
        print("V3_BOT_TOKEN is missing from .env; refusing to start.",
              file=sys.stderr)
        return 2

    # Deferred imports: config reads the environment, and .env must be
    # loaded into it before those modules are first imported.
    from v3 import config as v3config
    from v3 import store as v3store
    from v3.control_bot import build_application, restore_scheduled_runs
    from v3.engine import Engine

    problems = v3config.validate()
    if problems:
        for problem in problems:
            print(f"config: {problem}", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.INFO)
    # Pin httpx to WARNING so Bot API URLs (which contain the token)
    # never reach the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db_path = getattr(v3config, "DB_PATH", None) or os.path.join(
        base, "data", "v3.db")
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if hasattr(v3store, "set_db_path"):
        v3store.set_db_path(db_path)
    v3store.init_db()

    engine = Engine()

    request = None
    try:
        from telegram.request import HTTPXRequest
        request = HTTPXRequest(
            proxy=getattr(v3config, "TELEGRAM_PROXY", None) or None,
            connect_timeout=10, read_timeout=20, write_timeout=20,
            pool_timeout=10)
    except Exception:  # noqa: BLE001 — fall back to the default request
        request = None

    async def _post_init(app):
        async def _watchdog():
            _sd_notify("READY=1")
            while True:
                try:
                    await asyncio.wait_for(app.bot.get_me(), timeout=15)
                    _sd_notify("WATCHDOG=1")
                except Exception:
                    # Failed probe withholds the WATCHDOG=1 ping, so a
                    # wedged process gets killed/restarted by systemd.
                    pass
                await asyncio.sleep(30)

        app.bot_data["watchdog_task"] = asyncio.create_task(_watchdog())
        # Re-start timers persisted before a restart. A bad row is
        # dropped with a notice by the helper itself; a failure
        # here must never block boot.
        try:
            await restore_scheduled_runs(app)
        except Exception:  # noqa: BLE001 - boot must not fail here
            log.exception("scheduled-run restore failed")

    async def _post_stop(app):
        task = app.bot_data.pop("watchdog_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        eng = app.bot_data.get("engine")
        if eng is not None and hasattr(eng, "shutdown"):
            await eng.shutdown()

    app = build_application(token, engine=engine, post_init=_post_init,
                            post_stop=_post_stop, request=request)

    async def on_error(update, context):
        log.exception("update failed: %s", context.error)
        message = update.effective_message if update is not None else None
        if message is not None:
            try:
                await message.reply_text(
                    "⚠️ That step didn't go through (connection hiccup). "
                    "Please send it again.")
            except Exception:  # noqa: BLE001 — error reply is best-effort
                pass

    app.add_error_handler(on_error)
    print("Tectonic Engine v3 control bot starting (polling).")
    app.run_polling(allowed_updates=["message", "callback_query"],
                    drop_pending_updates=True, bootstrap_retries=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
