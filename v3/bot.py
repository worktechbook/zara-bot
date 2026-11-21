"""v3 control-bot UI: board labels, step formatting, flow-step capture.

Labels are deliberately distinct from every target-bot label. v3 never
uses these target labels as its own reply-keyboard text:
  "▶️ Start", "📋 Tasks", "🍪", "✅ Account registered", "❌ Cancel"
so a user sending "▶️ Start" during flow creation cannot trigger a
v3 menu command: no v3 menu handler matches that text, and the flow
creation handler records it literally as a step.

Flow-step capture: the caller passes each message the user sends
while creating a flow to record_step(); it returns the step dict and
stores nothing else. In particular it never interprets the text as
a menu command, whatever the text is.

Step dicts use v2's keys: {"type": "send"/"click"/"wait"/"paste", ...}.
"""

from telegram import ReplyKeyboardMarkup

# v3's own menu labels (reply-keyboard text, exact strings).
UI_LABELS = {
    "upload": "📥 Upload Excel",
    "accounts": "🔐 Telegram Accounts",
    "builder": "🧩 Flow Builder",
    "saved": "📄 Saved Flow",
    "run": "🚀 Run batch",
    "stop": "⏹ Stop",
    "status": "📊 Status",
    "instructions": "📘 Bot Instructions",
    "balance": "💰 Balance",
}

# Exact target-bot labels v3 must never use as its own menu text.
FORBIDDEN_UI_LABELS = {
    "▶️ Start",
    "📋 Tasks",
    "🍪",
    "✅ Account registered",
    "❌ Cancel",
}

MENU_BUTTONS = [
    [UI_LABELS["upload"], UI_LABELS["accounts"], UI_LABELS["builder"]],
    [UI_LABELS["saved"], UI_LABELS["run"], UI_LABELS["stop"]],
    [UI_LABELS["status"], UI_LABELS["instructions"]],
    [UI_LABELS["balance"]],
]

# Convenience aliases matching the board keys, for handler modules.
M_UPLOAD = UI_LABELS["upload"]
M_ACCOUNTS = UI_LABELS["accounts"]
M_BUILDER = UI_LABELS["builder"]
M_SAVED = UI_LABELS["saved"]
M_START = UI_LABELS["run"]
M_STOP = UI_LABELS["stop"]
M_STATUS = UI_LABELS["status"]
M_INSTRUCTIONS = UI_LABELS["instructions"]
M_BALANCE = UI_LABELS["balance"]


def board_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MENU_BUTTONS, resize_keyboard=True)


def record_step(text):
    """Record one flow-creation message literally as a step.

    "▶️ Start" sent here is a target-bot step, not a v3 command.
    A "/start" step is never valid in v3: flows start with 📋 Tasks.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty step")
    if text == "/start":
        raise ValueError("v3 flows do not include /start: start with 📋 Tasks")
    return {"type": "send", "text": text}


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


def parse_wait_seconds(text):
    """Parse '5', '5 seconds', 'five seconds' -> seconds. None if unparseable."""
    import re

    t = (text or "").strip().lower()
    m = re.search(r"(\d+)", t)
    if m:
        return int(m.group(1))
    for word, n in NUMBER_WORDS.items():
        if word in t:
            return n
    return None


def _step_type(step):
    # Accept both the v3 "type" key and a legacy "kind" key, so a step
    # dict from any store shape renders the same way.
    return step.get("type") or step.get("kind")


def format_step(step, i):
    t = _step_type(step)
    if t == "send":
        return f"{i}. Send: {step.get('text', '')}"
    if t == "click":
        return f"{i}. Click: {step.get('label', '')}"
    if t == "wait":
        return f"{i}. Wait: {step.get('seconds', '')} seconds"
    if t == "paste":
        col = step.get("column", 1)
        suffix = "" if col == 1 else f" {col}"
        return f"{i}. Paste Worker Name{suffix}"
    return f"{i}. ?"


def format_flow(flow):
    interval = flow.get("interval", flow.get("interval_seconds", ""))
    lines = [
        f"Flow: {flow.get('name', '')}",
        f"Target: {flow.get('target', '')}",
        f"Interval: {interval} seconds",
    ]
    for i, s in enumerate(flow.get("steps", []), 1):
        lines.append(format_step(s, i))
    return "\n".join(lines)


def step_brief(step):
    """One-line brief for the Saved-Flows edit picker."""
    t = _step_type(step)
    if t == "send":
        return f"Send: {step.get('text', '')}"
    if t == "click":
        return f"Click: {step.get('label', '')}"
    if t == "wait":
        return f"Wait: {step.get('seconds', '')}s"
    if t == "paste":
        col = step.get("column", 1)
        suffix = f" {col}" if col and col > 1 else ""
        return f"Paste Worker Name{suffix}"
    return str(step)
