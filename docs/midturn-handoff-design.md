# Mid-turn handoff design r3: daemon probe + forced prompt

> Design only; no code changed by this document.
> Once implemented it is merged into `docs/design.md` as **§34**. It reuses the §4 rotation flow, the §3 generation naming slot.N, the three-section handoff note + machine skeleton, and the history page; it does not invent a second rotation mechanism.
> r1 / r2 (A/B two layers, CLI hooks, usage frames, P1–P8 probe scripts) were dropped; they are kept in §9 "Rejected".

## Requirements (basis of this design)

1. Every mode must be able to auto-handoff mid-turn; this is basic functionality.
2. Keep it simple: the daemon runs one probe and drives every session window; for every worker CLI (Claude Code, Codex, pi) it forces the auto-handoff prompt.
3. Mid-turn threshold 80% (`midturn_ratio` default 0.8).
4. Probe loop interval = max(1 s, time to scan all windows).

Requirements 1 and 2 overturn the first line of `pi_extension_v2.ts` ("agent_settled only reports; it never requests continuation") and design.md §32 ("no rotation in the middle of a long turn"); §34 states this explicitly.

---

## 0. Conclusions

1. **Root cause**: rotation is decided in only two places — `Fleet.deliver()` (`fleet.py:3958-3976`) and §32 `Fleet.idle_rotate()` (`fleet.py:3789-3809`), both of which require the slot to be `idle`; nobody watches usage while a turn is running. pi usage also reaches Redis only at startup / settled (`Fleet.pi_metadata`, `fleet.py:3151`), so while analyst-1 grew to 186,673 within one turn (measured from the pi session file), status kept showing 4,323.
2. **The probe is the daemon itself**: a separate thread loops with interval = max(1 s, time this round took to scan all busy slots); each round looks only at busy slots; first `stat` (mtime, size), skip if unchanged, otherwise read only the tail ≤ 64KB; pi reads `pi-sessions/<slot>/g<N>/*.jsonl`, Claude reads the transcript, Codex reads the rollout. No usage frames, no CLI hooks, no probe scripts.
3. **Forced prompt**: usage ≥ 0.8 × window → the daemon bypasses the delivery gate and pastes one fixed auto-handoff prompt line straight into that slot's window, then presses Enter. Once per generation.
4. **Turn settlement**: when that turn ends, if the message carries the mid-turn mark and there is no valid formal report → record `handoff_split`: not complete, not BLOCKED, no attempt counted, no supervisor acceptance triggered; the continuation message goes to the FIFO head; rotate via the original §4 `finish_rotation`; once the successor generation is READY it continues the same dispatch.
5. **Only one fallback**: 300 s after the prompt the turn has not ended → type it once more; another 300 s → the watchdog's existing `Watchdog.rotate` kills the window + machine-skeleton handoff + continuation.
6. **Limits and clocks**: one dispatch is split at most 3 times; crossing the line again turns it BLOCKED to the supervisor with "split it smaller"; the service request clock is paused during a mid-turn handoff.
7. **Three key facts** (§4): all three CLIs, when they receive paste + Enter while running, **read it within the current turn** (after the tool batch); no Escape is needed. But three small gates must change — the pi extension's `input` hook swallows the typed text (worker) or turns it into operator_input (supervisor), Codex's turn binding requires "exactly one user message", and the Claude supervisor's UserPromptSubmit hook blocks it.
8. **Threshold risk must be surfaced** (§5): 0.8 leaves plenty of margin at 1M (Claude) and 400k (Astra); **at 200k (pi DeepSeek) only 23,616 tokens remain, and two large file reads at once make pi auto-compact first, so the handoff misses**. The default stays at the specified 0.8 rather than being lowered silently; the operator decides.

---

## 1. Review of the current state (evidence)

