# Tectonic Engine v3 — rebuilt to Zara v2 parity

Full rebuild from scratch (see `REBUILD_SPEC.md`, written from a
complete read of Zara v2's code). Zara v2 stays live on Oracle until
v3 is tested and Operator orders a switch. V3 never touches Zara's
service, directory, database, or token.

## Job (same as v2)
- Upload an Excel sheet (row 1 is data, no header, columns A–D,
  all worksheets, `.xlsx`/`.xlsm` only), per-account or shared
  round-robin.
- Define editable flows: send / click / wait / paste steps, built
  step-by-step or pasted as a block, listed/activated/edited/deleted
  in Saved Flows.
- Run a batch now, on a timer, or via Check Start (watch the target
  menu, auto-start when the task appears), across up to 25 linked
  worker accounts, with dead-slot skip and pending redistribution.
- Billing: ৳0.50 per submitted ID, reserve-upfront with refund,
  wallets, ledger, `/credit` / `/credits` / `/ledger`, subscription
  gate with Worker Board, low-balance warnings, auto-paywall-revert
  at the zero-slot choke point only.
- Status dashboard, instructions (9 pages), board/paywall message
  bookkeeping, active-batch persistence across restarts.

## Rules that define v3 (Operator, 2026-11-01)
1. **No `/start` anywhere, ever.** Flows start directly with 📋 Tasks
   (rejected at save and at run), and the engine itself never sends
   `/start` either. When a run's or check's first step is 📋 Tasks
   and a fresh read finds no 📋 Tasks button, the engine sends
   📋 Tasks once as a recovery — never a flow step, never saved,
   never validated as one — waits for its reply, and re-checks.
   Run mode: still missing stops the whole run. Check mode: still
   missing records watching and retries on the check cadence.
2. **No rate-limit handling at all.** No rotation, no muting, no
   parking, no retries, no probe rotation, no backoff — and no
   special fail-stop branch either. A rate-limit error is treated
   exactly like any other error (in a run it fails the row if
   unsubmitted; in a check probe it is recorded as failed and the
   probe continues). The bypass is not sending `/start` for every
   row — not any handling built around the limit.
3. **Speed first.** As many reads, polling, and streaming as give
   speed, even if not strictly needed. No pacing or throttling out
   of fear of a limit. No jitter. The one step pause is the
   user-ordered 1 s inter-step pause (rule 4), not a limit fear.
4. **Timings: the flow interval, the 12 s guard wait cap, the
   1 s inter-step pause, and the 60 s check cadence.** The submission timing is the per-flow
   interval, set through the bot, slept between rows only — plus
   the user-ordered activity-triggered guard wait: on a missed
   read the guard rescans on every new incoming message or edit
   until the button appears or 12 s (`GUARD_WAIT_SECONDS`) expires
   (run mode only, replacing the old fixed 3 s reread) —
   plus the user-ordered 1 second pause after every flow step
   except the last one (`STEP_PAUSE_SECONDS`, hardcoded, not
   configurable through the bot). The pause covers every step
   type, pastes included; a wait step keeps its own duration and
   still gets the 1 s after it, and a flow with no wait step still
   gets the 1 s. It runs in the run loop and the check-probe loop.
   Run-mode reply waits are bounded at 45 s
   (`WAIT_REPLY_TIMEOUT_SECONDS`): a reply that never arrives
   fails the row and the runner proceeds to the next row, other
   slots unaffected — a hang never freezes a slot. No click pause.
   A Stop cancels a wait. Check mode is the exception: a watching
   or failed probe re-runs every 60 s
   (`CHECK_PROBE_WAIT_SECONDS`, Zara's cadence), and a single probe
   is capped at 60 s (`CHECK_PROBE_TIMEOUT_SECONDS`), so a reply
   that never arrives records failed instead of freezing the
   check. History polling on a 1 s cadence is the only read
   cadence, used when the stream is unavailable.
5. **Reads + streaming, both, for speed.** After every send, read
   history immediately. Stream handlers (message + edited, no chat
   filter) wake waits. History is the source of truth; an event is
   only a wakeup. The guard's menu scan is not limited to the
   recent window: a reply keyboard persists as chat state, so
   the stream handlers (target chat + incoming only) record the
   current panel live — the newest incoming message carrying a
   reply keyboard — and the guard checks that recorded panel
   after the window misses, with no scan back. History stays at
   10 messages (v2's window) for the window scan, cold-start
   priming, and the dead-stream poll floor. Older superseded
   panels are never stored: every new panel message overwrites.
6. **No UI label collision.** v3's own reply-keyboard labels never
   equal a target-bot label — in particular v3 never uses
   `▶️ Start` as a menu label (it uses 🚀 Run batch), so sending
   `▶️ Start` as a flow step during flow creation is recorded as
   a step, never intercepted. Inline buttons (▶️ Start Now,
   Saved-Flows ▶️) are exempt: they route on callback data.
7. **Strict-exact matching.** A button matches only if, after
   emoji/punctuation/case normalisation, it equals the flow label
   exactly. Ambiguous or missing guarded pre-paste options stop
   the slot with rows pending (one push per distinct reason).

## Layout
- `v3/config.py` — env-only secrets (`V3_` prefix), timezone
  Asia/Dhaka, interval bounds, billing constants. No other time
  constants exist in this module by design.
- `v3/billing.py` — billing policy (bKash/Nagad, Binance Pay,
  ৳0.50/ID, gate and top-up texts).
- `v3/store.py` — SQLite: accounts, flows, batches, queue,
  active batches, credits, ledger, gate/board messages.
- `v3/sheet.py` — sheet parser, no header assumed.
- `v3/target.py` — target interface + no-timeout stream waiter.
- `v3/pyrogram_target.py` — Pyrogram user-client adapter
  (reply + inline buttons, stream handlers, callback clicks).
- `v3/engine.py` — FlowSession + Engine: linking, multi-slot
  runs, check mode + auto-fire, timer, billing, stop/shutdown.
- `v3/bot.py` — UI labels, formatting, wait parsing.
- `v3/control_bot.py` — PTB handlers: board, gate, upload,
  accounts + login, flow builder, saved flows + edit, run + timer,
  status, balance, billing commands, instructions.
- `v3/main.py` — entry point, `.env` only, exit 2 without
  `V3_BOT_TOKEN`, systemd watchdog, `drop_pending_updates`.
- `REBUILD_SPEC.md` — the rebuild contract, with v2 citations
  and the decisions left for the operator's word.

## Run tests
```
~/workspace/tectonic-engine-v2/.venv/bin/python tests/test_v3.py
~/workspace/tectonic-engine-v2/.venv/bin/python tests/test_v3_phase2.py
~/workspace/tectonic-engine-v2/.venv/bin/python tests/test_v3_store.py
~/workspace/tectonic-engine-v2/.venv/bin/python tests/test_v3_engine.py
~/workspace/tectonic-engine-v2/.venv/bin/python tests/test_v3_handlers.py
```
System python lacks `telegram`/`pyrogram`; use the venv interpreter
above. Tests use temp DBs and fake targets — no network, no login.
Note for local runs: unset the proxy env vars (`http_proxy`,
`https_proxy`, `NO_PROXY`, …) before building a PTB application —
this VM's `NO_PROXY` breaks httpx URL parsing at build time.

## What is still NOT live-proven (honest gap list)
- Worker account linking for v3 (no Pyrogram API id/hash or worker
  sessions supplied yet) and therefore no live target-bot run
  end-to-end (engine tested against fake targets only).
- Owner lock removed 2026-11-01 on the operator's order (not needed);
  the bot is open by design, gate/board by the billing rules.
- Deploy: code only to Oracle `~/tectonic-engine-v3/`, restart
  only service `tectonic-v3`; token stays only in its `.env`
  (mode 600), never in chat, logs, or memory.
