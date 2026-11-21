"""v3 configuration.

Secrets come from environment only, never from files or chat.
Timings in v3: the per-flow interval, set through the bot and
stored on the flow itself, plus the user-ordered 3 second reread
wait in the engine's run-mode read-first rule. There is
deliberately no other time constant in this module.
"""

import os


def _env_ints(name):
    out = set()
    for part in os.environ.get(name, "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            # Warn, do not drop silently: a mistyped id must be visible.
            print(f"config: ignoring malformed id in {name}: {part!r}")
    return out


BOT_TOKEN = os.environ.get("V3_BOT_TOKEN", "")
API_ID = None
_raw_api_id = os.environ.get("V3_API_ID", "").strip()
if _raw_api_id:
    try:
        API_ID = int(_raw_api_id)
    except ValueError:
        print(f"config: ignoring malformed V3_API_ID: {_raw_api_id!r}")
API_HASH = os.environ.get("V3_API_HASH", "")
TELEGRAM_PROXY = os.environ.get("V3_TELEGRAM_PROXY", "") or None

DATA_DIR = os.environ.get("V3_DATA_DIR", "data")
DB_PATH = os.environ.get("V3_DB_PATH", "") or os.path.join(DATA_DIR, "v3.db")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")

ADMIN_IDS = _env_ints("V3_ADMIN_IDS")
BILLING_BYPASS_IDS = _env_ints("V3_BILLING_BYPASS_IDS")

TIMEZONE = "Asia/Dhaka"
MIN_INTERVAL = 1
MAX_INTERVAL = 3600
MAX_SLOTS = 25
COST_PER_ROW_POISHA = 50
LOW_BALANCE_WARN_POISHA = (2500, 500)
DEFAULT_FLOW_INTERVAL_SECONDS = 27


def validate():
    """Return a list of problems; empty means the config can boot."""
    problems = []
    if not BOT_TOKEN:
        problems.append("missing V3_BOT_TOKEN")
    # API id/hash are NOT boot requirements: they are needed only
    # when linking a worker account (begin_link raises clearly
    # without them). Requiring them at boot would leave the bot
    # dead until worker credentials arrive, which is a separate,
    # still-outstanding step.
    return problems
