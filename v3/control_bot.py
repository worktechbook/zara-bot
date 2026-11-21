"""v3 control bot (python-telegram-bot v22): board, gate, handlers.

Fresh implementation following REBUILD_SPEC.md §2.5/§2.6/§2.7/§3.1/§3.5.
v2 parity except: flows never contain a /start step (they begin with
📋 Tasks), the board's Run button is "🚀 Run batch" (never "▶️ Start",
so a ▶️ Start step value cannot be intercepted by the menu router),
and a re-press of Saved-Flows ✏️ Edit while an edit is open restarts
the step list instead of sending nothing.

Handler order mirrors v2: conversations in group 0, menu routing and
all other handlers in group 1. Error handling lives in v3.main.
"""

import logging
import os
import re
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from v3 import billing as vbilling
from v3 import config as v3config
from v3 import sheet as v3sheet
from v3 import store as v3store
from v3.bot import (
    M_ACCOUNTS,
    M_BALANCE,
    M_BUILDER,
    M_INSTRUCTIONS,
    M_SAVED,
    M_START,
    M_STATUS,
    M_STOP,
    M_UPLOAD,
    MENU_BUTTONS,
    UI_LABELS,
    board_markup,
    format_flow,
    parse_wait_seconds,
    step_brief,
)

log = logging.getLogger("tectonic-v3")

BOARD_TEXT = "🤖 Worker Board\n\nChoose an action:"

# In-memory active-batch cache only; the authoritative pointer lives
# in store.active_batches (written on every upload).
ACTIVE_BATCH: dict[int, int] = {}

MAX_SLOTS = getattr(v3config, "MAX_SLOTS", 25)

_KIND_FALLBACK = {
    "topup": "Top-up",
    "deduct": "ID submitted",
    "adjust": "Correction",
    "reserve": "Reserved",
    "refund": "Refunded",
}


def kind_label(kind):
    labels = getattr(vbilling, "KIND_LABELS", None)
    if isinstance(labels, dict):
        return labels.get(kind, _KIND_FALLBACK.get(kind, kind))
    return _KIND_FALLBACK.get(kind, kind)


# ------------------------------------------------------------------ auth

def owner_of(update):
    """Return the caller's owner namespace; None only without a user."""
    user = update.effective_user
    if user is None:
        return None
    if user.id in getattr(v3config, "ADMIN_IDS", set()):
        return "admin"
    return f"user:{user.id}"


def require_owner(update, context):
    return owner_of(update)


def _board_allowed(owner):
    """True when /start may open the Worker Board directly."""
    if not vbilling.billable(owner):
        return True
    return v3store.get_balance(owner) >= v3config.COST_PER_ROW_POISHA


async def cmd_start(update, context):
    # NOTE: no user_data.clear() here. /start can arrive while a Flow
    # Builder / Edit / login conversation is open; PTB keeps those
    # conversation states in memory regardless, so clearing user_data
    # only corrupts them (lost drafts, 'session expired' wedges).
    owner = owner_of(update)
    if owner is None:
        return
    if _board_allowed(owner):
        v3store.ensure_default_flows(owner)
        v3store.clear_gate_message(owner)
        await show_board(update, context)
        return
    await show_gate(update, context)


# ------------------------------------------------------------------ board

def engine(context):
    return context.bot_data.get("engine")


def owner_chat_id(owner):
    """Telegram chat id for an owner namespace ('admin' or 'user:<id>')."""
    if owner == "admin":
        ids = getattr(v3config, "ADMIN_IDS", set()) or set()
        return sorted(ids)[0] if ids else None
    if owner.startswith("user:"):
        try:
            return int(owner.split(":", 1)[1])
        except ValueError:
            return None
    return None


def resolve_active_batch(owner, user_id):
    """Active batch for this owner, surviving restarts.

    Order: in-memory cache (if it still points at this owner's batch),
    then the persisted pointer, then — only when nothing was ever
    persisted — the owner's most recent batch with pending rows.
    The resolved batch is cached in ACTIVE_BATCH.
    """
    batch_id = ACTIVE_BATCH.get(user_id)
    if batch_id:
        batch = v3store.get_batch(batch_id)
        if batch is not None and batch.get("owner") == owner:
            return batch_id
        ACTIVE_BATCH.pop(user_id, None)
    batch_id = v3store.get_active_batch(owner)
    if batch_id:
        batch = v3store.get_batch(batch_id)
        if batch is not None and batch.get("owner") == owner:
            ACTIVE_BATCH[user_id] = batch_id
            return batch_id
    batch_id = v3store.latest_pending_batch(owner)
    if batch_id:
        ACTIVE_BATCH[user_id] = batch_id
        return batch_id
    return None


async def send_board_to_chat(bot, owner, chat_id):
    """Delete the owner's previous board message, send a fresh one."""
    old = v3store.get_board_message(owner)
    if old is not None:
        try:
            await bot.delete_message(old["chat_id"], old["msg_id"])
        except Exception:  # noqa: BLE001 — already gone / too old
            pass
        v3store.clear_board_message(owner)
    sent = await bot.send_message(chat_id, BOARD_TEXT,
                                  reply_markup=board_markup())
    try:
        v3store.set_board_message(owner, chat_id, int(sent.message_id))
    except Exception:  # noqa: BLE001 — tracking is best-effort
        pass


async def show_board(update, context):
    """Send the Worker Board, replacing the previous board message."""
    owner = require_owner(update, context)
    chat = update.effective_chat
    if owner is not None and chat is not None:
        await send_board_to_chat(context.bot, owner, chat.id)
    else:
        await update.effective_message.reply_text(
            BOARD_TEXT, reply_markup=board_markup())


REVERT_NOTICE = (
    "💰 Your credit is fully used up — you're back on the subscription "
    "screen. Top up to continue using the bot.")


async def revert_to_paywall(bot, owner, chat_id):
    """Remove the owner's Worker Board and show the subscription paywall.

    Idempotent: returns without sending anything when no board message
    is remembered for the owner, so a second call can never stack a
    second paywall.
    """
    old = v3store.get_board_message(owner)
    if old is None:
        return
    try:
        await bot.delete_message(old["chat_id"], old["msg_id"])
    except Exception:  # noqa: BLE001 — already gone / too old
        pass
    v3store.clear_board_message(owner)
    # The reply keyboard survives message deletion on the client, so the
    # notice carries ReplyKeyboardRemove; the gate screen then arrives
    # with only its inline buttons.
    try:
        await bot.send_message(chat_id, REVERT_NOTICE,
                               reply_markup=ReplyKeyboardRemove())
    except Exception:  # noqa: BLE001
        pass
    try:
        sent = await bot.send_message(chat_id, vbilling.gate_text(),
                                      reply_markup=gate_markup())
        # Remembered so /credit can swap it back into the Worker Board
        # the moment the admin tops the user up.
        v3store.set_gate_message(owner, chat_id, int(sent.message_id))
    except Exception:  # noqa: BLE001
        pass


async def maybe_revert_idle_owner(bot, owner, eng):
    """Revert a broke, fully-idle owner to the paywall. True if reverted.

    Fires only when no slot is running for the owner (no rows in
    flight, so every reservation has settled into a charge or a
    refund) and the settled balance still cannot cover one ID — never
    on reserved-but-unsettled credit. A remembered board message makes
    it idempotent: one revert per board.
    """
    if not vbilling.billable(owner):
        return False
    if eng is not None and eng.has_active_tasks(owner):
        return False
    if v3store.get_balance(owner) >= v3config.COST_PER_ROW_POISHA:
        return False
    if v3store.get_board_message(owner) is None:
        return False
    chat_id = owner_chat_id(owner)
    if chat_id is None:
        return False
    await revert_to_paywall(bot, owner, chat_id)
    return True


def active_accounts(owner):
    return [a for a in v3store.get_accounts(owner)
            if a.get("status") == "active"]


def slot_label(owner, slot):
    acc = v3store.get_account(owner, slot)
    if acc and acc.get("status") == "active":
        name = acc.get("name") or acc.get("phone") or f"Account {slot}"
        return f"✅ Account {slot} ({name})"
    return f"➕ Account {slot}"


def back_button(cb):
    return [[InlineKeyboardButton("⬅️ Back", callback_data=cb)]]


# ------------------------------------------------------------------- gate

SUPPORT_URL = os.getenv("V3_SUPPORT_URL", "https://t.me/your_support_handle")

CB_HOW = "gate:how"
CB_BUY = "gate:buy"
CB_BACK = "gate:back"


def gate_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📘 How This Bot Works",
                             callback_data=CB_HOW)],
        [InlineKeyboardButton("💳 Buy Subscription",
                             callback_data=CB_BUY)],
        [InlineKeyboardButton("💬 Contact With Support", url=SUPPORT_URL)],
    ])


def _gate_back_markup():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Back", callback_data=CB_BACK)],
    ])


async def show_gate(update, context):
    """Send the paywall screen and remember its message id."""
    msg = await update.effective_message.reply_text(
        vbilling.gate_text(), reply_markup=gate_markup())
    owner = require_owner(update, context)
    if owner and update.effective_chat is not None:
        v3store.set_gate_message(owner, update.effective_chat.id,
                                 int(msg.message_id))


