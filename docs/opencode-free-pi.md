# Pi × DeepSeek V4.1 Flash Free (real streaming shell)

## Launch

Install Pi and the official OpenCode first, and **log in inside OpenCode**. No Pi `/login opencode` and no Console API key are needed.

```bash
cd <fleet repo root>
./launchers/start-pi-deepseek-free.command
```

By default it opens and focuses a **new cmux workspace** `pi · DeepSeek Free`; if cmux is not running, the app is started first. Double-clicking the `.command` also hands over to cmux; once the workspace is created, the cmux app is explicitly brought to the front, so macOS Terminal no longer covers the Pi that has opened. It does not restart the app, does not touch existing fleet windows, and does not create a fleet. The cmux-internal socket path needs >= 0.64.25; the native `.command` open path from Finder / an external Terminal needs >= 0.65.0. `CMUX_BIN` selects the CLI.

**Double-click differs from internal testing**: the default `cmuxOnly` mode forbids the macOS Terminal process from connecting to the cmux socket. When the socket is found unusable, the launcher stops retrying `ping` and instead writes a temporary `.command` readable / writable / executable only by the current user (700), then calls the official `open -a /Applications/cmux.app <file>` so cmux itself opens a new terminal to run it. The temporary file contains only the chosen launch arguments, deletes itself on start and switches to the real launcher's `--terminal` mode; it opens no further window and does not change the socket security setting. If macOS accepts the open request but never runs the file, the temporary file may be left behind. The original Terminal showing "Process completed" is the normal end of the hand-over; Pi is in the cmux window. With the native open path, Pi may update the workspace title to `π - <project name>`.

```bash
# Set the code working directory
PI_WORKDIR=/path/to/project ./launchers/start-pi-deepseek-free.command

# Continue a Pi session
./launchers/start-pi-deepseek-free.command --continue

# Check arguments / executables only, no model request
./launchers/start-pi-deepseek-free.command --check-only

# Stay in the current terminal (no cmux workspace)
./launchers/start-pi-deepseek-free.command --terminal

# JSONL/RPC automation must use --terminal (the launcher prints extra text; for pure JSONL calls see below)
./launchers/start-pi-deepseek-free.command --terminal --mode json -p 'Explain this project'
```

The launcher's own flags `--terminal` / `--check-only` go before the Pi arguments. `--check-only` does not start cmux and creates no workspace. A cmux launch command longer than 900 bytes is refused to avoid terminal input truncation; type long tasks in the new window, or use `--terminal`.

`PI_BIN` and `PI_OPENCODE_BIN` can point at the real CLIs. The shared launcher puts the Node next to Pi on PATH, so it does not depend on whether the Finder/GUI shell has initialised fnm; a failed Pi version pre-check exits with a clear error instead of showing `?` and starting anyway. Pi history is stored separately in `${PI_CODING_AGENT_DIR:-~/.pi/agent}/sessions/opencode-free`.

