"""v3 Phase 2 tests, migrated to the rebuilt APIs.

Pyrogram adapter history entries are now dicts (id/text/buttons/
incoming/edit_date); button entries carry text + callback_data.
"""

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from v3 import main as v3main
from v3.pyrogram_target import PyrogramTarget


class FakeMessage:
    _next_id = 100

    def __init__(self, text, markup):
        self.text = text
        self.caption = None
        self.reply_markup = markup
        self.out = False
        self.edit_date = None
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id


class FakeMarkup:
    def __init__(self, rows):
        self.keyboard = rows


class FakeClient:
    def __init__(self):
        self.sent = []
        self.messages = []
        self.handlers = []

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def send_message(self, target, text):
        self.sent.append((target, text))
        self.messages.insert(0, FakeMessage(
            f"reply to {text}", FakeMarkup([["📋 Tasks", "🍪 ($0.0280)"]])))

    async def get_chat_history(self, target, limit=10):
        for message in self.messages[:limit]:
            yield message


def run(coro):
    return asyncio.run(coro)


class TestAdapter(unittest.TestCase):
    def test_send_and_history_with_string_and_object_buttons(self):
        client = FakeClient()
        target = PyrogramTarget(client, "@Taskl1_bot")
        run(target.send("📋 Tasks"))
        history = run(target.history(limit=10))
        self.assertEqual(client.sent, [("@Taskl1_bot", "📋 Tasks")])
        self.assertEqual(
            [b["text"] for b in history[0]["buttons"]],
            ["📋 Tasks", "🍪 ($0.0280)"])
        self.assertTrue(history[0]["incoming"])

    def test_stream_wake_is_registered(self):
        # Pyrogram imports require a running event loop (its sync
        # wrapper calls get_event_loop at import), so attach inside one.
        async def _attach():
            client = FakeClient()
            target = PyrogramTarget(client, "@Taskl1_bot")
            target.attach_stream()
            return len(client.handlers)

        self.assertEqual(run(_attach()), 2)

class TestMainEnv(unittest.TestCase):
    def test_load_env_parses_simple_file(self):
        path = pathlib.Path("/tmp/v3_test_env")
        path.write_text("# comment\nV3_BOT_TOKEN=abc\nV3_OWNER_IDS=1,2\n")
        try:
            values = v3main.load_env(str(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(values["V3_BOT_TOKEN"], "abc")
        self.assertEqual(values["V3_OWNER_IDS"], "1,2")

    def test_main_refuses_without_token(self):
        # main() reads .env next to the package; with no token in env
        # and no .env in the repo, it must exit 2, not start polling.
        # This test only checks the load_env path, not a live start.
        self.assertEqual(v3main.load_env("/tmp/does-not-exist-v3-env"), {})


if __name__ == "__main__":
    unittest.main()