async def on_gate_callback(update, context):
    q = update.callback_query
    if q is None:
        return
    data = q.data or ""
    await q.answer()
    if require_owner(update, context) is None:
        return
    if data == CB_HOW:
        # In place: the paywall message becomes instructions page 1.
        await show_instructions(update, context, gate=True)
    elif data == CB_BUY:
        await q.message.edit_text(
            vbilling.buy_subscription_text(update.effective_user.id),
            reply_markup=_gate_back_markup())
    elif data == CB_BACK:
        await q.message.edit_text(vbilling.gate_text(),
                                  reply_markup=gate_markup())


# ------------------------------------------------------------ menu router

_ROUTES = {}


async def route_menu(update, context):
    if require_owner(update, context) is None:
        return
    text = (update.effective_message.text or "").strip()
    handler = _ROUTES.get(text)
    if handler:
        await handler(update, context)
    # unknown text outside a conversation: ignore quietly


# ----------------------------------------------------------------- upload

async def show_upload_menu(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    accs = active_accounts(owner)
    buttons = [
        [InlineKeyboardButton(f"Upload Excel {a['slot']}",
                             callback_data=f"up:slot:{a['slot']}")]
        for a in accs
    ]
    buttons.append([InlineKeyboardButton("🔄 Shared Excel",
                                         callback_data="up:shared")])
    buttons += back_button("up:back")
    await update.effective_message.reply_text(
        "Upload Excel\n\nChoose which account's worker queue should receive "
        "the file:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _on_upload_choice(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "up:back":
        await show_board(update, context)
        return
    if data == "up:shared":
        owner = require_owner(update, context)
        if owner is None:
            return
        if not active_accounts(owner):
            await q.message.reply_text(
                "No active Telegram accounts. Add at least one account first.")
            return
        context.user_data["pending_upload"] = ("shared", None)
        await q.message.reply_text(
            "🔄 Shared Excel\n"
            "Send one .xlsx or .xlsm file.\n"
            "Rows will be assigned round-robin and permanently reserved:\n"
            "row 1 → first account, row 2 → second account, and so on.\n"
            "No row will be processed by two accounts.")
        return
    if data.startswith("up:slot:"):
        try:
            slot = int(data.split(":")[2])
        except (IndexError, ValueError):
            await q.message.reply_text(
                "That upload button is stale — pick the account again.")
            return
        owner = require_owner(update, context)
        if owner is None:
            return
        if slot not in [a["slot"] for a in active_accounts(owner)]:
            await q.message.reply_text(
                f"Account {slot} is no longer linked — link it first, "
                "then upload again.")
            return
        context.user_data["pending_upload"] = ("slot", slot)
        await q.message.reply_text(
            f"Upload Excel {slot}\n"
            f"Send an .xlsx or .xlsm file for Account {slot}. "
            "Column A from every sheet will be read as worker names.")
        return


def _rows_from_sheet(tmp_name):
    """Return (rows, sheets, skipped) from a downloaded workbook.

    Uses the detailed parser when the sheet module provides it, and
    falls back to the plain row parser otherwise.
    """
    detailed = getattr(v3sheet, "parse_xlsx_detailed", None)
    if detailed is not None:
        return detailed(tmp_name)
    rows = v3sheet.parse_xlsx(tmp_name)
    return rows, [], 0


async def on_document(update, context):
    msg = update.effective_message
    owner = require_owner(update, context)
    if owner is None:
        return
    pending = context.user_data.get("pending_upload")
    if not pending:
        await msg.reply_text(
            "Choose Upload Excel and pick a target first, then send the file.")
        return
    kind, slot = pending
    doc = msg.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".xlsx") or name.endswith(".xlsm")):
        await msg.reply_text("Send one .xlsx or .xlsm file.")
        return

    if kind == "shared":
        slots = [a["slot"] for a in active_accounts(owner)]
        mode = "shared"
    else:
        slots = [slot]
        mode = f"slot:{slot}"
    if not slots:
        await msg.reply_text("No active accounts for this upload.")
        context.user_data.pop("pending_upload", None)
        return

    tmp = tempfile.NamedTemporaryFile(
        suffix=os.path.splitext(name)[1] or ".xlsx", delete=False)
    tmp.close()
    try:
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(tmp.name)
        rows, sheets, skipped_empty = _rows_from_sheet(tmp.name)
    except Exception as exc:  # noqa: BLE001 - report parse failures plainly
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        await msg.reply_text(f"Could not read that file: {exc}")
        return
    finally:
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)

    if not rows:
        await msg.reply_text("No usable rows found (Column A is empty). "
                             f"Skipped {skipped_empty} empty row(s).")
        context.user_data.pop("pending_upload", None)
        return

    batch_id, queued, skipped = v3store.create_batch(
        owner, doc.file_name or "upload.xlsx", mode, rows, slots)
    context.user_data.pop("pending_upload", None)
    if not queued:
        await msg.reply_text(
            f"Nothing new queued: all {len(rows)} row(s) in "
            f"{doc.file_name} were duplicates inside the file.")
        return
    ACTIVE_BATCH[update.effective_user.id] = batch_id
    v3store.set_active_batch(owner, batch_id)
    skip_line = (f"Skipped: {skipped} duplicate row(s) in the file\n"
                 if skipped else "")
    await msg.reply_text(
        f"✅ Uploaded {doc.file_name}\n"
        f"Rows found: {len(rows)} ({v3sheet.detect_mode(rows)} mode)\n"
        f"Queued: {queued} new row(s)\n"
        f"{skip_line}"
        f"Sheets: {', '.join(sheets)}\n"
        f"Target: {'all active accounts (shared)' if kind == 'shared' else f'Account {slot}'}\n"
        "This batch is now active. Pick a flow in 📄 Saved Flow, then "
        "🚀 Run batch.")
    await show_board(update, context)


async def _on_upload_text(update, context):
    """Nudge when a user types text while the bot waits for a file.

    Only fires for non-menu text while an upload target is pending;
    menu taps still reach the router, and conversations (group 0)
    always win over this group-1 handler.
    """
    if not context.user_data.get("pending_upload"):
        return
    await update.effective_message.reply_text(
        "Send one .xlsx or .xlsm file for this upload.")


_MENU_TEXTS = [M_UPLOAD, M_ACCOUNTS, M_BUILDER, M_SAVED, M_START,
               M_STOP, M_STATUS, M_INSTRUCTIONS, M_BALANCE]


# --------------------------------------------------------------- accounts

LOGIN_PHONE, LOGIN_CODE, LOGIN_PASSWORD = range(3)


