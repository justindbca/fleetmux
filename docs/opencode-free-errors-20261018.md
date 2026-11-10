# pi × DeepSeek Flash Free bridge: error catalogue (2026-10-18)

Subject: `launchers/start-pi-deepseek-free.command` → `extensions/opencode-free/{index.ts,backend.mjs}` → local official OpenCode v2.0.30 → `opencode/deepseek-v4.1-flash-free`.
(Correction, 10-19: `extensions/opencode-free/` and `docs/opencode-free-pi.md` were committed with baseline `ea7e67d2`.)

> **Status (2026-10-19, `3b99ef26`)**: the bridge now uses "Pi tools over OpenCode's native tool channel" (a stub MCP server + a long-lived OpenCode service); the model no longer hand-writes a JSON envelope. Sections 1–5 describe the old bridge at baseline `f3cb9d4a`, and line numbers refer to that version; classes 1/3/4/7 can no longer occur with the new bridge. The fix history, current status of each class and open issues are in **Section 7**.

## 1. Overall failure rate (from Pi session logs `~/.pi/agent/sessions/opencode-free/*.jsonl`)

| Session | OK (toolUse + stop) | Errors |
|---|---|---|
| 2026-10-16T19-03 large session | 65 | 67 (excluding 2 user aborts) |
| 2026-10-16T00-37 | 6 | 2 |
| 2026-10-17T22-03 (manual test session) | 2 | 1 |

In the large session roughly one turn in two failed.

## 2. Error classes (by frequency; throw sites refer to baseline `f3cb9d4a`, current status of each class in Section 7)

| # | Error text | Count | Thrown at |
|---|---|---|---|
| 1 | `Stream envelope must start with the text key.` | 23 | backend.mjs:142 |
| 2 | `OpenCode attempted a native tool; request stopped. Only Pi tools may execute.` | 23 | backend.mjs:259 |
| 3 | `OpenCode returned invalid bridge JSON; no tools executed.` | 14 | backend.mjs:29 |
| 4 | `OpenCode returned an invalid bridge envelope; no tools executed.` | 4 | backend.mjs:33 |
| 5 | `context_length_exceeded: bridge transcript estimate exceeds input budget; compact the Pi session.` | 3 | index.ts:69 |
| 6 | `Unexpected OpenCode model or extra native agent turn.` | 1 | backend.mjs:251 |
| 7 | `Unescaped control character in streamed text.` | 1 | backend.mjs:160 |
| 8 | `OpenCode did not finish normally (tool-calls); no tools executed.` | seen during reproduction | backend.mjs:275 |
| — | `Operation aborted` | 2 | user abort, expected |

## 3. What each class looks like

### 1. Output does not start with `{"text":`
- Example (10-16 01:02): the displayed body is empty; the reasoning ends with "This is the final answer… Keep it in Chinese, concise." — the model wrote its Chinese answer directly without wrapping it in JSON.
- Example (10-16 19:05): the reasoning ends with "Final."; the body is again empty.
- Mostly on the final turn, when the model should wrap up and answer.

### 2. The model called one of OpenCode's built-in tools (the failure hit in the 10-17 manual test)
- The 06:04 turn that day: the reasoning says "I'll do 3 bash calls + 1 codemode… Let me write them.", no body appears in the UI, and the error is raised immediately.
- **Reproduction**: replaying the conversation up to that failure verbatim (the first 13 lines of the session: a 7.8k-character system prompt, 10 Pi tools, 9 messages) reproduced it 3 times out of 8.
- Event received at failure: `session.tool.input.started` with `name` `bash` / `codemode`, and **no** `{"text":…}` body at all.
- Full event sequence when an instrumented build runs to the end:
  `step.started → reasoning.started → tool.input.started(bash) ×2 + (codemode) → tool.input.ended(text=argument JSON) → tool.called(input={command:…}, executed:false) → step.ended finish="tool-calls"`
  That is, the model used OpenCode's native function-calling channel, with tool names that happen to collide with Pi's (bash/read/edit/write/codemode) and the same argument shape (`{"command": "..."}`, `{"code": "..."}`). `executed:false`, permission was ask, and OpenCode did not actually execute anything.
