# Launchers

Codex/Pi startup `.command` files related to fleetmux live here.

- `start-pi-full.command` — start Pi in this project with full local access.
- `start-pi-codex-dangerous-full.command` — open a cmux workspace running Pi with the OpenAI Codex provider.
- `start-codex-flashnext.command` — start local FlashNext through Pi using the `flashnext/...` model; like DeepSeek Free, it opens a new cmux workspace (`pi · Flash Next`) by default, `--terminal` stays in the current terminal, `--check-only` opens nothing. `PI_FLASHNEXT_PROVIDER=<name>` uses another `models.json` provider instead of `flashnext` (e.g. a second machine serving Flash Next); its sessions go to `sessions/<name>` and the workspace is named `pi · Flash Next (<name>)`.
- `start-dsh-flashnext.command` / `start-dsh-deepseek.command` — the same models in DSH (DeepSeek Harness, `npm install -g @deepseek-ai/dsh`) instead of pi. DSH has no terminal UI, so the cmux workspace (`dsh · Flash Next` / `dsh · DeepSeek`) runs `dsh web` on a free port and opens the browser. Shared core `start-dsh.command` builds a DSH patch from the same `~/.pi/agent/models.json` provider (`flashnext` / `workbuddy-ds`, the latter pinned to magpie's `workbuddy-ai/deepseek-v4.1-flash-sg`); the key goes only into the dsh process env. One DSH home per provider under `~/.dsh/homes/`, telemetry off. `--terminal` and `--check-only` work as above. Thin wrappers to copy into a project root: `launchers/compat/start-dsh-*.command`. Personal defaults (e.g. `DSH_PURGE=on`) go in `~/.fleetmux/launchers.env` (`KEY=value` lines, outside the repo; `FLEETMUX_LAUNCHER_ENV` points elsewhere); an explicitly set variable wins.
- `start-dsh-tui.command` / `start-dsh-tui-flashnext.command` — the DeepSeek (magpie) / FlashNext models in dsh-TUI (`@deepseek-harness-tui/dsh-tui`, pinned 0.18.0), the terminal UI for DSH, in cmux workspaces `dsh-tui · DeepSeek` / `dsh-tui · Flash Next`. Same core with `DSH_UI=tui`; on first run each installs the `dsh-tui` profile into that provider's DSH home. All DSH launches (web and TUI) default to `DSH_PERMISSION_MODE=danger-full-access` (no sandbox, no approval prompts; override by setting the variable to `read-only` / `workspace-write`). `--terminal` and `--check-only` work as above. Root wrappers: `launchers/compat/start-dsh-tui.command` / `launchers/compat/start-dsh-tui-flashnext.command`.
- `start-pi-opencode.command` — Pi terminal UI using OpenCode Console (`opencode`, not Go), with separate `sessions/opencode` history. Run it inside a cmux terminal; double-clicking uses macOS's default terminal, not a new cmux workspace.
- `start-pi-autohandoff.command` — continuation-window launcher for the auto-handoff extension (see below). Takes a handoff file path, opens a new cmux workspace named `autohandoff <hop>:<task>` and hands the file to the new session through `PI_HANDOFF_FILE`. Works with both launcher shapes: the local canonical (`start-codex-flashnext.command` with `--terminal`) and the Studio-style canonical (no `--terminal`, no `PI_WORKDIR`), where it calls `pi` directly with `--model $PI_AUTO_HANDOFF_MODEL`.
- `install-auto-handoff.sh` — installs the extension and continuation launcher into a pi agent dir. `install-auto-handoff.sh [--agent-dir DIR] [--host HOST]`; copies `extensions/auto-handoff.ts` and `launchers/start-pi-autohandoff.command` into `<agent-dir>/extensions/` (default `~/.pi/agent`), `--host HOST` goes through ssh to that host. Both files must stay in the same directory: the extension resolves the launcher through a sibling path first.
- `install-session-board.sh` — installs the cross-session board extension (`extensions/session-board.ts`, [contracts §7](../docs/contracts.md)) into `<agent-dir>/extensions/` (default `~/.pi/agent`, `--agent-dir DIR` to override) and symlinks `fleetmux/session_board.py` beside it, so pi runs the same Python as the Claude Code hooks of this checkout.

### Auto-handoff (pi context relay)

`extensions/auto-handoff.ts` is the pi counterpart of the Claude Code `/handoff` skill: when the context reaches a threshold it writes a handoff contract, opens a continuation cmux workspace and shuts the old session down. Install it at `<agent-dir>/extensions/auto-handoff.ts` (`~/.pi/agent/extensions/` by default, auto-discovered by pi) or load it with `pi -e`; an already running session picks it up with `/reload` once it is idle.

- Trigger: `turn_end` when `ctx.getContextUsage().percent >= PI_AUTO_HANDOFF_PCT` (default `80`). Disable with `PI_AUTO_HANDOFF=0`; manual trigger is `/auto-handoff [instructions]`.
- Contract: generated with `ctx.modelRegistry.complete()` over the branch (compaction-aware, same as pi's own `examples/extensions/handoff.ts`), written to `~/.handoff/<session-id>.md` (or `PI_HANDOFF_DIR`) in eight fixed sections; the contract explicitly forbids "should pass" style claims.
- Continuation: the launcher starts a new cmux workspace; the new session reads `PI_HANDOFF_FILE` from `session_start` and injects it as its first prompt, so the long contract never has to fit in the cmux `--command` line.
- `FLEETMUX_PI_DIR`, `PI_AUTO_HANDOFF_LAUNCHER`, `PI_HANDOFF_DIR`, `PI_HANDOFF_TASK`, `PI_HANDOFF_HOP` override the defaults; `# parent-session: <file>` in the contract footer records the lineage.

### DeepSeek V4.1 Flash Free — Pi shell with real streaming

Use `./launchers/start-pi-deepseek-free.command`: by default it opens and focuses a new **cmux workspace** (`pi · DeepSeek Free`), starting cmux if necessary. Double-clicking the launcher also routes Pi into cmux. With the default `cmuxOnly` socket policy, external Terminal/Finder launches use cmux >=0.65.0's official macOS `.command` file-open handler, not unauthorized socket control; socket security settings are unchanged. Use `--terminal` to stay in the current terminal (required for JSON/RPC automation); `--check-only` opens no workspace. It keeps Pi's UI, sessions and tools, and calls the **official OpenCode CLI's local session/SSE API**, not the Console inference endpoint. Text and thinking arrive live; no paid fallback or OAuth credential copying. Authenticate in OpenCode first. Set `PI_WORKDIR` to choose the project. See [usage, architecture and limitations](../docs/opencode-free-pi.md).

### OpenCode Console through Pi (separate direct-API path)

The locally selected OpenCode App model, `deepseek-v4.1-flash-free`, **cannot be used through Pi's direct Console API path**: a direct request to the public inference endpoint returned HTTP 403 `FreeTierError`, stating that the free tier is only available inside OpenCode. The `start-pi-opencode.command` launcher refuses this model and never silently substitutes a paid model or copies App OAuth credentials. This restriction does not describe the official-CLI shell above.

First obtain a Console API key and configure it with Pi's `/login opencode`, or provide `OPENCODE_API_KEY` in the launching terminal. Then explicitly choose an API-accessible model (check its pricing/availability in Console):

```bash
# Example only: this is a different, potentially paid model, not the App's free tier.
PI_OPENCODE_MODEL=deepseek-v4.1-flash ./launchers/start-pi-opencode.command
```

`PI_WORKDIR` overrides the working directory (default: this repository); `PI_OPENCODE_THINKING` defaults to `off`. Arguments such as `--continue` and `--resume` pass through to Pi. `--check-only` validates launcher parameters without starting Pi or making an API request; it does not prove authentication or model availability. With no `PI_OPENCODE_MODEL`, the launcher stops with setup instructions. Never commit API keys.

Projects that use the fleet may keep only thin compatibility wrappers that `exec` these files. For DeepSeek Free, copy **`launchers/compat/start-pi-deepseek-free.command`** into the sibling project's root. Do not copy the full launcher there: its relative root lookup would point at the wrong directory. The wrapper preserves the project's working directory and delegates to the canonical launcher. Do not put fleet launcher logic back into those projects.