async def show_accounts(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    buttons = []
    row = []
    for s in range(1, MAX_SLOTS + 1):
        row.append(InlineKeyboardButton(slot_label(owner, s),
                                        callback_data=f"acct:{s}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons += back_button("acct:back")
    await update.effective_message.reply_text(
        "Telegram Accounts\n\n"
        "Each account has its own encrypted session. "
        "Select an account to log in, check status, or remove it.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _on_account_cb(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "acct:back":
        await show_board(update, context)
        return
    if data.startswith("acct:"):
        slot = int(data.split(":")[1])
        await _show_slot(update, context, slot)


async def _show_slot(update, context, slot):
    owner = require_owner(update, context)
    if owner is None:
        return
    acc = v3store.get_account(owner, slot)
    name = acc["name"] if acc and acc["name"] else f"Account {slot}"
    status = acc["status"] if acc else "empty"
    phone = acc["phone"] if acc and acc["phone"] else "—"
    text = (f"Telegram Account {slot}\n\n"
            f"Status: {status}\n"
            f"Name: {name}\n"
            f"Phone: {phone}\n\n"
            "API details and session are encrypted locally.")
    buttons = [
        [InlineKeyboardButton("🔑 Login / Re-login",
                             callback_data=f"acct_login:{slot}"),
         InlineKeyboardButton("📊 Status",
                             callback_data=f"acct_status:{slot}")],
        [InlineKeyboardButton("🗑 Disconnect & Remove",
                             callback_data=f"acct_unlink:{slot}")],
    ]
    buttons += back_button("acct:back")
    # called from both message and callback contexts
    await update.effective_message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(buttons))


async def _on_slot_status(update, context):
    q = update.callback_query
    await q.answer()
    slot = int(q.data.split(":")[1])
    owner = require_owner(update, context)
    if owner is None:
        return
    acc = v3store.get_account(owner, slot)
    if not acc or acc.get("status") != "active":
        await q.message.reply_text(f"Account {slot}: not connected.")
        return
    await q.message.reply_text(
        f"Account {slot}: {(acc.get('name') or 'connected')}\n"
        f"Phone: {acc.get('phone') or '—'}\n"
        "Session: active.")


async def _on_unlink(update, context):
    q = update.callback_query
    await q.answer()
    slot = int(q.data.split(":")[1])
    buttons = [
        [InlineKeyboardButton("✅ Yes, remove",
                             callback_data=f"acct_unlink_yes:{slot}"),
         InlineKeyboardButton("⬅️ Back", callback_data=f"acct:{slot}")],
    ]
    await q.message.reply_text(
        f"Disconnect and remove Account {slot}? "
        "Its saved session will be deleted.",
        reply_markup=InlineKeyboardMarkup(buttons))


async def _on_unlink_yes(update, context):
    q = update.callback_query
    await q.answer()
    slot = int(q.data.split(":")[1])
    owner = require_owner(update, context)
    if owner is None:
        return
    eng = engine(context)
    try:
        if eng is not None:
            await eng.unlink(owner, slot)
    except Exception:  # noqa: BLE001 - engine may raise on missing session
        pass
    v3store.remove_account(owner, slot)
    await q.message.reply_text(f"Account {slot} disconnected and removed.")
    await show_board(update, context)


async def login_entry(update, context):
    q = update.callback_query
    await q.answer()
    if require_owner(update, context) is None:
        return ConversationHandler.END
    slot = int(q.data.split(":")[1])
    context.user_data["login_slot"] = slot
    await q.message.reply_text(
        f"Login — Account {slot}\n\nSend the phone number "
        "(international format, e.g. +8801XXXXXXXXX).")
    return LOGIN_PHONE


async def login_phone(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return ConversationHandler.END
    phone = (update.effective_message.text or "").strip().replace(" ", "")
    slot = context.user_data["login_slot"]
    context.user_data["login_phone"] = phone
    eng = engine(context)
    try:
        await eng.begin_link(owner, slot, phone)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(
            f"Could not start login: {exc}\nSend the phone number again, "
            "or /start to quit.")
        return LOGIN_PHONE
    await update.effective_message.reply_text(
        "Code sent. Send the Telegram login code here. "
        "(It will be deleted automatically once read.)")
    return LOGIN_CODE


async def _delete_quietly(update):
    """Auto-delete a credential message right after reading it (a code
    or password must not linger in chat)."""
    try:
        await update.effective_message.delete()
    except Exception:  # noqa: BLE001 - deletion is best-effort hygiene
        pass


async def login_code(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return ConversationHandler.END
    code = (update.effective_message.text or "").strip().replace(" ", "")
    await _delete_quietly(update)
    slot = context.user_data["login_slot"]
    context.user_data["login_code"] = code
    eng = engine(context)
    try:
        name = await eng.complete_link(owner, slot, code)
    except Exception:
        # Most likely 2FA password required — ask and retry.
        await update.effective_message.reply_text(
            "This account needs its 2-step verification password. "
            "Send it now.")
        return LOGIN_PASSWORD
    return await _login_done(update, context, name)


async def login_password(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return ConversationHandler.END
    password = (update.effective_message.text or "").strip()
    await _delete_quietly(update)
    slot = context.user_data["login_slot"]
    code = context.user_data["login_code"]
    eng = engine(context)
    try:
        name = await eng.complete_link(owner, slot, code, password)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(
            f"Login failed: {exc}\n/start to quit and try again.")
        context.user_data.pop("login_slot", None)
        context.user_data.pop("login_code", None)
        return ConversationHandler.END
    return await _login_done(update, context, name)


async def _login_done(update, context, name):
    owner = require_owner(update, context)
    slot = context.user_data.pop("login_slot")
    phone = context.user_data.pop("login_phone", "")
    context.user_data.pop("login_code", None)
    if owner is None:
        return ConversationHandler.END
    v3store.upsert_account(owner, slot, name=name, phone=phone,
                           status="active")
    await update.effective_message.reply_text(
        f"✅ Account {slot} connected successfully ({name}).")
    await show_board(update, context)
    return ConversationHandler.END


async def login_cancel(update, context):
    for k in ("login_slot", "login_phone", "login_code"):
        context.user_data.pop(k, None)
    await show_board(update, context)
    return ConversationHandler.END


def _login_conversation():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(login_entry,
                                           pattern=r"^acct_login:\d+$")],
        states={
            LOGIN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                         login_phone)],
            LOGIN_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                        login_code)],
            LOGIN_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                            login_password)],
        },
        fallbacks=[CommandHandler("start", login_cancel)],
        name="account_login",
    )


# ------------------------------------------------------------ flow builder

# Conversation states
FB_NAME, FB_TARGET, FB_INTERVAL, FB_COUNT, FB_STEP_TYPE, FB_STEP_PARAM = range(6)

STEP_TYPES = [("send", "📤 Send"), ("click", "👆 Click"),
              ("wait", "⏳ Wait"), ("paste", "📋 Paste Worker Name")]


def _draft(context):
    return context.user_data.setdefault("flow_draft", {})


async def _expired(update, context):
    """End a builder conversation whose draft is gone.

    The draft can vanish mid-build (builder-menu Save/Cancel buttons,
    /start clearing user_data, an earlier crash). Recover with a clear
    message instead of a KeyError that wedges the conversation state.
    """
    context.user_data.pop("flow_draft", None)
    await update.effective_message.reply_text(
        "⚠️ Flow builder session expired (the draft was cleared). "
        "Start again from 🧩 Flow Builder.")
    await show_board(update, context)
    return ConversationHandler.END


async def show_builder(update, context):
    buttons = [
        [InlineKeyboardButton("🧩 Create Step-by-Step",
                             callback_data="flow:stepbystep"),
         InlineKeyboardButton("📝 Paste Full Flow",
                             callback_data="flow:paste")],
        [InlineKeyboardButton("👁 Preview", callback_data="flow:preview"),
         InlineKeyboardButton("💾 Save Flow", callback_data="flow:save")],
        [InlineKeyboardButton("❌ Cancel", callback_data="flow:cancel")],
    ]
    buttons += back_button("flow:back")
    await update.effective_message.reply_text(
        "Dynamic Flow Builder\n\n"
        "Paste the complete example below after editing it, "
        "or choose Create Step-by-Step.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _on_builder_cb(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "flow:back":
        await show_board(update, context)
    elif data == "flow:preview":
        d = context.user_data.get("flow_draft")
        if not d or not d.get("steps"):
            await q.message.reply_text("No flow draft yet.")
        else:
            await q.message.reply_text("👁 Preview\n\n" + format_flow(d))
    elif data == "flow:save":
        d = context.user_data.get("flow_draft")
        if not d or not d.get("name") or not d.get("steps"):
            await q.message.reply_text(
                "Nothing to save — build a flow first.")
            return
        if any((s.get("type") or s.get("kind")) == "send"
               and (s.get("text") or "").strip() == "/start"
               for s in d["steps"]):
            await q.message.reply_text(
                "v3 flows do not include /start: start with 📋 Tasks.")
            return
        if not any((s.get("type") or s.get("kind")) == "paste"
                   for s in d["steps"]):
            # A flow with no paste step can never submit an ID — saving
            # it would only let runs bill for nothing.
            await q.message.reply_text(
                "A flow needs at least one 📋 Paste Worker Name step — "
                "otherwise no ID is ever submitted. Add a paste step "
                "and save again.")
            return
        owner = require_owner(update, context)
        if owner is None:
            return
        fid = v3store.save_flow(owner, d["name"], d["target"],
                                d["interval"], d["steps"])
        context.user_data.pop("flow_draft", None)
        await q.message.reply_text(f"✅ Flow saved (ID: {fid}).")
        await show_board(update, context)
    elif data == "flow:cancel":
        context.user_data.pop("flow_draft", None)
        await q.message.reply_text("Flow creation cancelled.")
        await show_board(update, context)


# ------------------------------------------------- step-by-step builder

async def sb_entry(update, context):
    q = update.callback_query
    await q.answer()
    # v3: no pre-seeded step. Flows begin with 📋 Tasks as the first
    # step the user enters; there is no locked /start step.
    context.user_data["flow_draft"] = {"steps": []}
    await q.message.reply_text(
        "New Flow\n\nFlows start with 📋 Tasks — send the first step's "
        "target action via the steps below.\n\n"
        "Send a name for this flow.")
    return FB_NAME


async def sb_name(update, context):
    _draft(context)["name"] = (update.effective_message.text or "").strip()
    await update.effective_message.reply_text(
        "Send the target bot username.\nExample: @YourBot")
    return FB_TARGET


async def sb_target(update, context):
    if "name" not in _draft(context):
        return await _expired(update, context)
    target = (update.effective_message.text or "").strip()
    if not target.startswith("@"):
        target = "@" + target
    _draft(context)["target"] = target
    await update.effective_message.reply_text(
        f"Send the interval in seconds ({v3config.MIN_INTERVAL}-"
        f"{v3config.MAX_INTERVAL}).\nExample: 30")
    return FB_INTERVAL


async def sb_interval(update, context):
    try:
        n = int((update.effective_message.text or "").strip())
    except ValueError:
        n = 0
    if not v3config.MIN_INTERVAL <= n <= v3config.MAX_INTERVAL:
        await update.effective_message.reply_text(
            f"Interval must be {v3config.MIN_INTERVAL}-{v3config.MAX_INTERVAL}. "
            "Try again.")
        return FB_INTERVAL
    if "target" not in _draft(context):
        return await _expired(update, context)
    _draft(context)["interval"] = n
    await update.effective_message.reply_text(
        "How many flow steps do you want? (1-100)")
    return FB_COUNT


async def sb_count(update, context):
    try:
        n = int((update.effective_message.text or "").strip())
    except ValueError:
        n = 0
    if not 1 <= n <= 100:
        await update.effective_message.reply_text(
            "Step count must be 1-100. Try again.")
        return FB_COUNT
    d = _draft(context)
    if "interval" not in d:
        return await _expired(update, context)
    d["wanted_steps"] = n
    d["step_index"] = len(d.get("steps", []))
    if d["step_index"] >= n:
        return await _finish_draft(update, context)
    return await _ask_step_type(update, context)


async def _ask_step_type(update, context):
    d = _draft(context)
    i = d["step_index"] + 1
    total = d["wanted_steps"]
    buttons = [[InlineKeyboardButton(label, callback_data=f"step_type:{key}")]
               for key, label in STEP_TYPES]
    await update.effective_message.reply_text(
        f"Flow Step {i}/{total}\nUse one supported instruction:",
        reply_markup=InlineKeyboardMarkup(buttons))
    return FB_STEP_TYPE


async def sb_step_type(update, context):
    q = update.callback_query
    await q.answer()
    if "wanted_steps" not in _draft(context):
        await q.message.reply_text(
            "⚠️ Flow builder session expired. Start again from "
            "🧩 Flow Builder.")
        return ConversationHandler.END
    stype = q.data.split(":")[1]
    _draft(context)["pending_type"] = stype
    prompts = {
        "send": "Send the text to send to the target bot.",
        "click": "Send the button label to click "
                 "(must match the target bot's button text).",
        "wait": "Send the wait time, e.g. '5' or 'five seconds'.",
        "paste": "Which Excel column? Send A, B, C or D.",
    }
    await q.message.reply_text(prompts[stype])
    return FB_STEP_PARAM


async def sb_step_param(update, context):
    text = (update.effective_message.text or "").strip()
    d = _draft(context)
    stype = d.pop("pending_type", None)
    if stype is None or "wanted_steps" not in d:
        return await _expired(update, context)
    if stype == "send":
        if text == "/start":
            await update.effective_message.reply_text(
                "v3 flows do not include /start: start with 📋 Tasks.")
            d["pending_type"] = stype
            return FB_STEP_PARAM
        step = {"type": "send", "text": text}
    elif stype == "click":
        step = {"type": "click", "label": text}
    elif stype == "wait":
        secs = parse_wait_seconds(text)
        if secs is None or secs <= 0:
            await update.effective_message.reply_text(
                "Could not parse a wait time. Try e.g. '5' or 'five seconds'.")
            d["pending_type"] = stype
            return FB_STEP_PARAM
        step = {"type": "wait", "seconds": secs}
    else:  # paste
        col = {"a": 1, "b": 2, "c": 3, "d": 4}.get(text.lower()[:1])
        if not col:
            await update.effective_message.reply_text(
                "Send A, B, C or D.")
            d["pending_type"] = stype
            return FB_STEP_PARAM
        step = {"type": "paste", "column": col}
    d["steps"].append(step)
    d["step_index"] += 1
    if d["step_index"] < d["wanted_steps"]:
        return await _ask_step_type(update, context)
    return await _finish_draft(update, context)


async def _finish_draft(update, context):
    d = _draft(context)
    d.pop("wanted_steps", None)
    d.pop("step_index", None)
    await update.effective_message.reply_text(
        "All flow steps received.\n\n" + format_flow(d) +
        "\n\nUse Preview / Save Flow from the builder menu.")
    await show_board(update, context)
    return ConversationHandler.END


async def sb_cancel(update, context):
    context.user_data.pop("flow_draft", None)
    await update.effective_message.reply_text("Flow creation cancelled.")
    await show_board(update, context)
    return ConversationHandler.END


def _stepbystep_conversation():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(sb_entry,
                                           pattern=r"^flow:stepbystep$")],
        states={
            FB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, sb_name)],
            FB_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       sb_target)],
            FB_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                         sb_interval)],
            FB_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                      sb_count)],
            FB_STEP_TYPE: [CallbackQueryHandler(sb_step_type,
                                                pattern=r"^step_type:")],
            FB_STEP_PARAM: [
                # No CommandHandler("start", ...) capture here: v3 has
                # no /start step value to capture. A /start command
                # during building falls back to cancelling the draft.
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               sb_step_param)],
        },
        fallbacks=[CommandHandler("start", sb_cancel)],
        name="flow_stepbystep",
    )