| Claim | Review | Evidence |
|---|---|---|
| Rotation is decided only in deliver and idle_rotate | True | `fleet.py:3958-3976`; `fleet.py:3789-3809` (requires `state == 'idle'` and an empty mailbox) |
| v2 handoff is a separate handoff delivery and requires an idle lease | True | `runtime_v2.offer_handoff` (`runtime_v2.py:2035`); the `purpose == 'handoff'` branch of `settle` (`runtime_v2.py:972-976`) |
| §32 states it does not cover long turns | True | design.md §32 "Known limits" |
| analyst-1 reached 186,673 in one turn and was compacted by pi | True | `~/.cmux-fleet-feishu/runs/fleet-20261027-213921-248bd82f/pi-sessions/analyst-1/g0/*.jsonl`: assistant usage peak 186,673, 2 `type=compaction` lines; run config `handoff_ratio 0.6` |
| analyst-2 / reviewer exceeded the threshold without rotating | True | analyst-2 g0 peak 140,911; reviewer g0 peak 135,283 before g1 appeared |
| reviewer stalled the whole task by not submitting `fleet_result` | Mechanism true | a pi task worker with no structured candidate → `settle` rules `exactly one structured candidate is required` → the pi worker goes straight to `finish('blocked')` (`runtime_v2.py:1059-1081`; pi does not get the TUI's two correction attempts) |

---

## 2. Design

### 2.1 Probe (separate thread inside the daemon)

```text
loop:
  t0 = now
  for slot in busy slots (state busy, lease received, message is not a handoff delivery):
      path = this generation's session file for the slot
      st = stat(path); (mtime, size) same as last time → skip
      read tail ≤ 64KB, take the last main-thread assistant usage → used
      results[slot] = (generation, message_id, used, window, path, st)
  sleep(max(0, 1 − (now − t0)))      # interval = max(1 s, this round's time); single thread, sequential, no overlap or pile-up
```

- **Reads files only and writes only one in-memory result table** (locked); Redis transactions, typing and rotation all happen in the main loop (`RuntimeV2.run`'s `step → pump → tick`, plus a new step `midturn_apply()` that reads the result table). Slow file reads only lengthen the probe's round; they never block delivery.
- Idle slots are not probed: they belong to §32 `idle_rotate`.
- Results carry `generation` and `message_id`; before applying, the main loop checks it is still the same generation and the same in-flight message, and drops stale results.
- As a side effect, `used` is written into `ctx_used` of `fx:agent:<slot>`, so status is live within the turn (fixes the 4,323 display problem).

| CLI | File | How usage is read | Evidence |
|---|---|---|---|
| pi | `<run>/pi-sessions/<slot>/g<N>/*.jsonl` (the newest one in this generation's directory) | `usage` of the last `message.role == assistant`: `input + cacheRead + cacheWrite + output` (with cacheWrite at 0, as observed, this equals the orchestrator's `input+cacheRead+output`); window from Redis `ctx_window` (self-reported by pi at startup) | measured from the session files of the run's four slots: analyst-1 186,673, analyst-2 140,911, reviewer 135,283 |
| Claude Code | slot field `transcript` (registered at bind) | existing `claude_transcript_usage` (`fleet.py:362-378`): last non-sidechain assistant line `input + cache_read + cache_creation + output`; window per §22 | measured locally: a running session's transcript writes a usage line on every model response; the last line's timestamp is < 1 s behind |
| Codex | slot field `thread` → rollout | existing `handoff.read_usage` (`handoff.py:59-87`): last `token_count.info.last_token_usage.total_tokens`; window from the rollout's self-reported `model_context_window` | measured from a local rollout: one token_count after every tool call within a turn, rising 20,650 → 108,394 |
| opencode | session store in the generation's data directory (`FLEETMUX_OC_SESSION_DIR`) | **unverified** (path and format); resolve before end-to-end; if not found → one `midturn.no_usage` per generation (error audit + control card), reported openly, never silent | the plugin already has `ctxUsed` (`opencode_plugin/index.js:333-337`); fallback: land it in Redis with the next metadata frame |

The existing `claude_transcript_usage` reads a 512KB tail and `read_usage` a 128KB tail; the probe uses a 64KB tail for all (usage lines sit near the last response; if no usage line is found, keep the previous value rather than treating it as 0).

### 2.2 Trigger and forced typing

Condition (decided by the main loop's `midturn_apply`): `used ≥ midturn_ratio × window`, and not yet typed in this generation (slot field `midturn_typed_g == generation`), and all guards pass (§6).

Action:
1. **[transaction]** write `midturn_id`, `midturn_at`, `midturn_used` on the in-flight message hash; write slot fields `midturn_id`, `midturn_draft` (pre-built draft, `handoff.partial_template()`, same as `start_rotation`), `midturn_mtime`, `midturn_typed_g`, `midturn_deadline = now + 300`, `midturn_tries = 1`; pause the service request clock (§2.5); push the in-flight message's `deadline_at` back by `2 × 300 + profile_ready_timeout` seconds so tick does not mark it expired/held during the handoff turn; audit `midturn.ordered`; control card "worker-1 上下文 160k/200k，已发中途交接指令" (worker-1 context 160k/200k, mid-turn handoff instruction sent).
2. **[outside the transaction]** `self.mgr.send_prompt_to_idle(pane, prompt, bracketed=<True for Claude, False otherwise>)`. Despite "idle" in its name, this function does not check idleness; it only pastes + presses Enter (`cmux_manager.py:667-669`). Failure → audit `midturn.type_failed`, retry once next round; if it still fails, the fallback timer takes over.

The prompt is a fixed **single line** (multi-line pastes can be split into two messages in non-bracketed mode, recorded in §11) and follows the §4 three-section format:

> `[FLEET MID-TURN HANDOFF fx-<16hex>] Context is {used}/{window}. Stop now; start no new step. Edit only the handoff note {path}: keep its three ## headings (Done (facts) / In progress / Next steps (in order)), at most 10 lines each; under In progress write exactly where you stopped inside the current instruction, under Next steps what remains of it. Do not send your final report or call fleet_result/fleet_decision. Then end your turn with the single word HANDOFF.`

`fx-<16hex>` = `midturn_id`, used by Codex binding and the Claude supervisor hook to recognise and let this line through (§4).

### 2.3 Turn settlement: `handoff_split`

Location: `runtime_v2.settle`, after `wall_settle` and before the `aborted / failed` checks (the wall takes precedence; §23 unchanged). When the message carries `midturn_id`:

| What the turn settled with | Handling |
|---|---|
| A valid formal report (worker Done+Evidence / supervisor decision JSON / service business JSON) | The model ignored the prompt and finished the job: settle and report as usual; clear `midturn_*` on the slot; after the turn §32 `idle_rotate` rotates normally |
| No valid report (including only replying HANDOFF, or only writing the draft) | **`handoff_split`**, see below |
| `aborted` / `failed` / `unfinished` (not caused by us) | The original hold path, unchanged |

`handoff_split` actions (one transaction, written like `wall_settle`, `runtime_v2.py:840-869`):
1. `release(env, {'midturn': 'split'}, outcome='handoff_split')`: lease back to idle, XACK, PEL cleared. **Do not call** `report` / `on_supervisor(invalid)` / `remember`; **do not touch** `report_retries`, `bad_replies` or contract report ownership; **do not take** the pi worker "no candidate → blocked" path (the one that stalled reviewer).
2. Build the continuation message: same `request_id`, `kind` and reply spec as the original; `continues=<original message_id>`; `midturn_splits = original + 1`; `replay` inherited as is (not incremented); `deadline_at = time offered + time remaining on the original when the prompt was typed` (§2.5); body = `[Continuation] You are {label}; {prev} stopped this same instruction midway (context full). Handoff note: {note}. Continue from it: do not redo finished steps, re-check the step marked in progress. The original instruction follows unchanged:` + original body. Use the LREM/LPUSH Lua in `offer_handoff` to put it at the **FIFO head**.
3. Slot: `handoff_id = midturn_id`, `handoff_draft / handoff_mtime` from `midturn_*`, `state = handoff`, `rotation_pending.add(slot)` → the existing `finish_rotation(timed_out=False)`: if the draft was edited and passes the three-section check → model version, otherwise the skeleton (original §4 rule).
4. Audit `midturn.split`; UI card "worker-1 中途交接 → worker-1.1 续做（第 1 次）" (worker-1 mid-turn handoff → worker-1.1 continues (split 1)) (not in the model's context, §11).

**Why `handoff_id` is not written when the prompt is typed**: `RuntimeV2.tick` calls `finish_rotation(timed_out=True)` on expiry for slots with a non-empty `handoff_id` and `state in (handoff, held)` (`runtime_v2.py:2271`); writing it early lets that path rotate first and lose the continuation.

The continuation passes the gates of `deliver()` (`fleet.py:3894`): `kind='continuation'` keeps the task id check and skips the live profile-switch decision and the advisor consultation (same dispatch; normal rotation keeps the chosen runtime, §12 / §27); the service guard "drop when the request is already terminal" (`runtime_v2.py:1906-1910`) still applies.

### 2.4 Fallback (only one)

| When | Action |
|---|---|
| 300 s (`midturn_retry`) after the prompt, turn not ended | type the same line once more (`midturn_tries = 2`) |
| another 300 s, still not ended | call the watchdog's existing `Watchdog.rotate(slot, 'midturn timeout', screen)` (`watchdog.py:689`): save the screen → skeleton handoff note (merged with the draft if the model already edited it) → new generation in place → `kill_pane` → `confirm_retired`. New variant `reason='midturn'`: no FROZEN report; the original message is recorded as `handoff_split` (audit `midturn.killed`), and the continuation is built per §2.3 step 2 |

- Paused / adopted slot / not in this fleet's registry → per §24 hold only, never kill; the clock resumes counting.
- Slot has a §24 `prompt_wait` (dialog waiting for a human, including delete confirmations) → stop the timer, no more typing, no kill; resume counting once the mark is cleared.
- Killing goes only through `Watchdog.rotate`; if the watchdog's own stalled → frozen arrives first and the message carries `midturn_id`, it takes the same continuation variant. Escape is not used and is not taken as proof of stopping (§11, §17 unchanged).

### 2.5 Limits and clocks (short)

- **`midturn_splits`**: stored on the Redis message hash (continuation = original + 1); service also writes the request hash `fx:req:<id>`. Persisted with AOF, no TTL; kept across rotation (the count is on the message, not the slot); kept by `start --resume`: when `requeue_lost` re-queues, copy `midturn_splits` and `continues` to the new message (today it copies only the body, `runtime_v2.py:1849-1852`; needs changing); service reads the request hash. Limit `midturn_max_splits`, default 3. Crossing the line again at the limit → no prompt, no split: a task worker registers §24 F′ 8.4's `blocked:<slot>` to the supervisor with the reason "这条派工一个会话装不下（已续做 3 次），请拆小" (this dispatch does not fit in one session (continued 3 times); split it smaller); a service worker synthesises `{"blocked": …}` and goes through `service_blocked_intake`; a supervisor slot: task `finish('blocked')`, service paused + card. If the successor generation is READY with usage already ≥ the mid-turn line (the handoff note + the role itself do not fit) → also BLOCKED (`midturn.impossible`).
- **Service request clock pause**: when the prompt is typed, write `clock_paused_at` on the request hash; when the continuation is `offer`ed to the successor generation, add that span to `clock_paused_ms` and clear `clock_paused_at`; `Fleet.expire_requests` and `RuntimeV2.tick`'s expiry judge by `now − created − clock_paused_ms − (now − clock_paused_at, if paused)`; if the split chain ends any other way (kill failure hold, BLOCKED, request cancelled) the clock resumes immediately. Published results carry `clock_paused_ms`. A task's per-message `deadline_at` is likewise pushed back (§2.2); the task's overall `hours` budget is not paused (§17 / §23 hard budget).

---

## 3. Coverage

| Slot | Covered |
|---|---|
| worker: pi / Claude / Codex | yes |
| worker: opencode | yes, provided the §2.1 usage source is resolved; otherwise report `midturn.no_usage` openly |
| supervisor: pi (default) / Claude / Codex | yes (continuation = the same message re-delivered to the successor supervisor; `bad_replies` untouched) |
| task / service mode | both covered |
| advisor slot (§27) | not needed: one consultation at a time, 180 s ticket, `advisor_restart` manages itself |
| adopted sub-fleet supervisor (§19), supervisor_slot of a named sub-fleet under delegation (§29) | not done, unchanged (same as `deliver`'s `subfleet.adopted → window=0`) |

---

## 4. Key facts a / b / c

### a) Paste + Enter while running: interjection, or queued until after the turn?

| CLI | Conclusion | Escape needed | Evidence | Status |
|---|---|---|---|---|
| Claude Code | **Read within the current turn**: a message entered while tool calls run is passed to the model in the same turn as soon as those tool calls finish; only during pure text output is it queued until the turn ends | no | official docs code.claude.com interactive-mode "Queue messages while Claude works": "if you queue a message while Claude is running tool calls, Claude Code passes it to Claude as soon as those tool calls finish, within the same turn"; how-claude-code-works "Interrupt and steer". In real local transcripts, text typed mid-turn is recorded as `type: attachment` / `attachment.type: queued_command` / `commandMode: prompt` lines (several sessions) | docs + transcript shape verified; **not yet measured on an interactive fleet Claude slot** (covered by end-to-end) |
| Codex | **Read within the current turn**: in the TUI Enter = submit; submit during a turn is a steer that enters the current turn's pending input queue and is picked up by the turn loop at the next step; Tab (`composer.queue`) is what queues until after the turn | no | main source `codex-rs/core/src/session/turn_input.rs` (steer pushes into the active turn's pending input), `codex-rs/tui/src/chatwidget/input_flow.rs`, `codex-rs/config/src/tui_keymap.rs` (`submit` / `queue` keys), `tui/assets/tooltips.txt`; local `codex features list`: `steer  removed  true` (now always on) | source verified; **not measured on 0.162.1 / 0.164** |
| pi | **Read within the current turn**: Enter = submit; submit while running is a steer that enters after the current assistant turn's tool calls finish and before the next model call; `alt+enter` is follow-up (queued until done) | no | `docs/keybindings.md:89` (`tui.input.submit = enter`), `:166` (`app.message.followUp = alt+enter`); `docs/how-pi-works.md:13`; `InputEvent.streamingBehavior?: "steer" \| "followUp"` (`types.d.ts:910-920`) | docs verified; **but blocked by the fleet extension, see c)** |
| opencode | unverified | — | the binary contains `delivery:"steer"` | **unverified** |

So none of the three CLIs needs an Escape first. The only "queued until after the turn" case is a model producing long text in one go without calling tools — such a turn grows context slowly, so just let it end naturally (if it ends without a valid report it still takes the split; the prompt line sits queued in the old session, the old tab is closed right away by `finish_rotation`, and the handoff note is the model version or the skeleton depending on whether a draft exists).

**Target-machine versions**: locally pi 1.3.0, Claude Code 2.1.280, codex-cli 0.162.1, opencode v2.0.32. The actual versions on the deployment hosts (pi 1.3/1.4, Claude 2.1.3xx, Codex 0.162/0.164) **could not be read this time** (ssh banner timeout / hostname resolution failure); before rollout, check each version on the target machine and run end-to-end (a Codex 0.161 vs 0.162 difference caused an earlier incident). Claude's docs note a queue behaviour change from v2.1.281 ("Before v2.1.281, the key interrupted the turn in both cases", about the send-immediately key, not plain Enter); 2.1.3xx target machines must be measured.

### b) How current v2 delivery blocks while the agent is busy, and which gate forced typing bypasses

| Gate | Location | Behaviour |
|---|---|---|
| `Fleet.pump` | `fleet.py:3758-3787` | calls `deliver` only for slots with `state == 'idle'` and pending messages |
| `Fleet.deliver` | `fleet.py:3894-3897` | returns immediately if `state != 'idle'` |
| `RuntimeV2.deliver` | `runtime_v2.py:1892-1900` | requires an idle slot and lease `status == 'idle'`, else returns; `bus.offer` also needs an idle lease |
| Typing itself | `cmux_manager.send_prompt_to_idle` (`cmux_manager.py:667-669`) | **does not check idleness**; just `_paste` + Enter |

Forced typing **does not use the queue, does not offer, and creates no new delivery**: a new main-loop step `Fleet.midturn_apply()` (in `fleet.py`, next to `idle_rotate`) writes the marks in a transaction and then calls `self.mgr.send_prompt_to_idle(pane, prompt, bracketed=...)` directly. This bypasses the three gates above without touching the in-flight delivery's lease / fence — it is just one extra line within the in-flight delivery's turn, and turn settlement still hangs on the original delivery.

### c) Will the typed text be blocked by the fleet's own hooks?

| Slot | Blocked? | Evidence | Minimal change |
|---|---|---|---|
| pi worker | **Yes, swallowed**: the extension's `input` hook returns `{action: "handled"}` for `active \|\| !ctx.isIdle()` and shows "派工执行期间请等待；手工输入未加入该任务" (wait while a dispatch runs; manual input was not added to the task); the model never sees it | `pi_extension_v2.ts:206` | add one rule at the very top of the `input` hook: `active` present, `event.source === "interactive"`, text starts with `[FLEET MID-TURN HANDOFF fx-` → `return {action: "continue"}` (pi handles it as a steer). About 3 lines. A human typing the same prefix is also let through — that is a human typing, harmless; whether it is really a mid-turn turn is decided by the daemon at settlement from the message's `midturn_id` |
| pi supervisor | **Yes, turned into operator_input** into the daemon queue, never reaching the model | `pi_extension_v2.ts:196-205` | put the same pass-through check before the `supervisor` branch |
| Claude worker | **No**: only the v2 Claude **supervisor** installs UserPromptSubmit (`prompt_hook_slot`, `fleet.py:1612-1614`); the Stop hook mailbox channel ignores mid-turn input. At settlement `claude_user_prompts` only counts `type == 'user'` lines (`fleet.py:1255-1257`), while a mid-turn interjection is a `type: attachment` line, so the "this turn was started by this delivery" check (`tui_turn.answered`, `fleet.py:1446-1455`) still holds and settlement lands on the original delivery | code + transcript shape | none |
| Claude supervisor | **Possibly**: UserPromptSubmit (`claude-prompt`) turns any human text that is not this generation's delivery body into operator_input and blocks it (design.md §11, Claude supervisor subsection); whether a mid-turn interjection triggers UserPromptSubmit is **unverified** | `fleet.py:1322-1360` | add a pass rule: text equals the mid-turn prompt registered for this slot and generation (Redis `midturn_id` matches) → pass, empty stdout |
| Codex supervisor / worker | **Input not blocked, but settlement is refused**: `codex_queue.bound` requires the turn to have "exactly one user message whose full text equals the queued text"; a mid-turn steer adds one more → `turn.ignored`, and the original delivery never settles | `codex_queue.py:228-231` | relax to: the first is this delivery's queued text, and every later user message's full text equals this generation's registered mid-turn prompt (with `fx-<midturn_id>`) |

---

## 5. Threshold: at 0.8, can the three window sizes finish the handoff before the CLI auto-compacts?

**Limitation (stated up front)**: the probe runs every second, but usage in the session file **only updates after each model response**. The tool results from a batch run after a response are only visible after the next response; and CLI auto-compaction checks exactly "after tool results are appended, before the next model call" (pi `docs/compaction.md:37`). So one large tool result (or a batch of parallel reads) can push usage from below the mid-turn line straight past the auto-compaction point in one step, and **even 1 s polling cannot catch it**. The margin between the mid-turn line and the auto-compaction point must fit "one batch of tool results + the handoff turn itself (prompt + writing a 30-line handoff note, about 3–5k tokens)".

Upper bound of a single tool result entering the model (official docs): pi bash / read 2000 lines or 50KB (`docs/codemode.md:43`), worst case about 17k tokens for Chinese text; Claude Code 2.1.280 Read 25,000 tokens, Bash 30,000 characters, MCP 25,000 tokens (code.claude.com tools-reference / env-vars / mcp); Codex model `truncation_policy` currently 10,000 tokens (main `models.json`; 0.162.1 unverified).

| Window | Mid-turn line 0.8W | CLI auto-compaction point C | Margin C − 0.8W | Can the handoff finish? |
|---|---|---|---|---|
| **200k (pi DeepSeek)** | 160,000 | 200,000 − 16,384 = 183,616 (pi `docs/compaction.md:32-35`) | **23,616** | **Not guaranteed**. The handoff turn is about 5k, leaving about 18.6k: one 50KB read (≤17k) barely fits; **two large reads in parallel, or the crossing step itself landing above 170k, and pi auto-compacts first**, so the handoff instruction lands after compaction (after compaction the model still sees the recent prompt and can usually still write the note, but detail is already lost — exactly what this avoids). Margin at 0.75: 33,616; at 0.7: 43,616 |
| **400k (Astra, pi provider `openai-codex`)** | 320,000 | 400,000 − 16,384 = 383,616 | **63,616** | Yes: fits 3 full-size reads + the handoff turn |
| **1M (Claude opus / sonnet)** | 800,000 | about 967,000 (design.md §22, citing CHANGELOG 2.1.247) | **about 167,000** | Yes: Claude's single-tool cap is 30k; the margin covers 5 batches |

**For the operator**: 0.8 does not hold for a 200k window (margin 23.6k; one batch of two large reads triggers auto-compaction first). The default is the specified 0.8 and **is not silently lowered**; whether windows of 200k and below get a separate 0.7 (configure the roster `handoff:` table per `pi:<model>`; §22 already supports it) is the operator's decision. Two more points:
- The auto-compaction point for Claude models with a 200k window (haiku etc.) is **unverified**; if it is about 167k, the 0.8 line at 160k leaves only about 7k and likewise does not hold.
- From Claude Code 2.1.296, Read supports `allow_large` (sized by remaining context), so a single tool result has no upper bound; on target machines ≥ 2.1.296 the 200k tier is more dangerous.

---

## 6. Guards and edge cases

| Situation | Handling |
|---|---|
| §28 clearing / `clear_required` | no prompt (same as `start_rotation`) |
| paused | no prompt (§7 "pause stops delivery / rotation"); an already-typed one settles as usual |
| §24 `prompt_wait` dialog waiting for a human | no prompt; for an already-typed one the fallback timer stops (delete confirmations are never killed) |
| adopted slot / profile switch in progress | no prompt (copy of the `idle_rotate` guards) |
| the in-flight delivery is itself a handoff delivery | no prompt (a handoff turn that overflows takes the original §4 handoff_timeout) |
| `max_busy` | from prompt to split the slot is busy, then handoff; both count as busy (existing `pump` accounting); the successor generation in starting does not count (original §7 semantics) |
| supervisor "nobody busy, must dispatch" | a worker in handoff / starting makes `others_active()` true (`fleet.py:3289-3293`, `state != 'idle'` means busy), so a supervisor replying RUNNING is not judged wrong |
| late callbacks from the old generation | unchanged, dropped by generation |
| half-finished work left by a killed long tool | the program does not guarantee it; the successor generation re-checks via the handoff note |

---

## 7. Tests

- **Unit tests** (real private Redis + fake cmux, reusing the harness of `tests/test_v2_codex_claude.py`; new file `tests/test_midturn_v2.py`): probe (skip on unchanged stat, 64KB tail read, reading each of the three CLI files, dropping stale results, interval = max(1 s, elapsed)); trigger (type once past 0.8, not again in the same generation, no prompt under each guard, single-line prompt, Claude bracketed); settlement (no report → split, continuation at the queue head, no report, not blocked, `replay` unchanged, `midturn_splits` +1; valid report → normal settlement then idle rotation; pi worker does not go straight to blocked); fallback (retype at 300 s, `Watchdog.rotate` continuation variant after another 300 s, `prompt_wait` stops the timer); limit of 3 turns BLOCKED (task / service / supervisor), `requeue_lost` keeps the count; service clock pause and resume; relaxed Codex `bound` (a second user message counts only if it is the registered mid-turn prompt); Claude supervisor UserPromptSubmit lets the mid-turn prompt through; pi extension contract tests (prefixed text passes / unprefixed text is still swallowed or turned into operator_input).
- **End-to-end smoke** (real cmux, CLIs at target-machine versions, new script `scripts/smoke_midturn.sh`): isolated `FLEETMUX_HOME` / `FLEETMUX_STATE`, `--lifecycle smoke`; the roster sets `midturn_ratio` to 0.10 (an explicit test threshold, restored afterwards; §4 "demo thresholds must not linger"); the task has the worker read 20 medium files one by one and then write a summary. Assertions: audit `midturn.ordered → midturn.split → rotate.done`; handoff note produced; history page shows `worker-1.1`; original message `outcome=handoff_split`, continuation committed with `continues` pointing at the original; task COMPLETED and the output accepted; no `execution.held`. Combos to run at least: `pi pi task`, `pi claude task` (the default combo), `pi pi service` (request time limit shorter than "handoff + continuation", assert no timeout), one Codex slot (unverified rows of the combo table use the smoke lifecycle), and one mid-turn on a supervisor slot. Add one for opencode once its usage source is resolved.

---

## 8. Implementation breakdown

| Block | Files | Content | Success criteria |
|---|---|---|---|
| **M1 daemon (done as one block)** | `fleetmux/midturn.py` (trimmed), `fleetmux/fleet.py`, `fleetmux/runtime_v2.py`, `fleetmux/watchdog.py`, `fleetmux/handoff.py`, `fleetmux/codex_queue.py`, `fleetmux/roster.py`, `fleetmux/pi_extension_v2.ts` (only the §4c pass rule), `tests/test_midturn_limits.py`, `tests/test_midturn_v2.py` (new), pi contract test fixtures | **Trim midturn.py**: keep usage reading (`claude_transcript_usage`, `codex_rollout_usage`, plus new pi session reading, all with a 64KB tail), thresholds (`resolve_settings` / `decide` without A/B layer parameters, `DEFAULTS.midturn_ratio = 0.8`, new `midturn_retry = 300`, `midturn_max_splits = 3`), clocks (`effective_age` / `pause_clock` / `resume_clock` / `pause_message` / `resume_message`), continuation and BLOCKED texts (`continuation`, `blocked_text`), the single-line prompt; delete `MIDTURN_INJECT`, `TOOL_LIMITS` / `TOOL_SOURCES`, the A/B layers / k / b of `limits()`, `parse_usage_frame`, `order_key` / `order_record` / `LUA_NOTICE` / `posttool_hook` / `posttool_main` / `HOOK_*`, `blocked_prompt`, `midturn_nudge` / `midturn_b_wait`. **fleet.py**: probe thread + `midturn_apply()` (one main-loop step), forced typing, `deliver` skips profile switch and advisor for continuations, `expire_requests` by effective age, Claude supervisor `claude-prompt` pass rule, roster `handoff:` table accepts `midturn_ratio` (change `roster.py:135-169` too). **runtime_v2.py**: `settle`'s `handoff_split` branch, continuation at the queue head, tick expiry by effective age, fallback timers, `requeue_lost` copies `midturn_splits` / `continues` (`runtime_v2.py:1836-1855`). **watchdog.py**: `rotate(reason='midturn')` continuation variant. **handoff.py**: mid-turn skeleton variant (In progress carries the original instruction's head and the continuation mark). **codex_queue.py**: relaxed `bound`. **pi_extension_v2.ts**: 3-line prefix pass rule + rewritten first-line comment | `tests/test_midturn_limits.py` (after trimming) and `tests/test_midturn_v2.py` all pass; full `pytest -q tests` with zero regressions (including `test_v2_*`, `test_watchdog*`, `test_fleet.py`, `test_handoff_limits.py`, `test_roster.py`, `test_advisor*`, `test_codex_queue*`, pi contract tests); the control line shows `ctx=<used>/<W> mid=<0.8W>` |
| **M2 end-to-end smoke** | `scripts/smoke_midturn.sh` (new) | §7 end-to-end | the §7 combos pass with CLIs at target-machine versions; the script prints pi / claude / codex / opencode versions at the start and compares them with the production host; no hard-coded local paths |
| **M3 docs** | `docs/design.md` | new §34 (condensed version of this design, citing requirements 1 and 2 as the basis for overturning `agent_settled only reports; it never requests continuation` and §32 "no rotation in the middle of a long turn"); §4 item 1 adds "from §34, crossing 0.8 within a turn also rotates"; §32's closing "Known limits" points to §34; §7 "pi adaptation requirements" adds "input with the mid-turn prefix passes"; §9 placement table adds `midturn.py`; §5 / §11 describe the relaxed Claude supervisor hook and Codex binding | each replacement stated explicitly; the §5 threshold risk (200k tier) written in as is; end-to-end results and unaccepted items listed honestly |

---

## 9. Rejected (r1 / r2, dropped as overly complex)

- **A/B two layers** (CLI-cooperative interjection layer + daemon kill-window floor layer) and the per-CLI injection-capability table: rejected; instead the daemon forces the same single prompt line into every CLI, and killing the window is only the timeout fallback.
- **pi extension usage frames + bridge control flow** (`usage` / `midturn` / `abort` frames, `ctx.abort()`): rejected; instead the daemon reads pi session files; the extension keeps only the 3-line §4c pass rule.
- **Claude / Codex PostToolUse `additionalContext` hooks**: rejected; paste directly instead (Claude and Codex already read Enter within the turn while running, §4a).
- **P1–P8 probe scripts and the version-comparison pipeline**: rejected; verified once by the end-to-end smoke on target-machine CLI versions.
- **A handoff-margin formula derived from per-tool truncation limits** (R = T·k·b + H) with automatic clamping: rejected; the threshold is the specified 0.8, and windows with insufficient margin are reported openly in §5, not adjusted automatically.
- **Waiting for a "no tool subprocess" gap before killing the window**: rejected; the fallback is only "type once more → kill the window and continue".

---

## 10. Unverified (must be checked on the target machine before rollout)

1. The actual versions of pi, Claude Code, Codex and opencode on the deployment hosts (ssh failed this time), and whether under those versions "paste + Enter while running" is read within the turn as §4a describes.
2. Whether the Claude supervisor's UserPromptSubmit fires for a mid-turn interjection (§4c; the pass rule is added first and does not depend on the answer).
3. opencode: interjection behaviour while running, and the usage path and format in its session store (§2.1); if unresolved, opencode slots can only report `midturn.no_usage` openly.
4. The auto-compaction point of Claude 200k models (haiku etc.); Codex's auto-compaction point when `--compact-limit` is not given.
5. Codex 0.162.1's actual single tool output cap (10,000 tokens in main source).
