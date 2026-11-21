"""Target-bot client interface for v3.

A Target is the worker account's view of the target bot: it can send
text and read history. History entries are dicts, newest-first:

    {"id": int | None, "text": str, "buttons": [...],
     "incoming": bool, "edit_date": int | None}

Buttons in a history entry are plain strings or dicts of the form
{"text": str, "callback_data": str | None}; the engine normalises
both shapes. Older tuple-style entries (text, buttons) are also
accepted by the engine's entry normaliser for compatibility with
early fakes.

Speed rules, both deliberate:
- After every send, read history immediately. A read is cheap and
  it is what makes the next step fast.
- Waiting uses a stream event with NO overall limit. Stream
  handlers (message + edited, attached by the real adapter) set
  the event; history is then re-read and is the source of truth.
  An event is only a wakeup, never the answer itself. When the
  stream is unavailable, waiting degrades to polling history on
  a 1 second cadence (the only read cadence in v3).
"""

import asyncio


class StreamWaiter:
    """Stream wakeup for waits: set on a new message or edit,
    drained before each wait so a stale wakeup can never satisfy
    it. History re-reads after every wakeup are the source of
    truth; the engine's reply cursor, not this waiter, decides
    what is new.
    """

    def __init__(self, target):
        self.target = target
        self.event = asyncio.Event()

    def wake(self):
        self.event.set()

    def drain(self):
        """Drop wakeups that predate the coming wait.

        A probe's own sends set the event; without draining, the
        post-probe wait would return instantly on those stale
        wakeups and the check loop would spin without ever
        suspending — starving every other task on the loop
        (a spinning task awaiting only already-resolved futures
        never lets the loop's timers fire).
        """
        self.event.clear()

    async def wait_event(self):
        """Wait up to 1 second for a stream wakeup.

        Returns True only when the event fired; False on the 1 second
        cadence expiry. The caller loops, so a dead stream degrades
        to history polling rather than blocking forever. This 1 second
        cadence is a read cadence, not a submission limit.
        """
        try:
            await asyncio.wait_for(self.event.wait(), timeout=1)
        except asyncio.TimeoutError:
            return False
        self.event.clear()
        return True