Pure Pi CLI call (without the launcher's extra text):

```bash
pi -e ./extensions/opencode-free/index.ts \
  --provider opencode-cli --model deepseek-v4.1-flash-free \
  --mode json -p 'Hello'
```

## Shortcut entry in other project roots

**Do not copy the real launcher wholesale into another project root**: it depends on its position relative to `launchers/`, and a copy would treat the wrong parent directory as the framework root.

If the consuming project sits next to the fleetmux checkout (or `FLEETMUX_PI_DIR` points at it), copy the thin wrapper template instead:

```bash
cp launchers/compat/start-pi-deepseek-free.command ../my-project/start-pi-deepseek-free.command
chmod +x ../my-project/start-pi-deepseek-free.command
../my-project/start-pi-deepseek-free.command --check-only
```

The wrapper's default working directory is the project it sits in, and it calls the real launcher in `$FLEETMUX_PI_DIR/launchers/` (default: the sibling `fleetmux/launchers/`); it still opens in cmux by default, and `PI_WORKDIR` and the arguments are passed through unchanged. The real launcher now checks that its companion scripts / extension exist before opening a window, and `--check-only` also catches a misplaced copy.

## Actual call chain (since 2026-10-19; the old "hand-written JSON envelope" protocol is retired)

```text
Pi TUI / JSONL / sessions / read,bash,edit,write …
  → extensions/opencode-free/index.ts (custom provider)
  → backend.mjs: one long-lived opencode serve per Pi process (macOS sandbox-exec sandbox, 127.0.0.1 random port)
      config mounts a stub MCP (mcp-stub.mjs, lists tools, never executes): mcp.servers.pi, codemode:false
      → Pi's tools appear to the model as native OpenCode tools named pi_<name>
  → each request opens a temporary session (model fixed to opencode/deepseek-v4.1-flash-free)
  → subscribe to /api/event first, then POST /api/session/{id}/prompt
  → session.text.delta / reasoning.delta mapped to Pi in real time (the body is a plain text stream)
  → model calls pi_* natively → the arguments OpenCode parsed are captured from session.tool.called
      → that call's approval is answered with reject (nothing executes inside OpenCode) → at the end of the step the calls are handed to Pi, results go in on the next turn
  → request ends: interrupt and delete this session, sweep leftover sessions; the service is kept for the next request
```

- **Real streaming**: body text is shown as soon as it arrives over SSE; a short answer may come as a single delta, which is decided by upstream chunking.
- **Tool calls**: OpenCode parses the arguments from the model's native call; the bridge never asks the model to hand-write JSON. At the Pi boundary it still checks that the tool name was declared, the arguments are an object, and there are ≤32 calls per step. Calls with malformed arguments are dropped; if every call in a step is malformed, the next step lets the model resend.
- **Model uses OpenCode's built-in tools** (shell / read / codemode — the instructions OpenCode injects steer it that way): those needing approval are always rejected; those that don't (read-only tools, codemode) run to completion inside the sandbox; OpenCode then starts the next step automatically and the model usually switches to pi_*. At most 3 steps per request.
- **Sandbox**: opencode serve may only write its own temporary directory and OpenCode's `log` / `shell` directories; `OPENCODE_DB` and `TMPDIR` point into the temporary directory, so it cannot touch the user's own OpenCode sessions. Reason: codemode's internal calls skip permission evaluation, raise no approval, and cannot be switched off with `policies` in config (see `opencode-free-errors-20261018.md` Sections 6 and 7). Refuses to run on non-macOS.
- **Long-lived service**: started by the first request (about 5 seconds: service + MCP connect + a 2-second wait for tool registration), later requests start in about 1.5 seconds; replaced when the launch arguments or the Pi tool list change or the process dies; after 50 requests or a failed request (other than a user cancel) a new service is pre-warmed in the background and takes over only once ready; stops after 15 idle minutes and when Pi exits (`kill -9` on Pi leaves it behind; clean up by hand).
- **Egress proxy (against IP blocks)**: when `~/.config/pi-opencode-free/proxy` exists (one proxy URL, mode 600) or the environment variable `PI_OPENCODE_PROXY` is set, opencode serve goes out through it (`HTTPS_PROXY`/`HTTP_PROXY`; local 127.0.0.1 bypasses it). The proxy URL carries a password, so it lives only in that file, never in the launcher environment, logs or handoff restart scripts; if it is configured but unusable, the bridge reports an error and does not fall back to a direct connection. To change egress, edit the proxy URL in the file; newly started OpenCode services pick it up.
- The local Basic password exists only in memory: never printed, never written to config, and OpenCode's OAuth/API credentials are never copied. Model calls and upstream authentication are still done by the official CLI.
- Only the fixed free model is called; the launcher refuses model / credential overrides and never switches to a paid model on 403, quota or rate limiting. Pi itself still has a manual `/model`; a user who deliberately switches to another provider is no longer using this free bridge.
- Usage comes from OpenCode SSE; with multiple steps, the last step's own tokens are taken (`usage.updated` is cumulative). The context limit of 200000 and output limit of 32000 come from the local official model catalogue (the official DeepSeek V4.1 Flash API is 1M / 384K; the free channel caps it at 200k / 32k), and the backend cannot be enlarged on that basis. The window declared to Pi is the input budget 168000 (= 200000−32000), so Pi compacts on its own at 168000−16384≈151.6k, leaving room for one turn of growth without hitting the bridge's pre-check; hence Pi's footer shows /168k.

## Limitations

1. Tested baseline: Pi 1.3.0, OpenCode 2.0.30 (2026-10-19). Depends on the official local v2 session / SSE / MCP APIs, which may change in future versions.
2. Free eligibility and quota are decided by OpenCode. The 403 text is an IP-based abuse verdict (detected abusive traffic from this IP address), unrelated to account or tool set; dense requests from one egress get blocked, and switching egress restores service. In that case the bridge reports a clear error; it does not impersonate another client or switch account or model.
3. Text only; image input and image tool results are explicitly refused.
4. Pi's thinking/sampling/maxTokens options do not control this official agent; the reasoning display comes from the actual SSE. The official default reasoning settings are kept.
5. MCP tools register asynchronously after `connected` with no signal, so the bridge waits a fixed 2 more seconds (stopgap); with the long-lived service only each service's first request pays those 2 seconds. Multi-turn and branching follow Pi's history; no separate hidden OpenCode conversation is reused.
6. Requests time out after 180 seconds by default, with no automatic SSE reconnect / replay. Aborting a local request does not guarantee that remote inference already submitted stops billing or releases quota right away.
7. A host crash or `kill -9` may leave the long-lived service or temporary directories behind; only a normal shutdown cleans up. Official OpenCode's own logs may retain history; this implementation does not change its global logging policy.
8. Pi tools run with the current user's permissions and are not sandboxed (the sandbox wraps only opencode serve). Run only in authorised directories / accounts; this feature is not wired into fleet supervisor/worker scheduling or production deployment.

## Difference from the old Console launcher

`start-pi-opencode.command` is still the entry point for **Pi accessing the OpenCode Console API directly**, and requires explicitly choosing a model with API access. Its refusal of free models is a restriction of that direct path; it does not mean the official OpenCode CLI's local session API cannot be used.

This feature follows the official session, Basic auth, `/prompt` and SSE APIs described in an `OPENCODE-PI-USAGE.md` reference document. It does not use the reverse-direction Pi gateway in section 5 of that document, and does not label other Pi models as DeepSeek.

## Verification

- `python -m pytest -q tests`: 589 passed (new regressions for native file open after external socket refusal, temporary file permissions / cleanup, open failure and version boundaries).
- Consuming-project entry fix: a full copy in the original root reproduced `projects/launchers/start-pi-full.command: No such file or directory`; after replacing it with the thin wrapper template, real `--check-only` and `--terminal --offline --version` runs pass, with the working directory correctly set to the consuming project's root.
- cmux launch contract: create / focus a workspace by default, `--terminal` does not open windows recursively, passing the working directory and arguments containing spaces / quotes / newlines, version check, long-command refusal, no silent fallback on creation failure; a real cmux `--check-only` passes; an independent cmux workspace was later also created from the consuming project's entry and the Pi TUI checked.
- Mock-service verification: subscribe before posting, deltas arriving before completion, UTF-8/CRLF/escape chunking, cross-session filtering, actual usage, cancel / timeout, process reaping, 403 / error / dropped stream, illegal tools not executed, Pi's real provider event contract.
- Real free model: Pi JSONL received 21 body deltas and 2 reasoning deltas and completed normally.
- Real tool round trip: Pi `read` → Pi `write` → model confirms again; the temporary test output was `LIVE-TOOL-731\nDONE\n`.
- Real Pi TUI (separate PTY): input submitted from the edit box, `PI-TUI-STREAMOK` received, Ctrl-D exits normally (exit 0); no existing cmux/fleet window touched.
- GUI launch problem reproduced and fixed: with a minimal `PATH=/usr/bin:/bin:/usr/sbin:/sbin` it used to fail with `env: node: No such file or directory`; after the fix, the Pi version check and the real cmux create / TUI check pass in the same environment. A real cmux input-box test also received `CMUX-FREE-READY`. No running fleet was restarted.
- **Real macOS file-open acceptance**: launched with `/usr/bin/open <consuming project root>/start-pi-deepseek-free.command` (not by running the script from inside cmux). Before the fix, Terminal plainly returned "访问被拒绝——只有在 cmux 内启动的进程才能连接" (access denied — only processes started inside cmux can connect); after the fix the same entry created `workspace:1000020019` / `surface:1000020034`, the UI showed the correct project directory and the free model, and real input received "**双击启动验证通过**" (double-click launch verified). This evidence covers the permission boundary that the earlier internal launch checks missed.
- These are local development checks, not an independent review, merge or production deployment.