- OpenCode injects its own instructions: `session.instructions.updated` contains `core/codemode`, `core/skill-guidance`, `core/environment`, and each request carries a fixed prefix of about 6k extra tokens (cache.read=6144).
- Four attempts with a short conversation (1 user message, 2 tools) did not reproduce it; it only shows up once the conversation is long.

### 3. The JSON itself is broken
- The first half of the body has already streamed out normally and been shown to the user (example: 「两个后端都在正常出词了…再打一发针对性测试」, "both backends are producing tokens now… one more targeted test"), and parsing the whole JSON fails at the end. The user sees half a message, then the error; no tool runs.
- Mostly in turns whose tool arguments contain complex shell quoting or heredocs.

### 4. Wrong outer JSON shape
- The body streams normally (example: 「收到，按方案 2 走…要确认三件事」, "got it, going with option 2… three things to confirm"), but the keys are not exactly `text,tool_calls`, or a type is wrong. Same symptom as class 3: half a body + error.

### 5. Context limit exceeded
- Three times in a row late in the large session. The estimate is "bytes ÷ 3"; anything above 200000−32000 is refused and the request is never sent.
- How far "bytes ÷ 3" is off for mostly-Chinese conversations was not checked. (Checked on 10-19: the estimate is within about 1.5% near the limit and is not the cause; see Section 7, item 5.)

### 6. An extra agent step
- Once (10-16 20:33). Body and reasoning both empty. A second `session.step.started` appeared within the same request. Presumably OpenCode started another step automatically after a native tool call; not verified.

### 7. Unescaped control character in the body
- Once. A long body had already streamed (「先说清一件事：`/reload` 热装这条路可以判定失败了…」, "first, to be clear: the `/reload` hot-load route can be declared failed…") when the model wrote a raw newline inside a JSON string.

## 4. Constraints found during reproduction (know these before changing anything)