# ------------------------------------------------------- paste full flow

FB_PASTE = 100


async def paste_entry(update, context):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(
        "Paste the complete flow block in one message:\n\n"
        "Flow: My Flow\n"
        "Target: @ExampleBot\n"
        "Interval: 30 seconds\n"
        "1. Click: 📋 Tasks\n"
        "2. Wait: two seconds\n"
        "3. Click: 📋 Tasks\n"
        "4. Paste Worker Name\n"
        "5. Paste Worker Name 2\n\n"
        "Supported actions: Send:, Click:, Wait:, Paste Worker Name [2, 3, 4].")
    return FB_PASTE


def parse_flow_block(text):
    """Parse a pasted flow block. Raises ValueError on bad input.

    Never auto-inserts a /start step, and rejects any send step whose
    text is /start: v3 flows start with 📋 Tasks, not /start.
    """
    name = target = None
    interval = None
    steps = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("flow:"):
            name = line.split(":", 1)[1].strip()
        elif low.startswith("target:"):
            target = line.split(":", 1)[1].strip()
            if not target.startswith("@"):
                target = "@" + target
        elif low.startswith("interval:"):
            m = re.search(r"(\d+)", line)
            if m:
                interval = int(m.group(1))
        else:
            m = re.match(r"^\d+[.)]\s*(.+)$", line)
            if not m:
                continue
            body = m.group(1).strip()
            steps.append(_parse_step_line(body))
    if not name:
        raise ValueError("missing 'Flow:' name line")
    if not target:
        raise ValueError("missing 'Target:' line")
    if interval is None:
        raise ValueError("missing 'Interval:' line")
    if not (v3config.MIN_INTERVAL <= interval <= v3config.MAX_INTERVAL):
        raise ValueError(
            f"interval must be {v3config.MIN_INTERVAL}-{v3config.MAX_INTERVAL}")
    if not steps:
        raise ValueError("no numbered steps found")
    if any(s.get("type") == "send"
           and (s.get("text") or "").strip() == "/start" for s in steps):
        raise ValueError("flows start with 📋 Tasks, not /start")
    if not any(s.get("type") == "paste" for s in steps):
        raise ValueError("the block needs at least one "
                         "'Paste Worker Name' step — without it no ID "
                         "is ever submitted")
    return {"name": name, "target": target, "interval": interval,
            "steps": steps}


def _parse_step_line(body):
    low = body.lower()
    if low.startswith("send:"):
        return {"type": "send", "text": body.split(":", 1)[1].strip()}
    if low.startswith("click:"):
        return {"type": "click", "label": body.split(":", 1)[1].strip()}
    if low.startswith("wait"):
        rest = re.sub(r"^wait\s*:?", "", body,
                      flags=re.IGNORECASE).strip()
        secs = parse_wait_seconds(rest)
        if secs is None:
            raise ValueError(f"bad Wait step: {body!r}")
        return {"type": "wait", "seconds": secs}
    m = re.match(r"(?i)^paste\s+worker\s+name\s*(\d*)\s*$", body)
    if m:
        col = int(m.group(1)) if m.group(1) else 1
        if not 1 <= col <= 4:
            raise ValueError(f"paste column out of range: {body!r}")
        return {"type": "paste", "column": col}
    raise ValueError(f"unknown step: {body!r}")


async def paste_block(update, context):
    text = update.effective_message.text or ""
    try:
        draft = parse_flow_block(text)
    except ValueError as exc:
        await update.effective_message.reply_text(
            f"Could not parse that block: {exc}\n"
            "Paste the block again, or /start to quit.")
        return FB_PASTE
    context.user_data["flow_draft"] = draft
    await update.effective_message.reply_text(
        "Flow parsed.\n\n" + format_flow(draft) +
        "\n\nUse Preview / Save Flow from the builder menu.")
    await show_board(update, context)
    return ConversationHandler.END


def _paste_conversation():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(paste_entry,
                                           pattern=r"^flow:paste$")],
        states={
            FB_PASTE: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                      paste_block)],
        },
        fallbacks=[CommandHandler("start", sb_cancel)],
        name="flow_paste",
    )


# ------------------------------------------------------------- saved flows

# Conversation states for in-place editing
EDIT_PICK, EDIT_VALUE = range(2)

_COL_LETTERS = {"A": 1, "B": 2, "C": 3, "D": 4}


