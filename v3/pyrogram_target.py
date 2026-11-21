"""v3 Pyrogram target adapter. Fresh implementation, no v2 code.

Implements the Target interface from v3.target using a Pyrogram
user client (a worker account), NOT a bot client: a bot cannot
message another bot, so submission goes through a user session,
exactly as v2 did. The client, session path, API id and hash are
supplied by the caller from environment / session files. This file
never reads, prints, or stores a bot token, an API hash, or a
session string.

Buttons: target bots use reply keyboards (plain strings, or objects
with a .text attribute) and inline keyboards (objects with .text
and .callback_data). Both shapes are normalised here.

Stream: the adapter attaches a message handler and an
edited-message handler, both filtered to the target chat and to
incoming messages only, so a wakeup means the target bot sent
something — never the worker's own sends, never another chat.
Either handler wakes the StreamWaiter AND records live state:
the newest incoming message id, and the current reply-keyboard
panel (the newest incoming message carrying a reply keyboard;
a keyboard-removal message clears it). The panel is chat state,
not history archaeology: the engine checks the recorded panel
instead of scanning back. History is still re-read after every
wakeup and remains the source of truth for replies; it also
primes the panel on cold start.
"""

def _button_entries(reply_markup):
    """Button entries from reply or inline keyboard markup.

    Each entry is {"text": str, "callback_data": str | None}.
    Reply-keyboard buttons have no callback data; inline buttons
    carry it. Empty-text buttons are dropped.
    """
    if reply_markup is None:
        return []
    out = []
    rows = getattr(reply_markup, "keyboard", None)
    if rows is None:
        rows = getattr(reply_markup, "inline_keyboard", []) or []
    for row in rows or []:
        for button in row or []:
            if isinstance(button, str):
                out.append({"text": button, "callback_data": None})
            else:
                text = getattr(button, "text", "") or ""
                if text:
                    out.append({
                        "text": text,
                        "callback_data": getattr(
                            button, "callback_data", None) or None,
                    })
    return out


def _normalise_panel(message):
    """Normalised dict for a panel-setting message, as in history()."""
    edit_date = getattr(message, "edit_date", None)
    if edit_date is not None:
        try:
            edit_date = int(edit_date.timestamp())
        except AttributeError:
            edit_date = int(edit_date)
    return {
        "id": getattr(message, "id", None),
        "text": message.text or message.caption or "",
        "buttons": _button_entries(message.reply_markup),
        "incoming": not getattr(message, "out", False),
        "edit_date": edit_date,
        "reply_to": getattr(message, "reply_to_message_id", None),
    }


def _is_reply_keyboard(message):
    """True when the message sets a reply keyboard (not a removal)."""
    markup = getattr(message, "reply_markup", None)
    return markup is not None and getattr(markup, "keyboard", None)


def _is_keyboard_removal(message):
    markup = getattr(message, "reply_markup", None)
    return markup is not None \
        and getattr(markup, "remove_keyboard", False)


class PyrogramTarget:
    def __init__(self, client, target_username, waiter=None):
        self.client = client
        self.target = target_username
        self.waiter = waiter
        # Live state recorded by the stream handlers (and primed
        # by history()): the current reply-keyboard panel, the
        # newest incoming message id, and our last sent id for
        # reply-thread correlation. All generic: the target is
        # whatever username the flow points at, never a fixed bot.
        self.panel_message = None
        self.last_incoming_id = None
        self.last_sent_id = None
        self._stream_handlers = []

    def attach_stream(self):
        """Attach stream handlers. Call once, before running a batch.

        Exactly two handlers are registered: one for new messages,
        one for edited messages, both filtered to the target chat
        and to incoming messages only. Each handler wakes the
        StreamWaiter and records live state: the newest incoming
        id, and the current panel on a reply-keyboard message
        (cleared on a keyboard-removal message).
        """
        from pyrogram import filters
        from pyrogram.handlers import EditedMessageHandler, MessageHandler

        async def _observe(_client, message):
            if self.waiter is not None:
                self.waiter.wake()
            if getattr(message, "out", False):
                return
            mid = getattr(message, "id", None)
            if mid is not None:
                self.last_incoming_id = mid
            if _is_keyboard_removal(message):
                self.panel_message = None
            elif _is_reply_keyboard(message):
                self.panel_message = _normalise_panel(message)

        chat_incoming = filters.chat(self.target) & filters.incoming
        msg_handler = MessageHandler(_observe, chat_incoming)
        edit_handler = EditedMessageHandler(_observe, chat_incoming)
        self.client.add_handler(msg_handler)
        self.client.add_handler(edit_handler)
        self._stream_handlers = [msg_handler, edit_handler]

    def detach_stream(self):
        """Remove handlers registered by attach_stream, if any.

        Used when the slot's target changes: the old handlers are
        filtered to the old target and must not linger on the
        client once a fresh adapter takes over.
        """
        for handler in getattr(self, "_stream_handlers", None) or []:
            try:
                self.client.remove_handler(handler)
            except Exception:  # noqa: BLE001 - detach is best-effort
                pass
        self._stream_handlers = []

    async def send(self, text):
        message = await self.client.send_message(self.target, text)
        try:
            self.last_sent_id = message.id
        except Exception:  # noqa: BLE001 - id is best-effort
            pass

    async def callback(self, msg_id, data, text=None):
        """Answer an inline button press, falling back to its text.

        The raw callback answer is tried first; on any failure the
        button's own text is sent as a message instead, so the step
        still advances. Returns normally in both cases.
        """
        try:
            from pyrogram.raw.functions.messages import GetBotCallbackAnswer

            peer = await self.client.resolve_peer(self.target)
            await self.client.invoke(
                GetBotCallbackAnswer(peer=peer, msg_id=msg_id, data=data)
            )
            return
        except Exception:
            pass
        await self.client.send_message(self.target, text or data)

    async def history(self, limit=10):
        messages = []
        newest_incoming_was_removal = False
        seen_newest_incoming = False
        async for message in self.client.get_chat_history(
                self.target, limit=limit):
            edit_date = getattr(message, "edit_date", None)
            if edit_date is not None:
                try:
                    edit_date = int(edit_date.timestamp())
                except AttributeError:
                    edit_date = int(edit_date)
            incoming = not getattr(message, "out", False)
            if incoming and not seen_newest_incoming:
                seen_newest_incoming = True
                newest_incoming_was_removal = \
                    _is_keyboard_removal(message)
            messages.append({
                "id": getattr(message, "id", None),
                "text": message.text or message.caption or "",
                "buttons": _button_entries(message.reply_markup),
                "incoming": incoming,
                "edit_date": edit_date,
                "reply_to": getattr(
                    message, "reply_to_message_id", None),
            })
        # Cold-start priming (newest-first batch): the newest
        # incoming id, and the newest reply-keyboard panel — but
        # only when newer than what the stream already recorded.
        # A newest-incoming removal clears the panel instead.
        if newest_incoming_was_removal:
            self.panel_message = None
        else:
            for m in messages:
                if not m["incoming"]:
                    continue
                if m["id"] is not None and (
                        self.last_incoming_id is None
                        or m["id"] > self.last_incoming_id):
                    self.last_incoming_id = m["id"]
                    break
            for m in messages:
                if not m["incoming"]:
                    continue
                buttons = m["buttons"]
                if not buttons or not all(
                        b.get("callback_data") is None
                        for b in buttons):
                    continue
                stored = self.panel_message
                if stored is None or (
                        stored.get("id") is not None
                        and m["id"] is not None
                        and m["id"] > stored["id"]):
                    self.panel_message = m
                break
        return messages