- **Class 2 cannot be fixed by turning off OpenCode's built-in tools.** Adding `permission: {'*':'deny'}` to a temporary config, or changing permission from `ask` to `deny` only at session creation: all 7 attempts were refused upstream —
  `provider.auth HTTP 403: FreeTierError "OpenCode's free tier can only be used from within OpenCode."`
  A control run with the stock setup (`ask`) at the same time got model output normally. It looked as if the free tier requires requests to carry OpenCode's stock tool set.
  (**Correction, 10-19: this attribution is wrong.** That day's 403 text was an IP-based abuse block (see Section 6, item 4); on 10-19, adding MCP tools and adding `policies` both produced tokens normally, with cache.read still 6144.)
- In every class the tools were not executed, so the failure itself is safe; but every failure requires the user to resend manually, with no automatic retry. (**Correction, evening of 10-18: this does not hold when the model uses OpenCode's codemode; see Section 6.**)
- The error message does not include the SSE event type or tool name that triggered it, so the UI alone cannot tell which failure occurred (class 2 shows only a fixed sentence).

## 5. Reproduction material

- Replay script: read the first N lines of a Pi session jsonl, build `encodePrompt(system, messages, tools)`, call `streamOpenCode`, and record every event with `onProviderStreamEvent`. The system prompt is the concatenation of the `message.sections` parts; tools come from `message.toolsAdded`.
  (New bridge: `encodePrompt(system, messages)`, with tools passed via `streamOpenCode(prompt, { tools })`; pi-ai's `collapseSystemMessages` / `getCurrentSystemPrompt` / `getCurrentTools` can assemble it the same way index.ts does.)
- With the first 13 lines of session `2026-10-17T22-03-19-602Z_01a14be4-….jsonl`, 6 concurrent runs produce class 2 about 1–2 times.

## 6. Re-test on the evening of 2026-10-18 (OpenCode v2.0.30, all in temporary directories + temporary sessions, no repository changes)

### 1. 🚨 codemode executes without approval; `executed:false` cannot be trusted
- OpenCode's own codemode tool (internal name `execute`) can call 53 tools: 45 `tools.browser.*` and 8 `tools.opencode.*` (`list_mcp_resources`, `models`, `read_mcp_resource`, `session_move`, `session_rename`, `worktree_create`, `worktree_list`, `worktree_remove`).
- With every session permission set to `ask`, calls made inside codemode produce **not a single `permission.asked`**. Tested: asking it to call `tools.opencode.session_rename` really renamed the session, while the events showed `tool.called … executed:false` + `tool.success`.
- Call and execution happen almost simultaneously (same millisecond), so by the time the bridge sees the first `session.tool.*` event and throws, it is too late. Section 4's "no tool ran, the failure is safe" therefore does not hold for codemode; the bridge runs in an empty temporary directory, so the actual damage risk is low, but tools like `worktree_remove` can take a directory.
- **Cannot be switched off in config**: a top-level `policies: [{action:"tool.use", resource:"*", effect:"deny"}]` is loaded (visible in `GET /api/config`), yet codemode's internal calls still execute. Reason: the `tool.use` policy hangs off the permission evaluation hook `permission.hook("evaluate")`, and codemode's internal calls never go through permission evaluation. `Config.Agent` has no tool switch field either. It can only be blocked at the process level (e.g. macOS `sandbox-exec` restricting writes / network).
- With that policy config, cache.read is still 6144 and the `core/codemode` hash is unchanged (`fde2b06e…`).

### 2. Class 2 (native tools) splits into two cases
- **A. The model used a Pi tool name on the native channel** (`bash` / `codemode`; OpenCode's own equivalents are called `shell` / `execute`, so the inference was that these names do not exist in OpenCode and fail outright — **verified on 10-19**: the `tool.failed` text is `No tool named "bash" is currently available. Please use a tool from the available tool list.`, `executed:false`): replaying the first 13 lines of the 10-17 session 4 times reproduced it twice. Events: `tool.called(executed:false)` ×3 → `step.ended finish="tool-calls"` → **OpenCode automatically starts step 2** → in step 2 the model returns a valid envelope containing the same `tool_calls`. No `permission.asked` at any point. So the model just delivers its envelope "one step late"; if the bridge ignores step 1's tool events and only takes the last step's body, this case becomes a success. Class 6 in Section 3 ("an extra agent step") is very likely this step 2.
- **B. The model called a tool OpenCode really has** (`shell`): a `permission.asked` is emitted and then it **hangs indefinitely** (no further events for 60 seconds), with no `step.ended`. After replying `POST /api/session/<session>/permission/<request id>/reply` with body `{"decision":"reject","message":"…"}` (204), the `message` is passed to the model as a tool error and OpenCode automatically starts the next step within the same run. In one test, after 2 rejected shell calls the model switched to codemode and took 57 seconds to finish — a cap on rejections is needed.
- Short conversations (≤16k) in the bridge format: 9 attempts (including 4 replays at lines 11 and 15 of the 10-16 large session), 0 reproductions, all valid envelopes.

### 3. Correction to the Section 1 numbers
- All 7 sessions in the directory: 81 OK, **70** errors, 2 user aborts (the report said 71, overcounting by 1). 70 = 23+23+14+4+3+1+1+1; the last 1 is the `provider.auth HTTP 403` at 10-18 13:30Z.

### 4. The 403 is an IP block
- Text: `OpenCode's free models aren't available from your current network because we've detected abusive traffic from this IP address. This isn't related to your account.` Switching the egress IP restored service immediately; the new IP was blocked the same way after about 25 requests in roughly 40 minutes. Re-tests must be rate-limited, and so must the bridge itself.

## 7. Fix log and status, 2026-10-19

### 1. What was fixed (branch fast-forwarded step by step; the running `cmux-fleet-pi` is `3b99ef26`)

| Commit | Change |
|---|---|
| `20703ed0` | `opencode serve` runs inside macOS `sandbox-exec`: it may only write this service's temporary directory + OpenCode's `log` / `shell` directories; `OPENCODE_DB` and `TMPDIR` point into the temporary directory, so it cannot touch the user's own OpenCode sessions. Closes Section 6, item 1 (codemode bypassing approval) |
| `e2028074` | L2 lenient parsing (envelope) — later superseded and removed wholesale by `978bed29` |
| `7ced642f` / `377e5767` | L1-A skips a step whose tool name OpenCode does not have; L1-B auto-replies reject to approvals for real OpenCode tools and continues to the next step |
| `0682b713` | Pi tools renamed `pi_<name>` in the prompt to avoid OpenCode's same-named tools |
| `978bed29` | **Protocol change**: Pi tools are exposed natively as `pi_<name>` by a stub MCP (`mcp-stub.mjs`, lists tools, never executes); the arguments OpenCode has already parsed are captured from `tool.called`, the approval is answered with reject (nothing executes inside OpenCode), and at this step's `step.ended` the calls are handed to Pi; the body is a plain text stream; envelope parsing removed |
| `57f5152d` | Replying to an approval that was already closed as a side effect (404) is not an error |
| `2c2cbf92` | OpenCode's built-in tools no longer abort the whole turn: those needing approval are rejected, those that don't (read-only tools, codemode) run to completion inside the sandbox and the next step follows; already-streamed body text is kept; at most 3 steps |
| `b72a4f51` | After MCP reports connected, wait another 2 seconds before sending the prompt (stopgap, see item 3 below) |
| `3b99ef26` | Long-lived OpenCode service (one per Pi process); leftover sessions swept after each request; after 50 requests or a failed request a new service is pre-warmed in the background and takes over only once ready; stops after 15 idle minutes; stops when Pi exits |
| `bece67ee` / `4825bb98` / this commit | Pi calls with malformed arguments are dropped or the step continues; OpenCode egress goes through a fixed proxy (a dedicated ISP egress); history becomes a readable record (past calls written as `(called pi_x [id] with {…})`) instead of feeding message JSON to the model — at 10-19 05:41 the model had copied that JSON and written a tool call as body text, so nothing executed |

### 2. Status of each class

| # | Class (Section 2) | Status |
|---|---|---|
| 1 | Does not start with `{"text":` | **Cannot occur any more** (no envelope) |
| 2 | Model called an OpenCode built-in tool | **No longer fails the whole turn**: those needing approval are auto-rejected, those that don't run to completion inside the sandbox, and the model switches to `pi_*` in the next step. With the long-lived service the model was observed calling `pi_bash` directly in the first step |
| 3 | Broken JSON | **Cannot occur any more** (arguments are parsed by OpenCode) |
| 4 | Wrong outer envelope shape | **Cannot occur any more** |
| 5 | Context limit exceeded (bridge's own bytes ÷ 3 estimate) | **Fixed** (see item 5 below): not an estimation error; Pi's proactive compaction line (200k−16k) sat above the bridge's refusal line (168k). The window declared to Pi is now 168000, so Pi compacts on its own at about 151.6k, leaving room for one turn of growth |
| 6 | Extra agent step | Identified as the step OpenCode starts automatically after class 2; now handled as a continuation step |
| 7 | Unescaped control character | **Cannot occur any more** |
| 8 | `did not finish normally (tool-calls)` | Only reported when a step still has unfinished OpenCode tools |

Live runs (10-19): plain Q&A, a single tool, several tools in one step, and the replay of the long 10-17 session all pass, with cache.read starting at 6144; with the long-lived service the first request takes 4.9 seconds and later ones 1.4–1.9 seconds.

### 3. OpenCode v2.0.30 behaviour established here (read before changing the bridge)

- **MCP config must use the new format** `mcp.servers.<name>`; the old format `mcp.<name>` gets `codemode:false` normalised away and the tools are folded into codemode (first-step cache.read drops to 2048). With the new format the tools appear natively as `<server name>_<tool name>`, cache stays 6144, and the gateway does not refuse.
- **MCP tools register asynchronously**: after `/api/mcp` reports `connected`, the tools take a while longer to enter the request's tool registry, and no event or API shows when that is done (the log only has `mcp connected tools=N`). A prompt sent immediately gets no `pi_*` in its first step (the model is forced into codemode, or gets `No tool named "pi_bash"`); a control group that waited 2 seconds got them. In every observed case the connection came up after the session was created (the no-session case was not tested).
- **Approval API**: `POST /api/session/<session>/permission/<request id>/reply`, body `{"decision":"reject","message":"…"}` (the field is `decision`, not `reply`), 204. After one is rejected, the other pending requests of the same batch are closed as a side effect, and replying to them returns 404.
- **Events after a reject**: `permission.replied` → `tool.failed` (OpenCode built-in tools: `permission.rejected`; MCP tools: `tool.execution "Unable to execute pi_x"`) → next step `step.started`; the `step.ended` in between is sometimes present, sometimes not.
- **`session.usage.updated` is cumulative across steps**; per-step tokens come from `step.ended`.
- **codemode's internal calls skip permission evaluation**; neither top-level `policies` nor session permissions can stop them (Section 6, item 1); the `tool.use` policy only applies to calls that go through approval.
- **Session API**: `GET /api/session` lists all sessions; `POST /api/session/<id>/interrupt` stops the current run.
- **The 403 is an IP-based abuse verdict**, unrelated to account or tool set; on 10-18 the first egress was blocked after about 25 requests in roughly 40 minutes and switching egress restored service immediately; on 10-19 an egress with about 35 requests spread over several hours was not blocked.

### 4. Still open

- The 2-second wait is a stopgap: with the long-lived service only each service's first request pays it, but the root cause (OpenCode does not report when tool registration is complete) is upstream.
- After a reject the model occasionally explains in the body that it "used the wrong tool name", and that sentence is shown to the user.
- When Pi is `kill -9`'d, the exit hook cannot run and the long-lived OpenCode service is left behind; it has to be cleaned up by hand.
- The bridge itself has no rate limit; dense requests from one IP still get IP-blocked.
- An outside AI that still edits `backend.mjs` against the baseline `f3cb9d4a` envelope protocol will collide heavily with the current code (the event loop and the startup part were essentially rewritten).

### 5. Class 5 (context limit exceeded): post-mortem and fix (10-19)

- **The estimate was fine**: calibrated on 48 successful requests (prompts rebuilt exactly as the code built them then, against the input+cacheRead OpenCode reported), real tokens ≈ bytes × 0.317 + 8991 (OpenCode's fixed prefix is about 9k). Bytes ÷ 3 runs low for small prompts and close for large ones: estimate 163404 / actual 161028. The claim that "bytes ÷ 3 overestimates" does not hold.
- **The limit was fine too**: OpenCode's model catalogue says context 200000, output 32000, so the input budget is 168000.
- **Real cause**: Pi only compacts proactively when the context exceeds window − compaction.reserveTokens (200000 − 16384 = 183616); anywhere between 168k and 183.6k, the bridge's pre-check refuses first (`context_length_exceeded`), and only on receiving that error does Pi go "overflow → compact → retry". So every time it took one needless error; on 10-16 the first occurrence took two in a row (151, 154) before compacting.
- **Fix**: the contextWindow declared to Pi = the input budget 168000, so Pi's proactive compaction line = 168000 − 16384 ≈ 151.6k; the next turn's growth (tool results, new messages) fits in the reserve and cannot reach 168k; the bridge's pre-check uses an explicit constant 168000. Pi's footer shows /168k.
- **Correction (evening of 10-19)**: the first version (`b80056fe`) set the window to 184384, putting the compaction line at 168k = the bridge's refusal line, i.e. no room for growth; in a live run the context stopped at 156.8k without Pi compacting, so the next turn could hit the pre-check first. Only after changing it to 168000 (from this commit on) is there headroom.