async def show_saved_flows(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    flows = v3store.list_flows(owner)
    if not flows:
        await update.effective_message.reply_text(
            "No saved flows yet. Build one in 🧩 Flow Builder first.")
        return
    lines = ["📄 SAVED FLOWS\n", "Tap ▶️ to make a flow active, "
             "✏️ Edit to change a step in place, 🗑 to delete."]
    buttons = []
    for f in flows:
        lines.append(f"\n{f['name']} (ID: {f['id']})")
        lines.append(f"Target: {f['target']} | Interval: "
                     f"{f.get('interval', f.get('interval_seconds', ''))}s | "
                     f"Steps: {len(f['steps'])}")
        buttons.append([
            InlineKeyboardButton(f"▶️ {f['name']}",
                                 callback_data=f"saved:{f['id']}"),
            InlineKeyboardButton("✏️ Edit",
                                 callback_data=f"saved:edit:{f['id']}"),
            InlineKeyboardButton("🗑",
                                 callback_data=f"saved:del:{f['id']}")])
    buttons += back_button("saved:back")
    await update.effective_message.reply_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def _on_saved_cb(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "saved:back":
        await show_board(update, context)
        return
    fid = int(q.data.split(":")[1])
    owner = require_owner(update, context)
    if owner is None:
        return
    flow = v3store.get_flow(owner, fid)
    if not flow:
        await q.message.reply_text("Flow not found.")
        return
    v3store.set_active_flow(owner, fid)
    await q.message.reply_text(
        f"✅ Active flow changed to: {flow['name']}\n\n" +
        format_flow(flow) +
        "\n\nThis flow will be used the next time you press 🚀 Run batch.")
    await show_board(update, context)


# ---------------------------------------------------------- in-place edit

async def _edit_entry(update, context):
    q = update.callback_query
    await q.answer()
    owner = require_owner(update, context)
    if owner is None:
        return ConversationHandler.END
    fid = int(q.data.split(":")[2])
    flow = v3store.get_flow(owner, fid)
    if not flow:
        await q.message.reply_text("Flow not found.")
        return ConversationHandler.END
    context.user_data["edit_flow_id"] = fid
    context.user_data["edit_steps"] = list(flow["steps"])
    steps = flow["steps"]
    # v3: no locked first step. Every step is editable, including step 1,
    # because no flow begins with a locked /start step any more.
    buttons = [[InlineKeyboardButton(f"{i + 1}. {step_brief(s)}",
                                     callback_data=f"sedit:{i}")]
               for i, s in enumerate(steps)]
    buttons.append([InlineKeyboardButton("❌ Cancel",
                                          callback_data="sedit:cancel")])
    head = f"✏️ Editing: {flow['name']} (ID: {fid})\n"
    await q.message.reply_text(
        head + "Pick the step you want to change:",
        reply_markup=InlineKeyboardMarkup(buttons))
    return EDIT_PICK


async def _edit_pick(update, context):
    q = update.callback_query
    await q.answer()
    if q.data == "sedit:cancel":
        context.user_data.pop("edit_flow_id", None)
        context.user_data.pop("edit_steps", None)
        await q.message.reply_text("Edit cancelled.")
        await show_board(update, context)
        return ConversationHandler.END
    idx = int(q.data.split(":")[1])
    steps = context.user_data.get("edit_steps") or []
    if not 0 <= idx < len(steps):
        await q.message.reply_text("That step no longer exists.")
        return EDIT_PICK
    context.user_data["edit_idx"] = idx
    step = steps[idx]
    prompts = {
        "send": "Send the new text for this Send step.",
        "click": "Send the new button label for this Click step "
                 "(exact target-bot button text).",
        "wait": "Send the new wait time, e.g. '5' or 'five seconds'.",
        "paste": "Which Excel column? Send A, B, C or D.",
    }
    await q.message.reply_text(
        f"Step {idx + 1}: {step_brief(step)}\n\n" +
        prompts.get(step.get("type") or step.get("kind"),
                    "Send the new value."))
    return EDIT_VALUE


async def _edit_value(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return ConversationHandler.END
    fid = context.user_data.get("edit_flow_id")
    idx = context.user_data.get("edit_idx")
    steps = context.user_data.get("edit_steps")
    if fid is None or idx is None or not steps:
        await update.effective_message.reply_text("Edit session expired.")
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    step = dict(steps[idx])
    t = step.get("type") or step.get("kind")
    if t == "click":
        step["label"] = text
    elif t == "send":
        if text == "/start":
            await update.effective_message.reply_text(
                "v3 flows do not include /start: start with 📋 Tasks.")
            return EDIT_VALUE
        step["text"] = text
    elif t == "wait":
        secs = parse_wait_seconds(text)
        if secs is None:
            await update.effective_message.reply_text(
                "Could not read that wait time. Try e.g. '5' or "
                "'five seconds'.")
            return EDIT_VALUE
        step["seconds"] = secs
    elif t == "paste":
        col = _COL_LETTERS.get(text.upper())
        if col is None and text.isdigit() and 1 <= int(text) <= 4:
            col = int(text)
        if col is None:
            await update.effective_message.reply_text(
                "Send A, B, C or D.")
            return EDIT_VALUE
        step["column"] = col
    else:
        await update.effective_message.reply_text(
            "This step type cannot be edited.")
        return ConversationHandler.END
    steps[idx] = step
    if not v3store.update_flow_steps(owner, fid, steps):
        await update.effective_message.reply_text(
            "Could not save — flow not found.")
        return ConversationHandler.END
    flow = v3store.get_flow(owner, fid)
    context.user_data.pop("edit_flow_id", None)
    context.user_data.pop("edit_steps", None)
    context.user_data.pop("edit_idx", None)
    await update.effective_message.reply_text(
        f"✅ Step {idx + 1} updated in {flow['name']} (ID: {fid}).\n\n" +
        format_flow(flow) +
        "\n\nNo new flow was created — the change applies on the "
        "next Start.")
    await show_board(update, context)
    return ConversationHandler.END


EDIT_CONVERSATION = ConversationHandler(
    entry_points=[CallbackQueryHandler(_edit_entry,
                                       pattern=r"^saved:edit:\d+$")],
    states={
        EDIT_PICK: [CallbackQueryHandler(
            _edit_pick, pattern=r"^sedit:(\d+|cancel)$")],
        EDIT_VALUE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND,
                           _edit_value)],
    },
    fallbacks=[
        # Re-pressing ✏️ Edit while an edit is open restarts the step
        # list instead of sending nothing (the v2 wedge, fixed in v3).
        CallbackQueryHandler(_edit_entry,
                             pattern=r"^saved:edit:\d+$"),
        CallbackQueryHandler(_edit_pick,
                             pattern=r"^sedit:cancel$"),
    ],
    name="saved_edit",
)


async def _on_delete_cb(update, context):
    q = update.callback_query
    await q.answer()
    owner = require_owner(update, context)
    if owner is None:
        return
    parts = q.data.split(":")
    fid = int(parts[2])
    flow = v3store.get_flow(owner, fid)
    if not flow:
        await q.message.reply_text("Flow not found.")
        await show_saved_flows(update, context)
        return
    if parts[1] == "del":
        buttons = [[
            InlineKeyboardButton("🗑 Yes, delete",
                                 callback_data=f"saved:delok:{fid}"),
            InlineKeyboardButton("↩️ Keep",
                                 callback_data="saved:back"),
        ]]
        await q.message.reply_text(
            f"Delete flow '{flow['name']}' (ID: {fid})? "
            "This cannot be undone.",
            reply_markup=InlineKeyboardMarkup(buttons))
        return
    # delok
    v3store.delete_flow(owner, fid)
    await q.message.reply_text(f"🗑 Flow '{flow['name']}' deleted.")
    await show_saved_flows(update, context)


# ---------------------------------------------------------------------- run

TIMER_DATETIME = 0


def _run_context(owner, user_id):
    """Return (flow, batch_id, slots) or (None, missing_reason)."""
    flow = v3store.get_active_flow(owner)
    if not flow:
        return None, "No active flow. Pick one in 📄 Saved Flow first."
    batch_id = resolve_active_batch(owner, user_id)
    if not batch_id:
        return None, "No active batch. Upload an Excel file first."
    progress = v3store.get_progress(batch_id)
    slots = sorted(progress["per_slot"].keys(), key=lambda s: int(s))
    if not slots:
        return None, "The active batch has no rows."
    return (flow, batch_id, slots), None


async def show_start_prompt(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    run_ctx, missing = _run_context(owner, update.effective_user.id)
    if missing:
        await update.effective_message.reply_text(missing)
        return
    flow, _batch_id, slots = run_ctx
    context.user_data["pending_run"] = {
        "flow_id": flow["id"], "batch_id": _batch_id, "slots": slots}
    names = []
    for s in slots:
        acc = v3store.get_account(owner, s)
        names.append(f"Account {s}" + (f" ({acc['name']})"
                                      if acc and acc.get("name") else ""))
    buttons = [
        [InlineKeyboardButton("🔎 Check Start", callback_data="run:check"),
         InlineKeyboardButton("▶️ Start Now", callback_data="run:now")],
        [InlineKeyboardButton("🕒 Timer (Date & Time)",
                             callback_data="run:timer")],
        [InlineKeyboardButton("Cancel", callback_data="run:cancel")],
    ]
    interval = flow.get("interval", flow.get("interval_seconds", ""))
    await update.effective_message.reply_text(
        f"🚀 Run {len(slots)} worker(s)\n"
        f"Flow: {flow['name']} → {flow['target']} "
        f"({interval}s)\n"
        f"Accounts: {', '.join(names)}\n"
        "Choose an option:",
        reply_markup=InlineKeyboardMarkup(buttons))


def _flow_dict(owner, flow_id):
    f = v3store.get_flow(owner, flow_id)
    if not f:
        return None
    return {"id": f["id"], "name": f["name"], "target": f["target"],
            "interval": f.get("interval", f.get("interval_seconds")),
            "steps": f["steps"]}


def _skipped_note(skipped, moved):
    if not skipped:
        return ""
    nums = ", ".join(str(s) for s in sorted(skipped, key=lambda s: int(s)))
    note = (f"\n⚠️ Skipped dead slots: {nums} — relink them in "
            "🔐 Telegram Accounts and they will rejoin the next run.")
    if moved:
        note += (f" Their {moved} pending row(s) were re-dealt to the "
                 "working accounts.")
    return note


async def _on_run_cb(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "run:timer":
        # Owned by the timer conversation (group 0). When that
        # conversation is already active, the tap lands here
        # instead — it must not cancel the pending run, so it is
        # a no-op here (seen live 2026-11-09: a second Timer tap
        # silently discarded the schedule).
        return
    if data == "run:cancel":
        context.user_data.pop("pending_run", None)
        await q.message.reply_text("Start cancelled.")
        await show_board(update, context)
        return
    owner = require_owner(update, context)
    if owner is None:
        return
    pending = context.user_data.get("pending_run")
    if not pending:
        await q.message.reply_text(
            "Nothing to start — open 🚀 Run batch from the menu first.")
        return
    eng = engine(context)
    flow = _flow_dict(owner, pending["flow_id"])
    if flow is None:
        await q.message.reply_text("Flow not found.")
        return
    try:
        if data == "run:check":
            skipped, moved = await eng.start(
                owner=owner, slots=pending["slots"],
                batch_id=pending["batch_id"], flow=flow, mode="check")
            await q.message.reply_text(
                "🔎 Check start enabled\n"
                "The flow re-runs up to the first paste whenever the "
                "target bot's menu changes. As soon as a check "
                "passes, the check stops and the submission starts "
                "automatically — exactly as if you pressed ▶️ Start Now.\n"
                "If the task is not on the target bot's menu, the check "
                "keeps watching (👀 in Status) until it appears.\n"
                "▶️ Start Now or ⏹ Stop ends the check manually.\n"
                "No Excel row will be pasted during the check itself."
                + _skipped_note(skipped, moved))
        elif data == "run:now":
            skipped, moved = await eng.start(
                owner=owner, slots=pending["slots"],
                batch_id=pending["batch_id"], flow=flow, mode="now")
            started = len(pending["slots"]) - len(skipped)
            await q.message.reply_text(
                f"▶️ Started {started} worker(s) "
                f"with flow '{flow['name']}'."
                + _skipped_note(skipped, moved))
    except Exception as exc:  # noqa: BLE001
        await q.message.reply_text(f"Could not start: {exc}")
        return
    context.user_data.pop("pending_run", None)
    await show_board(update, context)


# ------------------------------------------------------------- timer

async def timer_entry(update, context):
    q = update.callback_query
    await q.answer()
    if require_owner(update, context) is None:
        return ConversationHandler.END
    if not context.user_data.get("pending_run"):
        await q.message.reply_text(
            "Nothing to schedule — open 🚀 Run batch from the menu first.")
        return ConversationHandler.END
    await q.message.reply_text(
        "Send the start date and time together:\n"
        "YYYY-MM-DD HH:MM\n"
        "Example: 2026-10-07 18:30\n"
        f"Timezone: Bangladesh ({v3config.TIMEZONE}).")
    return TIMER_DATETIME


async def timer_datetime(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M")
    except ValueError:
        await update.effective_message.reply_text(
            "Use the format YYYY-MM-DD HH:MM, e.g. 2026-10-07 18:30.")
        return TIMER_DATETIME
    aware = naive.replace(tzinfo=ZoneInfo(v3config.TIMEZONE))
    epoch = int(aware.timestamp())
    now = int(datetime.now(tz=ZoneInfo(v3config.TIMEZONE)).timestamp())
    if epoch <= now:
        await update.effective_message.reply_text(
            "That time is in the past. Send a future date and time.")
        return TIMER_DATETIME
    pending = context.user_data.pop("pending_run")
    flow = _flow_dict(owner, pending["flow_id"])
    if flow is None:
        await update.effective_message.reply_text("Flow not found.")
        return ConversationHandler.END
    eng = engine(context)
    try:
        skipped, moved = await eng.start(
            owner=owner, slots=pending["slots"], batch_id=pending["batch_id"],
            flow=flow, mode="timer", scheduled_at=epoch)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(
            f"Could not schedule: {exc}")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        f"🕒 Scheduled '{flow['name']}' for {text} ({v3config.TIMEZONE}).\n"
        "The workers will start automatically at that time."
        + _skipped_note(skipped, moved))
    await show_board(update, context)
    return ConversationHandler.END


async def timer_cancel(update, context):
    context.user_data.pop("pending_run", None)
    await show_board(update, context)
    return ConversationHandler.END


def _timer_conversation():
    return ConversationHandler(
        entry_points=[CallbackQueryHandler(timer_entry,
                                           pattern=r"^run:timer$")],
        states={
            TIMER_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                            timer_datetime)],
        },
        fallbacks=[CommandHandler("start", timer_cancel)],
        name="run_timer",
    )


async def _restore_notify(app, owner, text):
    chat_id = owner_chat_id(owner)
    if chat_id is None:
        return
    try:
        await app.bot.send_message(chat_id, text)
    except Exception:  # noqa: BLE001 - notification is best-effort
        log.exception("restore notice failed for %s", owner)


async def restore_scheduled_runs(app):
    """Re-start timer runs persisted before a restart.

    Runs at boot (main._post_init). A schedule is a commitment:
    it fires even if its time passed while the bot was down, and
    the owner is told it fired late. A row whose flow or batch is
    gone, or whose start fails, is dropped and the owner is told
    why — a bad row must never block boot or silently linger.
    """
    engine = app.bot_data.get("engine")
    if engine is None:
        return
    now = int(datetime.now(tz=ZoneInfo(v3config.TIMEZONE)).timestamp())
    for sched in v3store.list_scheduled_runs():
        owner = sched["owner"]
        try:
            flow_d = _flow_dict(owner, sched["flow_id"])
            batch = v3store.get_batch(sched["batch_id"])
            if flow_d is None or batch is None \
                    or batch.get("owner") != owner:
                v3store.delete_scheduled_run(owner)
                await _restore_notify(
                    app, owner,
                    "🕒 A scheduled run was dropped: its flow or file "
                    "no longer exists.")
                continue
            slots = [int(s) for s in (sched.get("slots") or [])]
            if not slots:
                v3store.delete_scheduled_run(owner)
                continue
            await engine.start(owner=owner, slots=slots,
                               batch_id=sched["batch_id"], flow=flow_d,
                               mode="timer",
                               scheduled_at=sched["scheduled_at"])
            if sched["scheduled_at"] <= now:
                await _restore_notify(
                    app, owner,
                    "🕒 Your scheduled run started late — the bot was "
                    "down at its scheduled time.")
        except Exception as exc:  # noqa: BLE001 - per-run failure
            log.exception("scheduled-run restore failed for %s", owner)
            try:
                v3store.delete_scheduled_run(owner)
            except Exception:  # noqa: BLE001 - best-effort
                pass
            await _restore_notify(
                app, owner,
                f"🕒 A scheduled run could not be restored: {exc}")


# ---------------------------------------------------------------- stop

async def do_stop(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    eng = engine(context)
    try:
        if eng is None:
            raise RuntimeError("no engine configured")
        await eng.stop(owner)
    except Exception as exc:  # noqa: BLE001
        await update.effective_message.reply_text(f"Stop failed: {exc}")
        return
    await update.effective_message.reply_text(
        "⏹ All workers stopped.\nPending names remain in their queues.")
    await show_board(update, context)


# ------------------------------------------------------------------ status

async def show_status(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    user_id = update.effective_user.id
    eng = engine(context)
    status_error = False
    try:
        st = eng.status(owner) if eng is not None else {}
    except Exception:  # noqa: BLE001 - a broken engine read must
        # not hide behind "Worker: Stopped"; it is flagged below.
        log.exception("status read failed for %s", owner)
        st = {}
        status_error = True
    # Progress follows the batch the engine is actually running,
    # not the resolved active batch: after a new upload the
    # pointer moves on while the run still works the old batch,
    # so Status used to show a different batch's numbers than the
    # one running (seen live 2026-11-09).
    batch_id = st.get("batch_id") or resolve_active_batch(owner, user_id)

    tz = ZoneInfo(v3config.TIMEZONE)
    lines = ["📊 DASHBOARD", ""]
    if st.get("active"):
        mode = st.get("mode") or "now"
        if mode == "timer" and st.get("scheduled_at"):
            dt = datetime.fromtimestamp(st["scheduled_at"], tz=tz)
            lines.append(
                f"Worker: Scheduled for {dt.strftime('%Y-%m-%d %H:%M')} "
                f"({v3config.TIMEZONE})")
        elif mode == "check":
            lines.append("Worker: Check mode (dry run)")
        else:
            lines.append("Worker: Running")
        chk = st.get("check")
        if chk and (mode == "check"
                    or chk.get("state") in ("watching", "fired")):
            if chk.get("state") == "watching":
                since_ts = chk.get("since", chk.get("at"))
                if since_ts:
                    since = datetime.fromtimestamp(since_ts, tz=tz)
                    lines.append(
                        f"👀 Check: watching — {chk.get('detail', '')[:120]} "
                        f"(since {since.strftime('%H:%M:%S')}). Submission "
                        "starts automatically when the task appears.")
                else:
                    lines.append(
                        f"👀 Check: watching — {chk.get('detail', '')[:120]}. "
                        "Submission starts automatically when the task appears.")
            elif chk.get("state") == "fired":
                at = datetime.fromtimestamp(chk["at"], tz=tz)
                lines.append("▶️ Check: task appeared — auto-started "
                             f"{at.strftime('%H:%M:%S')}")
            elif chk.get("state") == "passed":
                at = datetime.fromtimestamp(chk["at"], tz=tz)
                lines.append(f"🔎 Check: ✅ passed ({at.strftime('%H:%M:%S')})")
            elif chk.get("state") == "failed":
                lines.append(f"🔎 Check: ❌ {chk.get('detail', '')[:120]}")
            else:
                lines.append("🔎 Check: probing…")
        if st.get("flow_name"):
            lines.append(f"Flow: {st['flow_name']}")
    else:
        lines.append("Worker: Stopped")
        chk = st.get("check")
        if chk and chk.get("state") == "fired":
            at = datetime.fromtimestamp(chk["at"], tz=tz)
            lines.append("▶️ Check: task appeared — auto-started "
                         f"{at.strftime('%H:%M:%S')}")
        sr = st.get("stop_reason")
        if sr:
            at = datetime.fromtimestamp(sr["at"], tz=tz)
            lines.append(f"⏹ Auto-stopped {at.strftime('%H:%M:%S')}: "
                         f"{sr['reason']}")
            lines.append("Unsubmitted rows stay pending — press "
                         "🚀 Run batch again when the task is back on the menu.")
        # A persisted schedule is visible even with no run active
        # (and even after a restart, when the engine holds nothing).
        sched = v3store.get_scheduled_run(owner)
        if sched is not None and sched.get("scheduled_at"):
            dt = datetime.fromtimestamp(sched["scheduled_at"], tz=tz)
            sched_line = (
                f"🕒 Scheduled for {dt.strftime('%Y-%m-%d %H:%M')} "
                f"({v3config.TIMEZONE})")
            sched_flow = v3store.get_flow(owner, sched["flow_id"])
            if sched_flow is not None:
                sched_line += f" — flow '{sched_flow['name']}'"
            lines.append(sched_line)

    if status_error:
        lines.append("⚠️ Could not read the live worker state — the "
                     "numbers below are the saved batch only.")

    if not batch_id:
        lines += ["", "No active batch. Upload an Excel file to begin."]
        await update.effective_message.reply_text("\n".join(lines))
        return

    progress = v3store.get_progress(batch_id)
    lines += ["",
              f"Total: {progress['total']}",
              f"Done: {progress['done']}/{progress['total']}",
              f"Pending: {progress['pending']}"]
    for slot in sorted(progress["per_slot"], key=lambda s: int(s)):
        s = progress["per_slot"][slot]
        acc = v3store.get_account(owner, slot)
        name = acc["name"] if acc and acc.get("name") else f"Account {slot}"
        lines.append(f"Account {slot} ({name}): "
                     f"{s['done']}/{s['total']} done, "
                     f"{s['pending']} pending, "
                     f"{s.get('failed', 0)} failed")
    await update.effective_message.reply_text("\n".join(lines))


# ----------------------------------------------------------------- balance

async def show_balance(update, context):
    owner = require_owner(update, context)
    if owner is None:
        return
    user_id = update.effective_user.id
    if not vbilling.billable(owner):
        await update.effective_message.reply_text(
            "💰 Balance\n\nAdmin account — unlimited, no charges apply.")
        return
    bal = v3store.get_balance(owner)
    tz = ZoneInfo(v3config.TIMEZONE)
    lines = [
        "💰 Balance",
        "",
        f"{vbilling.format_bdt(bal)} (~{vbilling.ids_left(bal)} IDs left)",
        "",
        f"Your Telegram ID: {user_id}",
    ]
    entries = v3store.get_ledger(owner, 5)
    if entries:
        lines += ["", "Recent activity:"]
        for e in entries:
            dt = datetime.fromtimestamp(e["at"], tz=tz).strftime("%d-%m %H:%M")
            sign = "+" if e["delta"] > 0 else ""
            lines.append(
                f"{dt}  {sign}{vbilling.format_bdt(e['delta'])}  "
                f"{kind_label(e['kind'])}")
    if bal < v3config.COST_PER_ROW_POISHA:
        lines += ["", "Top up to submit IDs:"]
        lines.append(
            f"bKash / Nagad (Send Money): {vbilling.BKASH_NAGAD_NUMBER}")
        lines.append(
            f"USDT: Binance Pay → Pay ID {vbilling.BINANCE_PAY_ID}")
        lines.append("Minimum top-up: $1 (≈120–125 BDT, as per current "
                     "rate). Send your TrxID/TxID + phone + Telegram ID "
                     "to the admin.")
    await update.effective_message.reply_text("\n".join(lines))


# --------------------------------------------------------- billing commands

def _is_admin(update):
    user = update.effective_user
    return user is not None and user.id in getattr(v3config, "ADMIN_IDS", set())


async def cmd_credits(update, context):
    """/credits [user_id] — all wallets, or one user's."""
    if not _is_admin(update):
        return
    parts = (update.effective_message.text or "").split()
    if len(parts) == 2 and parts[1].isdigit():
        owner = f"user:{parts[1]}"
        bal = v3store.get_balance(owner)
        await update.effective_message.reply_text(
            f"User {parts[1]}: {vbilling.format_bdt(bal)} "
            f"(~{vbilling.ids_left(bal)} IDs left).")
        return
    wallets = v3store.list_balances()
    if not wallets:
        await update.effective_message.reply_text("No wallets yet.")
        return
    lines = ["💰 Wallets (emptiest first):", ""]
    for w in wallets[:50]:
        uid = w["owner"].split(":", 1)[1] if ":" in w["owner"] \
            else w["owner"]
        lines.append(
            f"{uid}: {vbilling.format_bdt(w['balance'])} "
            f"(~{vbilling.ids_left(w['balance'])} IDs)")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_ledger(update, context):
    """/ledger <user_id> [limit] — money trail for disputes."""
    if not _is_admin(update):
        return
    parts = (update.effective_message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.effective_message.reply_text(
            "Usage: /ledger <user_id> [limit]\n"
            "Example: /ledger 771234567")
        return
    owner = f"user:{parts[1]}"
    limit = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30
    entries = v3store.get_ledger(owner, min(limit, 100))
    if not entries:
        await update.effective_message.reply_text(
            f"No ledger entries for {parts[1]}.")
        return
    tz = ZoneInfo(v3config.TIMEZONE)
    lines = [f"📒 Ledger — {parts[1]} (newest first):", ""]
    for e in entries:
        dt = datetime.fromtimestamp(e["at"], tz=tz).strftime("%d-%m %H:%M")
        sign = "+" if e["delta"] > 0 else ""
        lines.append(
            f"{dt}  {sign}{vbilling.format_bdt(e['delta'])}  "
            f"{kind_label(e['kind'])}  {e['ref']}")
    await update.effective_message.reply_text("\n".join(lines))


# ------------------------------------------------------------ instructions

PAGES = [
    # 1 — overview
    """📘 Bot Instructions (1/9)

Welcome. This bot runs Excel rows on the target bot automatically,
using your own Telegram accounts — no manual typing.

💳 Subscription: every submitted ID costs ৳0.50, deducted only when
an ID is actually submitted — failed IDs are never charged. Your
balance and IDs-left live under the 💰 Balance button; top up
through the admin to keep running. No balance, no Worker Board.

The workflow is always the same four things:
1) Link your Telegram accounts
2) Upload an Excel file
3) Pick a saved flow
4) Run

Each menu below does one of these. The next pages explain every
screen in order. Read page 2 before linking your first account.""",

    # 2 — accounts
    """📘 Bot Instructions (2/9)

🔐 Telegram Accounts

You have 25 account slots. Each slot holds one Telegram account,
and every account works on its own share of the rows.

To link an account:
1. Tap a slot (for example, Account 1)
2. Tap Login
3. Send the phone number in international format (+8801XXXXXXXXX)
4. Send the login code Telegram sends you
5. If the account has two-step verification, send its 2FA password

Your login code and 2FA messages are deleted by the bot as soon as
they are received. Sessions are stored encrypted on this server.

📌 Status shows whether the slot is active or needs re-login.
📌 Disconnect & Remove wipes the slot's session and settings
   completely — use it before handing a slot to another account.""",

    # 3 — upload
    """📘 Bot Instructions (3/9)

📥 Upload Excel

Send an .xlsx or .xlsm file. Two ways to upload:

• Upload Excel 1, 2, 3… — the batch belongs to one account only.
• 🔄 Shared Excel — rows are split round-robin across all active
  accounts. Every row is permanently reserved for one account, so
  no row is ever processed twice.

Rules the bot follows:
• Every worksheet in the file is read, not just the first.
• Row 1 is data — do not add a header row.
• Empty rows are skipped automatically.
• Column A is the main value; B, C, D are extra columns the flow
  can paste (see page 5).

After upload, the batch becomes the active batch. Uploading a new
file replaces it.""",

    # 4 — flow builder
    """📘 Bot Instructions (4/9)

🧩 Flow Builder

A flow is the exact list of actions each account repeats on the
target bot for every row. Two ways to build one:

• 🧩 Create Step-by-Step — the bot asks for name, target bot,
  interval, and number of steps, one by one.
• 📝 Paste Full Flow — paste the whole block in one message:

Flow: My Flow
Target: @ExampleBot
Interval: 27 seconds
1. Click: 📋 Tasks
2. Paste Worker Name

Every flow opens with 📋 Tasks. v3 flows never send /start to the
target bot — the task menu is the first step, and you build from
there.

💾 After building, use 👁 Preview to check the steps, then
💾 Save Flow.""",

    # 5 — step types
    """📘 Bot Instructions (5/9)

🧩 Flow Builder — the four step types

📤 Send: — send a text message to the target bot,
   for example Send: hello
👆 Click: — tap a button on the target bot. Type the button's
   EXACT text, including emoji and price, e.g.:
   Click: 🌟 Create FB (No mail) ($0.0300)
   If the target bot changes a price, update this text to match —
   the bot clicks exactly what is written here.
⏳ Wait: — pause N seconds before the next step, e.g. Wait: 1
📋 Paste Worker Name — paste a value from the Excel row:
   Paste Worker Name = column A
   Paste Worker Name 2 = column B
   Paste Worker Name 3 = column C
   Paste Worker Name 4 = column D

The interval (1–3600 seconds) is the pause between rows for each
account, and you set it when you build the flow. A 27-second
interval processes roughly 130 rows per hour per account; more
accounts means more rows per hour at the same interval.""",

    # 6 — saved flows
    """📘 Bot Instructions (6/9)

📄 Saved Flow

Every saved flow shows its target bot, interval, and steps.

▶️ Activate — make a flow active for the next run.
✏️ Edit — change one step in place; the same flow is updated, no
   new copy is created. Every step can be edited, including the
   first one. To change a Click step, send the new exact button
   text.
🗑 Delete — remove a flow (the bot asks you to confirm first).

Your bot arrives with two ready flows for @Taskl1_bot:
• COOKIES NO MAIL — pastes column A
• 2FA COOKIES — pastes column B (2FA key), then column A

Both start with 📋 Tasks. Tip: when a price in @Taskl1_bot
changes, ✏️ Edit the Click step to the new label instead of
building a new flow.""",

    # 7 — start
    """📘 Bot Instructions (7/9)

🚀 Run batch

Before a run, the bot checks your accounts (an initial precheck)
so a dead account is caught before rows are spent.

Three ways to begin:
• 🔎 Check Start — watches the target bot and probes it, and
  re-checks whenever the target bot's menu changes. The moment
  the task appears, checking stops and the run starts
  automatically — exactly as if you had pressed ▶️ Start Now.
  Nothing is submitted while checking, so it costs nothing.
• ▶️ Start Now — begin processing immediately.
• 🕒 Timer — schedule the start: send YYYY-MM-DD HH:MM
  (for example 2026-10-07 18:30). Times are Asia/Dhaka.

One row is always processed by exactly one account — rows are
never shared or repeated between accounts.""",

    # 8 — stop / status / resume
    """📘 Bot Instructions (8/9)

⏹ Stop and 📊 Status

⏹ Stop halts every worker. Pending rows stay queued — nothing is
deleted. Press 🚀 Run batch again later and the run resumes from
where it stopped.

📊 Status shows the live dashboard:
• Worker: Running / Stopped
• Per account: Total / Done / Pending / Failed
• Overall progress for the active batch

💰 Balance (its own button) shows your wallet: current balance,
IDs left, recent activity, and your Telegram ID for top-ups.

If an account is kicked or its session dies mid-run, only that
account stops; its current row fails and its remaining rows stay
pending. Re-link that slot and press 🚀 Run batch to continue.""",

    # 9 — daily cycle / good practice
    """📘 Bot Instructions (9/9)

Daily cycle & good practice

• The daily cycle follows the Bangladesh calendar day
  (Asia/Dhaka). Plan big batches around it.
• A 27-second interval processes roughly 130 rows per hour per
  account; more accounts means more rows per hour at the same
  interval.
• Run 🔎 Check Start before a long batch — it costs nothing and
  proves the target bot is live.
• If a target-bot button label or price changes, ✏️ Edit the
  step (page 6) instead of rebuilding the flow.
• Top up before a big batch: if the balance runs out mid-run, the
  run stops on its own and pending rows wait — top up and press
  🚀 Run batch to resume. The bot warns you at ~50 and ~10 IDs left.
• Your 💰 Balance ledger records every top-up and every per-ID
  charge — the full money trail if you ever question a charge.
• Keep at least one spare account slot empty, so a kicked
  account can be replaced without stopping the whole run.""",
]


def _nav(page, gate=False):
    """Page nav; gate=True adds a ⬅️ Back row returning to the paywall."""
    prefix = "gins" if gate else "ins"
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(
            "◀️ Prev", callback_data=f"{prefix}:{page - 1}"))
    row.append(InlineKeyboardButton(
        f"{page + 1}/{len(PAGES)}", callback_data=f"{prefix}:noop"))
    if page < len(PAGES) - 1:
        row.append(InlineKeyboardButton(
            "Next ▶️", callback_data=f"{prefix}:{page + 1}"))
    rows = [row]
    if gate:
        rows.append([InlineKeyboardButton("⬅️ Back",
                                          callback_data="gate:back")])
    return InlineKeyboardMarkup(rows)


async def show_instructions(update, context, gate=False):
    if gate and update.callback_query is not None:
        # In-place: the paywall message becomes page 1.
        await update.callback_query.message.edit_text(
            PAGES[0], reply_markup=_nav(0, gate=True))
        return
    await update.effective_message.reply_text(
        PAGES[0], reply_markup=_nav(0))


async def _on_ins_page(update, context):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    gate = data.startswith("gins:")
    if data in ("ins:noop", "gins:noop"):
        return
    try:
        page = int(data.split(":")[1])
    except (IndexError, ValueError):
        return
    if not 0 <= page < len(PAGES):
        return
    await q.message.edit_text(PAGES[page],
                              reply_markup=_nav(page, gate=gate))


# ------------------------------------------------------------- registration

_ROUTES.update({
    M_UPLOAD: show_upload_menu,
    M_ACCOUNTS: show_accounts,
    M_BUILDER: show_builder,
    M_SAVED: show_saved_flows,
    M_START: show_start_prompt,
    M_STOP: do_stop,
    M_STATUS: show_status,
    M_INSTRUCTIONS: show_instructions,
    M_BALANCE: show_balance,
})


def build_application(bot_token, db_path=None, engine=None,
                      post_init=None, post_stop=None, request=None,
                      target_factory=None):
    """Build the PTB application. The token is held by the caller,
    never printed or stored by this module.

    db_path: when given, the store path is set and the DB created.
    engine: when given, that Engine instance is used; otherwise a
    fresh Engine() is built. post_init/post_stop/request are passed
    through to the ApplicationBuilder (used by v3.main for the
    watchdog, shutdown, and the proxied HTTPX request).
    target_factory is accepted for backward compatibility and ignored:
    v3 workers are built by the Engine itself, not by a factory.
    """
    if db_path is not None:
        v3store.set_db_path(db_path)
        v3store.init_db()
    if engine is None:
        from v3.engine import Engine
        engine = Engine()
    builder = ApplicationBuilder().token(bot_token)
    if request is not None:
        builder = builder.request(request)
    if post_init is not None:
        builder = builder.post_init(post_init)
    if post_stop is not None:
        builder = builder.post_stop(post_stop)
    app = builder.build()
    app.bot_data["engine"] = engine

    async def _notify(owner, text):
        chat_id = owner_chat_id(owner)
        if chat_id is None:
            return
        try:
            await app.bot.send_message(chat_id, text)
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass

    async def _revert(owner):
        await maybe_revert_idle_owner(app.bot, owner, engine)

    if hasattr(engine, "set_notifier"):
        engine.set_notifier(_notify)
    if hasattr(engine, "set_revert_handler"):
        engine.set_revert_handler(_revert)

    # Group 0: conversations win over the group-1 menu router, so a
    # message carrying a step value, a login code, or a timer time is
    # never swallowed by the board menu. The timer conversation
    # goes first: a date-time text must reach it even when another
    # conversation was left open, or the schedule is silently lost
    # (seen live 2026-11-09). A timer text matches nothing when
    # the timer conversation is inactive, so the order costs the
    # other conversations nothing.
    app.add_handler(_timer_conversation(), group=0)
    app.add_handler(_stepbystep_conversation(), group=0)
    app.add_handler(_paste_conversation(), group=0)
    app.add_handler(EDIT_CONVERSATION, group=0)
    app.add_handler(_login_conversation(), group=0)

    # Group 1: everything else.
    app.add_handler(CommandHandler("start", cmd_start), group=1)
    app.add_handler(CommandHandler("credits", cmd_credits), group=1)
    app.add_handler(CommandHandler("ledger", cmd_ledger), group=1)
    app.add_handler(CallbackQueryHandler(on_gate_callback,
                                         pattern=r"^gate:"), group=1)
    app.add_handler(CallbackQueryHandler(_on_upload_choice,
                                         pattern=r"^up:"), group=1)
    app.add_handler(MessageHandler(filters.Document.ALL, on_document),
                    group=1)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & ~filters.Text(_MENU_TEXTS),
            _on_upload_text),
        group=1)
    app.add_handler(CallbackQueryHandler(_on_account_cb,
                                         pattern=r"^acct:\d+$|^acct:back$"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_slot_status,
                                         pattern=r"^acct_status:\d+$"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_unlink,
                                         pattern=r"^acct_unlink:\d+$"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_unlink_yes,
                                         pattern=r"^acct_unlink_yes:\d+$"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_builder_cb,
                                         pattern=r"^flow:"), group=1)
    app.add_handler(CallbackQueryHandler(_on_saved_cb,
                                         pattern=r"^saved:(\d+|back)$"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_delete_cb,
                                         pattern=r"^saved:(del|delok):\d+$"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_run_cb, pattern=r"^run:"),
                    group=1)
    app.add_handler(CallbackQueryHandler(_on_ins_page,
                                         pattern=r"^ins:"), group=1)
    app.add_handler(CallbackQueryHandler(_on_ins_page,
                                         pattern=r"^gins:"), group=1)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, route_menu), group=1
    )
    return app
