# fleetmux

[![tests](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml/badge.svg)](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml)

**English** · [中文](README.zh-CN.md) · [Français](README.fr.md) · [Español](README.es.md)

**Run AI agent teams the way you run containers.** Run a team, not a single assistant: fleetmux runs one supervisor agent and N worker agents as visible
terminal sessions. The supervisor only judges and dispatches; workers only execute; a daemon, not the models,
decides when the work is done. Sessions near their context limit hand over to a fresh session with a written note,
so one task can run for hours across many model sessions. Official **Claude Code**, official **Codex CLI**, **Pi**
(Codex models and local models) and **opencode** mix in one fleet, in any supervisor × worker pairing that has
passed a real smoke run (`fleetmux/config/fleet.yaml#cli_pairs`).

**In daily use.** Since October 2026 it has run resident service fleets and scheduled task fleets on three Macs
for a live trading operation: an ops team that starts and stops the other teams, a trading desk, a strategy review
team that answers price and indicator triggers, and a scheduled research team. Each team has an AI lead and AI
workers; the ops team runs the trading desk and the review team as its sub-fleets.

## A project manager, not a secretary

Personal-assistant agents can already spawn sub-agents, run scheduled jobs and fail over between models, but a
sub-agent's result still goes back to a model to judge, long tasks rely on context compaction, and the model is
whatever the caller or the config names. fleetmux adds what a project manager does:

1. **Delivery accepted by the program.** Whether the outputs exist, whether they contain what they must and whether
   the tests pass is decided by the daemon, not taken from the model's own report.
2. **Sessions that continue without end.** Near the context limit a session writes a handoff note and a fresh session
   takes its place; a frozen session restarts in place; daemon and supervisor watch each other.
3. **Dispatch by a capability matrix.** Before every dispatch the advisor picks the model and effort from a
   capability matrix (domain × task type × model, each cell with evidence), and the pick must pass a check against
   that matrix; a usage wall moves the slot to the next usable model on its role's chain, or pauses.
4. **Visible throughout.** Every agent is a visible terminal session you can watch and step into at any time.
5. **Official CLIs, your own login.** Each agent runs in its vendor's own CLI under your own login; no token is
   extracted, no client is impersonated and no API call goes through a proxy.
6. **A budget per task.** A budget card caps calls, rounds, hours and per-account quota; a usage-wall table drops
   a model tier or pauses the run so it can resume later.
7. **Teams of teams.** A worker can become the lead of its own sub-fleet, so one resident fleet can run several
   business lines, each started and stopped on its own clock.

We learned why this matters from our own setup: an assistant gateway we ran lost its chat channels for months
and nothing outside it noticed, because nothing outside it was checking. In a fleet, the daemon checks every
delivery.

Built by Justin Wang (Toronto), AI systems engineer: [LinkedIn](https://www.linkedin.com/in/justinwangto) · [X](https://x.com/alaboluo).
Available for freelance work: AI setup from zero, agent harness tuning, LLM capability elicitation and multi-agent orchestration. DM me on X.

[Design baseline](docs/design.md)

**Highlights**

- **Literal acceptance.** A task ends only when declared artifacts exist and contain what they must;
  false `COMPLETED` claims are counted and block the task.
- **Context rotation.** At a configurable share of the context window the session writes a handoff note
  and a successor (`worker-1` → `worker-1.1`) continues in the same tab.
- **Sleep recovery.** Close the laptop lid, move, open it and reconnect: the run continues. The daemon tells
  a sleeping machine from a lost link, retries a startup that failed on the network with a backoff, and
  restarts no session until the network answers.
- **Private Redis per run** (Unix socket only, no TCP) carrying a bounded message bus with
  generation-checked deliveries, so a late callback from a retired session can never act.
- **Watchdog and brake.** Five-state stall detection per slot; a per-task budget card (calls, rounds,
  hours, per-account quota) and a usage-wall table that moves a walled slot down its model chain or pauses resumably.
  Accounts live in the `accounts` block of your private copy of the config file, never in code.
- **One config file.** Every table (CLI pairs, lifecycle modes, walls, watchdog dialogs, model cards,
  profiles, accounts) is a block of `fleetmux/config/fleet.yaml`; a private `~/.fleetmux/fleet.yaml`
  in the same format overrides the blocks an operator may change.
- **Official CLIs, your own login.** Claude Code always runs as the official `claude` TUI with your own login, and
  Pi is never given Claude credentials; Codex models run in the official Codex CLI or through Pi's own OpenAI login.
  fleetmux does not extract or forward tokens, impersonate a client or proxy API calls.
- **Sub-fleets.** A worker can become the supervisor of its own child fleet.
- **2,000+ tests** (in-memory Redis, fake cmux, synthetic transcripts; real Redis where it matters).

```
 cmux workspace "Fleet agents" (one page)
 ┌───────────────────────────────┬────────────────────────┐
 │ supervisor-1.2                │ [worker-1.1] [worker-2] │
 │ supervisor TUI (selected)     │ worker tabs            │
 │                               │                        │
 ├───────────────────────────────┤ selected worker's TUI  │
 │ [history] [daemon]            │                        │
 │ retired screens + handoffs    │                        │
 └───────────────────────────────┴────────────────────────┘
 daemon: the fleet daemon, private Redis socket, routing, verification and watchdog.
```

## Use cases

Every `start` names a `--lifecycle`; it decides what "done" means and how the run closes
(`fleetmux/config/fleet.yaml#lifecycle_modes`). These are the shapes it is built for; the scheduled report, the resident service and feature development run in daily use.

- **Operations that may only use local models.** Some environments allow only models running on your own
  machines, and local models are poor at working together: they lose track of who is doing what, report work as
  done when it is not, overflow a small context window, or freeze mid-turn. Part of that is the model, part is the
  harness around it. fleetmux takes the collaboration out of the models and gives it to the daemon. The supervisor
  only picks the next step and a worker only carries one out; the daemon decides when a turn has ended, whether the
  result exists and says what it must (files checked literally, service replies against `reply.required`; three
  false `COMPLETED` claims block the task), when a session hands over to a fresh one (window and ratio per model),
  and when a frozen session is restarted. Supervisor and workers can all be local models through Pi
  (`--supervisor-cli pi --worker-cli pi --pi-model <provider>/<model>`); `--max-busy` keeps concurrent prompts under
  the inference server's memory limit; the fleet's private Redis listens on a Unix socket only and opens no port.
- **Trading watch: strategies handed to AI, reviewed on a trigger.** Your own program watches prices and
  indicators; the model is not polled all day. When a price level or an indicator parameter is crossed, the program
  sends one request, e.g. `{"to": "exit-1", "payload": {strategy, position, trigger, data}, "reply": {"required":
  ["verdict", "param", "evidence"]}}`, and the worker that owns that role (one prompt per role through `--roles`, its
  tools and skills through `--worker-kit`) runs a full review: it reads the position and the market data, rechecks
  the strategy's assumptions and answers in JSON. A request that names `to` skips the supervisor's turn; triggers
  that arrive together queue under `--max-busy`; the daemon checks the reply's fields before your program reads it
  (`fleetmux results --drain`). Requests have no global deadline: one that expires holds only its own slot, and a
  frozen Claude Code worker resumes its own session and gets the same request again. The fleet places no
  orders and knows no calendar: your program decides what to
  do with the verdict, sends any order through its own checks, and `pause` / `resume`s the fleet around market hours.
- **One mid-strength model leading a crowd of weak free models on routine work.** Translation, drafting,
  summarising and sorting are mostly volume. A mid-strength supervisor (through Pi or Codex) cuts the job into small
  pieces, one chapter, one batch of documents, one topic, and declares the output files the task must produce, how
  long each must be and what each must contain. Workers on free models through Pi do the pieces (one model per roster
  row, `pi_model:`). A weak model only ever sees one small piece: `"fresh": true` on a dispatch item starts a clean
  session in the same slot so the last piece does not leak into the next, and a long piece hands over to a new
  session before a small window overflows. What a weak model gets wrong is caught by the program instead of trusted:
  the task closes only when every declared output exists, is long enough and contains the required text, and three
  false `COMPLETED` claims block it. Free endpoints are flaky and capped: rate limits and usage walls go through the
  wall table, which moves the slot down its model chain or pauses the run resumably; `--max-busy` stays under the free tier's
  concurrency; workers are added or removed by editing the roster YAML while the fleet runs. For a quality pass, give
  one roster row a stronger `pi_model:` and use it as the reviewer.
- **Scheduled research report** (`scheduled`). The caller's own scheduler starts a fleet at fixed times of
  the business day (with `clock_gate` on, the fleet itself refuses to launch on a closed market day): a supervisor plus two Claude Code workers that carry research skills and a web-search MCP
  through `--worker-kit`. The goal declares the report file and the sections it must contain; the daemon
  accepts it literally, gives the run a hard time budget and closes the workspace when it ends.
- **Resident judgment service** (`service`). Another program sends JSON requests with `fleetmux request`;
  a request that names `to` goes straight to that role's worker (`--worker-names exit-1,reentry-1,...`, one
  prompt per role through `--roles`). The daemon checks each reply against `reply.required` / `reply.allowed`,
  and the caller holds the fleet outside its working hours with `pause` / `resume`. See *Service mode*.
- **Feature development with independent review** (`dev_task`). The supervisor splits the feature, workers build
  it, and separate reviewer workers judge it; review is capped at three rounds and leftover P0 / P1 go to you
  instead of a fourth round. The task is done only when the delivery gate's `test_cmd` has passed and the work
  is merged and handed to the release queue. Interface contracts (`"contracts": true`) freeze shared interfaces before work starts.
- **Multi-phase program** (`program`). Each phase runs as its own child `dev_task` fleet; a ledger records
  the phases, and the program ends when the last phase is merged and reviewed.
- **Review until it passes** (`review_loop`). Review → fix → review, at most three rounds, then a report.
- **A task that outgrows one worker** (development). When a worker keeps a flowing workflow busy past a
  configurable time, it is asked to propose a split; on the supervisor's approval (with chosen models) the worker
  hands off and its successor becomes the supervisor of a child fleet, shown live in the parent's worker area.
  A worker can also ask for this itself. See *Sub-fleets* and `docs/design.md` §44.
- **One resident operations fleet for several lines of work** (development). A main supervisor in service mode
  owns one worker per business line; each of those workers supervises a named child fleet declared in the roster,
  which an outside program can start and stop on its own clock (`docs/design.md` §29).
- **Day and night models.** `start --roster day.yaml --night-roster night.yaml` and `fleetmux night on|off`
  swap each worker whose profile differs to its night profile (e.g. a free or local model) at an idle boundary, same slot names, and
  tell the supervisor what is done and what is left (a full day → night → day run is not yet accepted end to end). `start --resume` brings an interrupted run back the same way.
- **Run anywhere, watch from anywhere.** On Linux the `fleetmux` backend replaces cmux; `fleetmux attach` (also
  over `ssh -t`) or `fleetmux gui` shows the live page from another terminal, including a phone terminal app.

Chat channels (Telegram, Feishu, WeChat, email, Slack) are a second keyboard for the supervisor, with results and
"waiting for a human" pushed back to the same chat: `fleetmux channel`, [contracts §8](docs/contracts.md).

## Why

Running agents as interactive TUIs instead of headless `codex exec` calls keeps them visible: you can
watch every tab, type into one if you must, and close any of them with the ✕. The daemon never scrapes
screens. Codex's `notify` hook calls back into the daemon at the end of every turn, and the daemon pastes
the next message only when a session is idle.

What the daemon enforces, independently of what the models claim:

- **Acceptance is literal.** A task ends only when every declared artifact exists, is long enough and
  contains the required text. Three false `COMPLETED` claims block the task.
- **No host commands from models.** The supervisor can only declare files to check, never commands to
  run. Codex agents are sandboxed (`workspace-write`, network on); Pi and the optional Claude workers are not.
- **Context rotation before overflow.** Before each delivery the daemon reads the session's token usage
  from the selected CLI's rollout/transcript or Pi context API. At `--handoff-ratio` (default 60 %) it asks the session to write a
  three-section handoff note, merges it into a machine skeleton, saves the old screen to the history
  page, and starts a successor in the same place (`worker-1` → `worker-1.1` → `worker-1.2`) that reads the note. If the model
  does not write, the skeleton is used; missing model-written notes alone do not block rotation.
  Window/process failures still follow the normal failure path.
- **Stall watchdog.** A busy agent whose rollout file has not changed for `--stall-timeout` seconds
  (default 20 min; transport v2 also has the 5-minute multi-signal `--stall-window`, docs/design.md §24) is aborted and reported to the supervisor; a stalled supervisor gets one retry, then
  the task is blocked for a human. In transport v2 a frozen official Claude worker first resumes its own session
  (`claude --resume`, same tab) and gets its work again; only a resumed session that freezes again starts fresh (§43).
- **Stale callbacks are dropped.** Every spawn bakes a generation number into the notify hook; a late
  callback from a retired session is ignored.

## Requirements

| Item | Version | Notes |
|---|---|---|
| OS | Linux or macOS, x86_64 or ARM64 | The Linux `fleetmux` binary is static. The desktop window `fleetmux-web` needs glibc >= 2.39 and WebKitGTK 4.1 (Ubuntu 24.04 desktop has both). |
| Python | 3.14 | `scripts/install.sh` provides it through [uv](https://docs.astral.sh/uv/). The system Python is not used. |
| `redis-server` | >= 7.0 | Each run starts its own private instance under the release's `.runtime/redis/<token>/` (socket, PID, log, data). TCP is off; it never touches `localhost:6379`. |
| `tmux` | >= 3.4 | Sub-fleets show one session in two tabs. |
| `git` | >= 2.30 | The delivery gate fast-forwards branches. |
| One agent CLI | see `deps.yaml` | [Claude Code](https://code.claude.com), [Codex](https://github.com/openai/codex), [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) or [opencode](https://opencode.ai), logged in. Install only the ones your roster uses. pi also needs Node.js >= 22. |
| cmux (macOS only) | >= 0.65.0 | The default terminal on macOS. Run `start` inside a cmux terminal, or set cmux's *automation -> socket control mode* away from `cmuxOnly`. Without cmux, macOS uses WezTerm or `--backend fleetmux`. |

`scripts/check_deps.py` checks all of these against `deps.yaml` and prints one line per item.

Notes for Codex agents:

- The daemon parses Codex's rollout `.jsonl` files (`token_count`, `task_complete`, subagent metadata). If a Codex
  release changes that format, pin the version that works for you.
- Any model Codex can talk to works: OpenAI API, ChatGPT login, or a local OpenAI-compatible server. The daemon reads
  the context window from the rollout.
- Agents run in an **isolated Codex home** (`--codex-home`, default `~/.fleetmux/codex-home`). `auth.json` and the
  profile file are symlinked from your `~/.codex`, so the login is shared, but your hooks, MCP servers, plugins and
  skills do not run inside agents (`skills.include_instructions=false`). Per-agent "trust this folder" entries are
  written there automatically.

## Install

One command: `fleetmux <subcommand>` (e.g. `fleetmux start`, `fleetmux status`, `fleetmux channel`). The binary runs
its own terminal subcommands (`server`, `attach`, `reap`, `web`, `gui --runtime D`) and hands every fleet subcommand
to the Python CLI of the same release (docs/design.md §50). A machine set up under the project's earlier name
moves its data over once with `fleetmux home migrate` (docs/design.md §50.1).

### Quick install (Linux and macOS)

1. **Install the system packages.**

   ```bash
   sudo apt install redis-server tmux git     # Debian / Ubuntu
   brew install redis tmux git                # macOS
   ```

2. **Run the installer.** It needs only `curl` and `tar`. It installs the latest release into
   `~/fleetmux-releases/v<version>`, gets Python 3.14 through uv, downloads the prebuilt binaries (sha256-checked),
   links `fleetmux` into `~/.local/bin` and runs the dependency check. No root, no compiler.

   ```bash
   curl -fsSL https://raw.githubusercontent.com/justindbca/fleetmux/main/scripts/install.sh | bash
   ```

3. **Install and log in to one agent CLI**, for example Claude Code:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash && claude
   ```

4. **Check.** `fleetmux start --help` prints the options. Run `~/fleetmux-releases/v<version>/.venv/bin/python
   ~/fleetmux-releases/v<version>/scripts/check_deps.py` again after step 3: every line must say `ok`.

Options: `FLEETMUX_VERSION=0.2.1` installs a given release; `FLEETMUX_RELEASES` and `FLEETMUX_BIN_DIR` move the
install and the links (see the header of `scripts/install.sh`).

### From source (development)

```bash
git clone https://github.com/justindbca/fleetmux && cd fleetmux
pip install -e .
```

Add a Codex profile for the agents. Codex ≥ 0.156 keeps each profile in its own file
(see [`codex.config.example.toml`](codex.config.example.toml)):

```toml
# ~/.codex/fleet.config.toml   (selected with `codex --profile fleet`)
model = "gpt-5.5"
```

Nothing else: the fleet creates its own Codex home for the agents and answers Codex's
"trust this folder?" prompt for each agent directory itself.

### Release install on a target machine (macOS or Linux)

```bash
scripts/install_release.sh <sha> <tarball> [--bin-dir DIR] [--tool-sha SHA] [--test] [--activate]
```

One application release per full source SHA: the tarball (a `git archive <sha>`) is unpacked into
`$FLEETMUX_RELEASES/<sha>` (default `~/fleetmux-releases`), the release's `.venv` is built from the pinned
requirements, an already-built `--bin-dir` (`fleetmux`, `fleetmux-web`, `SHA256SUMS`, `manifest.json`) is verified
and both binaries land in `<release>/bin`, and `scripts/check_deps.py` runs with `<release>/bin` first on `PATH`.
An existing release is never joined or overwritten (the directory is created with a bare `mkdir`), a failed
install removes only the directory that run created, and `<sha>` must be 7-40 hex characters because it is the
release directory name. `--test` runs the release's own suite and keeps the complete log outside the release
dir, at `<releases>/.logs/<sha>-<timestamp>.log`, printing that path (so a failed run stays diagnosable);
`--activate` is the only step that moves `~/fleetmux-current`. On success the installer prints one explicit
start command, and records it as `start_command` in `<release>/bin/installed.json`, so `fleetmux` resolves to
this release instead of a stale `~/.local/bin` build:

```bash
PATH=<release>/bin:$PATH <release>/.venv/bin/fleetmux-py --help
```

`--tool-sha` records the installer-tool commit separately from the application `<sha>`. An installed release is
not a native PASS and not an acceptance receipt.

## Use

Open a cmux terminal, configure your Pi provider/model and replace `local/my-model` below.
New starts require Redis v2 and an explicit development opt-in. Without `--supervisor-cli` / `--worker-cli`, `start`
takes the default pair from `fleetmux/config/fleet.yaml#cli_pairs`: a **Pi supervisor on `openai-codex/gpt-6-astra` (thinking
high) with official Claude Code workers**. That table lists every supported pair and its status
(see "Redis v2 development runtime"); a pair it does not list is refused. `start` also requires `--lifecycle <mode>`
(`service`, `dev_task`, `program`, `scheduled`, `review_loop`, `smoke`; see `fleetmux/config/fleet.yaml#lifecycle_modes`); an
`unverified` pair starts only with `--lifecycle smoke`.
The Codex-specific setup and launch examples below are retained only as legacy v1 reference.

```bash
fleetmux start --workers 2 --supervisor-cli pi --worker-cli pi --lifecycle <mode> \
  --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
echo '{"goal": "Survey the three most-cited papers on X and write a comparison",
       "hours": 4,
       "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}' \
  | fleetmux task -                           # hand the goal to the supervisor
fleetmux status                               # task + per-agent state, context usage, inbox depth
fleetmux stop                                 # cancel; confirm runtime retirement before Redis shutdown
```

### Terminal backend: cmux, WezTerm or fleetmux

fleet talks to the terminal through one pluggable backend. `start --backend cmux|wezterm|fleetmux` picks it, and
the choice is written into the run's `config.json`: the daemon, the actors, `start --resume` and every
sub-fleet follow that value, and a run started before this flag existed stays on cmux. Without the flag
macOS is unchanged — cmux when cmux is installed, WezTerm on a Mac without cmux — and **Linux (including
WSL2) defaults to fleetmux since M3**; `--backend wezterm` (or `--backend cmux`) still selects the other
backends explicitly. An explicitly chosen backend that cannot run on the machine is refused before anything
is provisioned, naming what is missing.

```bash
fleetmux start --backend wezterm --workers 2 --supervisor-cli pi --worker-cli pi \
  --lifecycle <mode> --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
```

* **cmux** (default on macOS, the layout described above): supervisor top-left, history browser below,
  workers as tabs on the right.
* **WezTerm** (`--backend wezterm` on Linux / WSL2, or the default on a Mac without cmux; needs `wezterm` and
  `wezterm-mux-server` ≥ 20240203 on `PATH`):
  each run starts its **own private mux server** (its own socket, never your WezTerm). There is no
  browser surface and no tabs inside a split, so the layout degrades: one window, tab 1 with the
  supervisor on the left and `daemon` split below it, one tab per worker. The history page is not
  embedded — fleet prints `<run>/history.html` in `daemon` and opens it with `open` / `xdg-open` when
  a desktop is available. Named sub-fleets still work through tmux.
* **fleetmux** (the default off macOS since M3; on macOS only with `--backend fleetmux`): fleet's own
  fixed-layout terminal written in Rust (`rust/`, MIT) — see the subsection below.

#### fleetmux backend (Linux default since M3)

Each run gets **one private `fleetmux server`** that owns every agent's PTY and screen. You look at it with
`fleetmux attach`, which is a separate client: open and close it whenever you like, open several at once, run it
over `ssh`. Closing a viewer never stops an agent.

* **Install** (one of):
  * build from this checkout: `cd rust && cargo install --path crates/app`, or `cd rust && cargo build
    --release` and put `target/release/fleetmux` on `PATH`. Run cargo inside `rust/` so that the toolchain
    pinned by `rust/rust-toolchain.toml` is used. On Linux, building the workspace also needs
    `libwebkit2gtk-4.1-dev` (for the `fleetmux-web` window, M2.5; `apt-get install libwebkit2gtk-4.1-dev`);
  * `scripts/install_fleetmux.sh [--dest DIR]` installs the prebuilt binary pinned by `deps.yaml`
    (`programs.fleetmux.min`) into `~/.local/bin` by default and refuses it unless its sha256 matches. It
    downloads from the GitHub release `v<min>` (`.github/workflows/release.yml`). If that release has not been
    published yet, the script fails and installs nothing; build from source instead.
  * `$FLEETMUX_BIN` points fleet at a specific binary; otherwise `fleetmux` on `PATH` is used.
    `fleetmux --version` must print `fleetmux <x.y.z> (protocol 1)` with a version of at least `deps.yaml`'s
    minimum, or `--backend fleetmux` is refused before anything starts (`scripts/check_deps.py --backend fleetmux`
    reports the same).
* **Start and watch**:

  ```bash
  fleetmux start --backend fleetmux --workers 2 ...    # prints: Watch it with: fleetmux attach --runtime <dir>
  fleetmux start --backend fleetmux --attach ...       # same, then becomes that `fleetmux attach`
  fleetmux attach --runtime <dir> [--no-mouse]           # from any terminal, as many times as you like
  ```

  `--attach` is refused on other backends. The runtime directory is `<state dir>/fleetmux/<session token>/`.
  It holds `ctl.sock` (0600, the directory 0700, same-uid peers only), `server.json`, `panes.json`,
  `layout.json` (your ratios survive a server restart), `start.lock` and `server.log`. fleet derives the path
  from the run, so every fleet process finds the same server.
* **Watching a fleet on another machine** (fix-m25-2): `ssh -t <host> fleetmux attach --runtime <dir>` shows the
  terminal, but that viewer runs on `<host>`, so `C-b o` can only open a window there. To get the history window
  on your own desktop, run the viewer on your machine and forward the server's socket with your own SSH:

  ```bash
  # On your machine (the one with the desktop). Replace every <...> placeholder. <dir> = the runtime directory on
  # <host>, an absolute path: `fleetmux start` printed it there ("Watch it with: fleetmux attach --runtime <dir>").
  # Use a NEW short private directory every time, and the same $L in both terminals (set it in each of them).
  L=$HOME/.local/state/fmr/<host>-<session token>-<n>   # <n>: a number you have not used before (1, 2, ...)
  mkdir -p -m 0700 "$(dirname "$L")"                    # terminal 1: the parent (already there is fine)
  mkdir -m 0700 "$L"                                    # terminal 1: fails if $L exists -> stop, pick a new <n>
  ssh -N -o ExitOnForwardFailure=yes -o StreamLocalBindUnlink=no \
      -L "$L/ctl.sock:<dir>/ctl.sock" <user>@<host>     # terminal 1: keep it open while you watch
  fleetmux attach --runtime "$L"                        # terminal 2, with the same L
  ```

  * Only `ctl.sock` is forwarded. The control `hello`, the view connection and the web commands all use it, and
    the history page comes over it too (pushed to your machine and kept at
    `<$XDG_RUNTIME_DIR or ~/.local/state>/fleetmux-web/<first 24 chars of the last part of $L>-<pid>/page/history.html`,
    0600). The window, its `web.sock` and the clipboard are local. No TCP port is opened.
  * Install `fleetmux` (and, for the window, `fleetmux-web` of the same version) on your machine as well; both ends
    speak protocol 1.
  * Log in as the user the fleet runs as on `<host>`: the server accepts only peers with its own uid, and `sshd`
    connects as the login user. The remote `sshd` must allow unix-socket forwarding (`AllowStreamLocalForwarding`,
    on by default; OpenSSH 6.7 or later).
  * `$L/ctl.sock` must fit a unix socket path (at most 103 bytes on macOS, 107 on Linux), so keep `$L` short.
  * Identity: `fleetmux attach` checks that the socket it connected to is held by your own uid (the kernel's peer
    credentials; your `ssh` process here) and refuses when it cannot tell. It does not compare the remote server's
    uid with yours: two machines number users differently. It trusts whatever your own SSH forward reaches, so
    forward only to hosts you trust. On one machine nothing changes: the peer is the server itself.
  * If `attach` cannot connect it prints `$L/server.log`, which does not exist locally; read the server's log
    with `ssh <host> tail -n 20 <dir>/server.log`.
  * Nothing is ever replaced: with `StreamLocalBindUnlink=no` and `ExitOnForwardFailure=yes`, `ssh` exits instead
    of removing a `$L/ctl.sock` that already exists. If `mkdir` or `ssh` says the directory or socket is in use,
    stop there and start again with a new `<n>`; do not delete or overwrite what is there.
  * When you are done, stop that `ssh` with Ctrl-C in terminal 1. The socket file stays in `$L`; leave it, and use
    a new `$L` next time.
* **Watching in a window: `fleetmux gui`** (M2.6): on a desktop you can watch the fleet in a native window instead
  of a terminal. The window shows the same screen as `fleetmux attach` (same layout, tabs, keys and mouse), and
  while the lower-left `history` tab is selected (or zoomed with `C-b z`) the history page is drawn right in that
  cell, so there is no separate window. Switch to `daemon` or another lower-left tab and the page hides, showing the
  terminal `history` view under it.

  ```bash
  fleetmux gui --runtime <dir> [--devtools]             # needs fleetmux-web of the same version
  fleetmux start --backend fleetmux --gui ...         # on a desktop: start, then becomes that `fleetmux gui`
  ```

  * On a desktop, `fleetmux start` prints `Watch it in a window: fleetmux gui --runtime <dir>` before the usual
    `Watch it with: … attach …` line. `--gui` and `--attach` cannot be combined, and `--gui` is refused without a
    desktop.
  * Click into the page to use it (the lower-left group gets the focus); the prefix key still works inside the page
    and gives the keyboard back to the terminal. `C-b o` selects the history tab and focuses the page. `C-b d`
    or closing the window detaches (exit 0); the agents keep running. `browser_goto` / `browser_url` /
    `reload_browser` from the fleet act on the embedded page.
  * Copy and paste are handled by the page itself: Cmd-C / Cmd-V on macOS, Ctrl-Shift-C / Ctrl-Shift-V on Linux
    (Linux needs `wl-copy` / `wl-paste` on Wayland or `xclip` on X11); an Option-click selection is also copied as
    soon as the mouse button comes up. Cmd/Ctrl `=` `-` `0` change the font size.
  * Exit codes: `0` detached, window closed or server gone; `2` usage error; `3` cannot connect (as `attach`);
    **`4` cannot open the window**: no graphical session (Linux without `DISPLAY` / `WAYLAND_DISPLAY`, e.g. under
    `ssh`), `fleetmux-web` missing or of another version, WebKitGTK missing, the web runtime path too long, or the
    window not ready within 3 s. It prints one line with the reason plus `use: fleetmux attach --runtime <dir>`,
    and never turns into a terminal attach by itself.
  * Another machine: forward `ctl.sock` exactly as above (same `$L`, `mkdir`, `ssh -N -L …` in terminal 1), then
    run `fleetmux gui --runtime "$L"` in terminal 2 instead of `fleetmux attach`. The history page comes over the
    same socket into the local `page/history.html` (0600); no TCP port is opened.
* **Layout**: fixed. Supervisor tabs top left; `history` (a terminal view of the run history,
  `python -m fleetmux.history_view`) and `daemon` bottom left; worker tabs on the right. A successor takes
  its predecessor's tab position.
* **Tabs and separators**: each tab is a padded, clickable text block sitting on its group's border row, with a
  thin `│` between neighbours; a click selects a tab only inside its block (not in a gap or on the border after
  the last tab). The selected tab of the focused group is a bright bold label on a dark-grey block, the selected
  tab of an unfocused group a grey label on the same block, other tabs plain grey text with no background, and
  `•` marks a tab with unseen output. Tab colours are the 16-colour ANSI palette, so each terminal (and the
  gui's theme) shows them in its own shades; on a light terminal theme they are faint but readable. All three
  pane borders stay visible (the focused pane's border is highlighted). Drag the vertical separator to change the
  left/right ratio and the left column's horizontal separator — it shows a visible grip — to change the
  upper/lower ratio (both 10 %–90 %, kept in `layout.json`).
* **Sidebar**: while the fleet daemon runs, a 34-column sidebar on the left lists this fleet's cards (state dot,
  bold title, two grey lines). Cards that do not fit are scrolled to, like cmux: the wheel over the cards moves the
  list one card per step, a grey `┃` thumb on the sidebar's inner edge shows the visible share and where it is, and
  the selected card (a click, `←` / `→`, a new bash page) is always scrolled into view. Drag its right edge to resize it: 24 up to `min(60, columns − 60)` columns (a window
  too narrow for 24 cannot drag), the drag paints that border column Yellow bold, and the release saves the width to kv
  `sidebar_width`; hiding and showing the sidebar again keeps it, while a restarted server returns to 34, and the
  gui cannot drag the edge (one xterm.js canvas, it does not know the border column). Row 0 is a two-segment
  topbar across the screen, split at that drag line: the left segment holds `◫  +  ↺  ←  →` and follows the
  sidebar's width, the right segment starts two columns after the `│` divider and shows the current fleet's title
  plus the daemon's one-line state (`fleetmux-demo · 运行中 · …`, "running"), cut with `…` at the right edge. `◫` collapses
  the sidebar to 0 columns — the panes fill the screen from column 0, nothing is drawn below row 0, and the
  buttons keep exactly their columns — `◫` or `C-b s` shows it again at the width it had; `+` opens a bash page
  (see the next bullet), `↺` (or `C-b r`) restores the newest
  closed fleet from kv `restorable`: the server spawns the recorded `start --resume` command in a `resume` pane,
  zooms it and the status line says `正在恢复…` ("restoring…"); with nothing to restore it says `没有可恢复的 fleet` ("no fleet to restore"), and while the
  sidebar still lists a `running` / `paused` / `stopping` card it says `先关闭正在运行的 fleet 再恢复` ("close the running fleet before
  restoring") and spawns
  nothing (one session holds one fleet). A closed
  fleet's card keeps a grey dot and `已关闭 · 可恢复` ("closed · restorable"; a run a lifecycle timeout stopped with `resumable` says
  `到点停 · 可恢复`, "stopped at the time limit · restorable"), draws `↺` on its first row (it restores that card's run by
  id) instead of `×`, and the pane area shows the closed-fleet placeholder; the server exits 10 s after the last
  pane and viewer are gone, and the run can still be resumed with `start --resume` afterwards. While no fleet runs,
  `fleetmux gui` (or `./start.sh gui`: the Python `gui` subcommand, `fleet.py gui`) is the one-command window on that history: it scans this
  HOME's `runs/` once, writes the grey cards newest-closed-first (at most 16, only runs whose runtime is this
  HOME's) and execs `fleetmux gui --runtime <that runtime>`; with no resumable run it prints
  `这个 HOME 没有可恢复的 fleet；用 start 新起，或 start --resume <run>` ("this HOME has no fleet to restore; start a
  new one with start, or start --resume <run>") and exits 1 without starting a service. A
  window that lists only grey cards — no `running` / `paused` / `stopping` card — shows
  `没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复` ("no fleet running; click ↺ on a sidebar card to restore") in the
  pane area, and such a server exits 10 s after the last pane and
  viewer too; a server without kv `sidebar` never exits by itself. `←` / `→` (or
  `C-b (` / `C-b )`) step the
  card selection through the fleet and bash cards as one list (see the next bullet). Sidebar S3 (M4, one
  `fleetmux server` per fleet tree, one page per fleet): a card that carries `page` switches every viewer to that
  fleet's page — click it, or step onto it with `←` / `→` / `C-b (` / `C-b )` — and the highlighted card and the
  topbar follow the page shown; a reclaimed child fleet's page is a read-only record (`已收回 · 只读`, "reclaimed · read-only", over its
  empty groups, its `history_view` tab below), and a child card has no `×` (child fleets start and stop through
  their roster and `subfleet.up` / `down`). A card without `page` (an older daemon) still only says
  `切换 fleet：S3 开放` ("switch fleet: available in S3"). In `fleetmux gui` and the M2.5 window a sub-fleet link of `history.html` switches the page
  the same way (the server does it, so it works with the child daemon gone). With a sidebar the cells start on row 1
  (their tab row) and the cards on row 2; the topbar has no background of its own. A card's `×` (running / paused
  fleets only) or `C-b X` opens the close confirmation box: `y` confirms, any other key or a click elsewhere
  cancels. Confirming writes kv `stop_request` = `requested` and the status line shows `已请求关闭，等 daemon…` ("close requested, waiting for the
  daemon…") —
  the daemon only starts acting on it in the next milestone, so a request made today stops nothing. The panes'
  PTYs, the tabs and the gui's history overlay all move right while the sidebar is shown; without the kv key
  `sidebar` there is no sidebar and nothing changes. Shown/hidden is shared by every viewer and not saved in
  `layout.json`. With a gui window attached (macOS) the topbar's left segment (the toolbar) shifts right by the
  columns its traffic lights take: the window has a transparent, title-less titlebar, xterm.js row 0 sits on the
  traffic-light line, and every viewer — terminals too — starts its toolbar after them; Linux keeps the native
  titlebar and no shift. In the gui, blank and `─` cells of row 0 still drag the window (double click zooms it);
  the `│` divider and the title text are neither, so the page leaves them to the terminal instead of dragging the
  window.
* **Bash pages (S1c)**: three entry points open one: the sidebar's `+` (row 0, column 4), a double click on its
  empty card area (two left presses on the same cell within 300 ms, from row 2 down to the row above the status
  line, in the columns left of the card gap) and `C-b c`. A bash is `$SHELL -l` (or `/bin/bash -l`) started in
  `$HOME`, a pane of type `shell` that the **server** spawns as the last tab of the root page's `workers` group, so
  every new bash is mapped into the main fleet's worker area and managed by the main fleet. The root fleet takes it into the fleet's registry
  (`<session>.fleetmux.json`, `type: shell`; the daemon's tick and `arrange`): the daemon keeps it in the worker area
  behind the workers and lists it on the history page, and a bash opened while no fleet runs is taken in by the next
  root fleet. It is no worker: no slot, no work, not counted by `--max-busy`, not in the roster; a sub-fleet never
  takes one in. Each bash gets a `bash-<n>` card after the fleet cards, in creation order (green dot,
  `终端 · 运行中` ("terminal · running"), `~`, `×` on its first row); a shell that exits takes its card and its tab with it. Opening a bash
  or clicking its card shows the root page, cancels a zoom of another pane, selects the tab and focuses its group —
  the topbar reads `bash-<n> · 终端 · 运行中` and the fleet card's blue bar gives way — and clicking the fleet's card
  focuses the supervisor group again (the bash stays a tab of the worker area). A bash card's `×` closes the
  bash right away while the shell sits at its prompt; while a program runs in the foreground (the PTY's
  foreground process group is not the shell's own) the same confirmation box asks first —
  `bash-<n> 里还有程序在跑，关掉会结束它。` ("a program is still running in bash-<n>; closing will end it.") with
  `[ 关闭 bash 页 ]` ("close bash page") — and only `y` closes it; closing a bash
  also ends the job in its foreground, even one that ignores SIGHUP (`pane.kill`, PROTOCOL.md §4.13). Closing the
  root fleet (Python's `close_panes` and `close_all`: the sidebar `×` / `↺` path and the lifecycle close) also
  closes every bash whose shell is idle at its prompt (the pane record's `fg_pgid` equals its `pgid`) and keeps
  the ones with a program in the foreground — or without `fg_pgid` (an older server) — naming them in the close
  outcome; `close_all` ends the server once no bash is left and keeps it while a busy one lives — the closer
  then takes the run's cards out of the live states itself (a resumable timeout run reads `到点停 · 可恢复` with `↺`,
  any other run `stopped` / `已停止` ("stopped"), its sub-fleet cards `已收回 · 随根关闭`, "reclaimed · closed with
  the root"), so the server exits by itself 10 s
  after the last pane and viewer, as before. A sub-fleet's close never touches a bash.
  `session_exists` skips `type: shell` panes, so a server holding only bashes is not a live fleet. The closed-fleet /
  no-fleet placeholder is drawn on the fleet's page only while no live card is up, no pane other than `shell` exists
  there, nothing is zoomed and no bash card is selected. Switching to another fleet is S3
  (M4): a card with `page` switches the page; a card without `page` only says `切换 fleet：S3 开放`.
* **Keys** (prefix `C-b`, server option `--prefix`): `1`–`9` / `Tab` / `BTab` pick a tab; the arrow keys move
  focus between groups; `z` zooms; `[` enters page mode (`PgUp` / `PgDn`, `/` search, `q` quit); `<` `>` `+`
  `-` change the ratios and `=` resets them; `o` opens `history.html` on the machine you are attaching from (or
  shows its path); `d` detaches. Mouse: click tabs and panes, drag the separators, wheel to scroll back.
  Shift+drag leaves selection to your outer terminal. In a pane whose program does not take the mouse, a
  drag selects (highlighted), double click selects a word, triple click the whole line; each copies at once via
  OSC 52 plus `pbcopy` / `wl-copy` / `xclip` on the server host, and the highlight stays until your next click or
  key. Word boundaries are cmux's (Ghostty's default): whitespace and `'` `"` `│` `` ` `` `|` `:` `;` `,` `(` `)` `[` `]` `{` `}` `<` `>` `$`.
* **Copy (OSC 52) by outer terminal** — what was actually checked during the M1 acceptance:

  | Outer terminal | OSC 52 copy |
  |---|---|
  | cmux (macOS) | works |
  | WezTerm (macOS and Linux) | works |
  | Terminal.app (macOS) | not accepted |
  | gnome-terminal (VTE, Linux) | not accepted |
  | iTerm2 | not tested |

  Where OSC 52 is not accepted, the server-host clipboard tools (`pbcopy` / `wl-copy` / `xclip`) are the only
  copy path; on a headless host without them nothing reaches a clipboard.
* **Crash recovery order**: if a `fleetmux server` dies (for example `kill -9`), run **`fleetmux reap --runtime
  <dir>` first** and only then `fleetmux start --resume`. The first spawn of a new server rewrites
  `panes.json`, after which `reap` can no longer find the old process groups. Try this only on a run you
  started for testing — never `kill` / `reap` the server of a run you are using or someone else's fleet.
* **Limits (today)**:
  * Sub-fleets are refused, as on WezTerm (`subfleet.backend_problem`).
  * There is no embedded browser; the history is a terminal tab.
  * The M0–M2 Mac and Linux-VM acceptance was passed by the operator on the fixed commit `64f7be96`; the
    reworked tab style above (fix-m1-tabs) passed the operator's real-mouse check (7 items) in cmux and WezTerm
    on the release built from `b1de5637`.
  * In that acceptance the performance targets (server CPU while 8 panes flood, idle CPU with no wakeups, memory
    for 10,000 lines of scrollback) were met on both machines, and the Linux-side checks (`htop` end-to-end,
    the logind `KillUserProcesses` warning) ran green on the VM.
  * Replay snapshots: 9 real Claude Code / Codex / pi sessions were redacted under the operator's conditions and
    imported (`rust/fixtures/recordings/`); the targeted `replay` tests pass. The full `m3` gate and the
    final review are **not done yet**.
  * Design: [docs/design.md §33.6](docs/design.md#336-fleetmux-backend-linux-default-since-m3).

`start` options: `--profile` (Codex profile, default `fleet`), `--backend` (`cmux` / `wezterm` /
`fleetmux`, see above), `--attach` (fleetmux only), `--codex` / `--redis-server` (binaries,
default: first on PATH), `--redis-runtime-dir` (default: checkout `.runtime/redis`),
`--runs-dir` (default `~/.fleetmux/runs`), `--codex-home` (the agents'
isolated Codex home, default `~/.fleetmux/codex-home`), `--handoff-ratio`,
`--handoff-timeout`, `--stall-timeout`, `--window` (fallback context size until the rollout reports one),
`--compact-limit` (Codex auto-compaction token limit; by default Codex decides), `--chrome-script`
(see below).

Each run writes to `~/.fleetmux/runs/fleet-<time>-<token>/`: `config.json`, `workspace/<worker>/`
(worker cwd, all deliverables land here), `agents/supervisor/`, `handoffs/`, and `prompts/`.
`~/.fleetmux/latest.json` points at the newest run and pins `redis_runtime_root`,
`redis_runtime_dir`, `redis_instance_id` and `redis_socket` for callbacks and control.

All per-user data lives under `~/.fleetmux/`: the fleet home above, `state/` (window registries), `config/`
(channel credentials, whitelists, leases), `launchers.env`, `board/` and `fleets/<name>/` (a second fleet's
home). A machine set up under the project's earlier name runs `fleetmux home migrate [--dry-run]` once, while no
fleet is running: it moves the data here, leaves a symlink at each old path and updates the run records, so old
runs can be resumed ([design §50.1](docs/design.md)); until then `fleetmux start` stops with one line saying so.

Private Redis files live separately in `.runtime/redis/<token>/`: `redis.sock`, `redis.pid`,
`redis.log`, `fleet.rdb`, and v2's `appendonlydir/`. The short token avoids Unix socket path-length
limits; directories are owner-only (700). Overlong paths fail before Redis starts, with no `/tmp`
or TCP fallback. Use an explicit short, writable `--redis-runtime-dir` for a read-only/non-editable
installation. Older configs pinned to an unmanaged `/tmp/fleetmux-*.sock` remain supported; existing
fleets are not restarted. Stop removes the socket through Redis shutdown, but retains logs/data.
Keep `.runtime/` out of Git (a local `.git/info/exclude` entry is sufficient); these are runtime
files, not source. See [the design baseline](docs/design.md#13-redis-run-directory-inside-the-project).

## Layout and the history page

Everything lives on one cmux page: the supervisor top-left, the **history** browser pane under it, the
workers as tabs of one pane on the right (worker-1, worker-2, …). After startup the `daemon` tab's
surface is docked as an inactive tab beside history in the lower-left pane; history stays selected.
Its now-empty startup workspace closes
without killing the daemon. Click `daemon` only when you need logs; it is not another AI agent. When a session rotates, its successor opens in the same spot and the
retired session's whole screen (with scrollback) and handoff note go to `<run>/history.html`, which the
history pane reloads. On that page you pick an agent (`supervisor`, `worker-1`, …), pick a retired
generation (`supervisor-1`, `supervisor-1.1`, …, `worker-1`, `worker-1.1`, …), switch between its screen
and its handoff note, or search every generation of every agent at once (Enter jumps to the next match).
Successors are named `<slot>.<generation>` for the supervisor and the workers alike: `supervisor-1` → `supervisor-1.1` → `supervisor-1.2`, `worker-1` → `worker-1.1` → `worker-1.2`.

## Service mode (resident fleet)

`start --mode service` keeps the fleet running with no goal and no end: it answers a stream of
requests. The supervisor only routes; each worker can have its own prompt; answers are JSON checked by
the daemon, not by a model.

```bash
fleetmux start --lifecycle service --worker-names exit-1,reentry-1,rung-1 --roles ~/my-roles \
    --supervisor-cli pi --worker-cli pi --pi-model local/my-model \
    --allow-unsandboxed-pi --allow-development-v2 --max-busy 2
echo '{"id": "cu-1:r7", "to": "exit-1", "payload": {...},
       "reply": {"required": ["verdict", "param", "evidence"], "allowed": ["verdict", "param", "evidence", "note"]}}' \
  | fleetmux request -
fleetmux results --drain        # one JSON line per finished request
fleetmux pause --reason "market closed"; fleetmux resume
```

- `--worker-names`: worker slots named by role (lowercase, no dots; dots mark generations). Each name ends
  with `-<number>` (`proto-1`, not `proto`) so its generations read `proto-1` → `proto-1.1` → `proto-1.2`,
  like `worker-1` → `worker-1.1`; a new run refuses other names. A run started before this rule keeps its
  names on `--resume`.
- `--roles DIR`: `DIR/<agent>.md` is appended to that agent's `AGENTS.md` as "Your role". One prompt per worker.
- `--supervisor-profile` / `--worker-profile`: separate Codex profiles, e.g. a local model for one side.
- `--max-busy N`: at most N workers work at once (a local model server may reject concurrent large
  requests). The supervisor is not counted.
- A request: `id` (single use, `[A-Za-z0-9][A-Za-z0-9:._-]*`), `payload` (any JSON, ≤ 200 000 chars),
  `reply.required` (keys the answer must have), optional `reply.allowed` (no other keys accepted), optional
  `to` (a worker: Python routes it, no supervisor turn), optional `instruction`, optional `timeout_s`.
  Without `to` the supervisor picks a worker or skips the request.
- Deadlines: a request without `timeout_s` never expires by wall clock (health is the watchdog's: stalls, lost
  links, API errors); `--request-timeout N` sets a default deadline for the run (none by default). A request whose
  deadline passes is published as `timeout`; only the worker executing it is held (its late answer is audited as
  `result.late`, never applied), the supervisor and the rest of the fleet keep working (docs/design.md §43).
- The answer must be one JSON object. A bad answer gets one correction; a second bad answer is published
  as `invalid`. Result statuses: `ok`, `invalid`, `skipped`, `timeout`, `stalled`.
- `pause` stops delivery and rotation; requests keep queuing, and those with a deadline still time out (send
  `timeout_s` if you must never wait longer). A supervisor `BLOCKED`, a closed tab or a failed delivery pauses the fleet instead of
  ending it. Calendars (market hours, holidays) are the caller's business: pause and resume from outside.
- Callers on the same machine can skip the CLI: push `{"kind": "request", "request_id": ..., "spec": ...}`
  to `fx:events` on the private socket named in `~/.fleetmux/latest.json`, then read `fx:result:<id>`.

## Worker roster YAML (hot add / remove)

`start --roster roster.yaml` (instead of `--workers` / `--worker-names`) takes the workers from a file,
and the daemon re-reads it while the fleet runs:

```yaml
workers:
  - name: exit-1            # same rules as --worker-names (ends with -<number>)
    role: roles/exit.md     # optional, relative to the yaml; overrides --roles DIR/exit-1.md
  - name: reentry-1
```

`start --worker-kit KIT --kit-fleet NAME` takes the roster template from `KIT/fleets/NAME/` instead: the
whole directory is copied to `<run>/fleet-config/` and the run follows that copy exactly like
`--roster <run>/fleet-config/roster.yaml`; `--resume` keeps the copy (docs/design.md §30).

- **Add**: a new name is written to `config.json` (and `latest.json` if it points at this run), then
  spawned with the READY prompt as a tab at the end of the worker column. The supervisor sees the new
  list appended to its next message (`[Roster change] Workers you can dispatch to now: ...`).
- **Remove**: the worker takes no new work (dispatch to it is rejected), finishes what it already has,
  and when idle with an empty queue its screen goes to the history page and its tab is closed. Its
  workspace directory stays. Putting it back in the file before that cancels the removal.
- **Role change** for an existing worker applies at its next generation; it is not restarted.
- **The supervisor edits it too**: every supervisor prompt has an "Adding and retiring workers (the roster)"
  section naming this run's roster file (`{{ROSTER}}`; runs started with `--workers` / `--worker-names` say
  it is not available). It adds a row `- {name: <role>-<n>, role: <path>}` when more work is ready than free
  workers, deletes one when a worker has no more work, within the limits of its task brief, and dispatches to
  a new worker only once it is in a `[Roster change]` list. A rejected edit reaches it as
  `[Roster rejected] <errors>; the roster in effect is unchanged: <workers>` (once per rejected file). A Codex
  supervisor's sandbox gets the roster's directory as a writable root.
- **Rejected as a whole** (roster kept, reasons in the daemon tab as `roster.rejected`): invalid YAML,
  unknown keys, bad or duplicate names, zero workers, a missing role file, or a name already used in
  this run (a removed worker's name cannot come back). Changes are logged as `roster.changed`.
- **Handoff threshold per model** (docs/design.md §22): optional `handoff:` table and per-worker
  `window` / `handoff_ratio`; edits apply at the next delivery check, no restart.
- **Advisor switch**: optional top-level `advisor: on|off`, default off. While on, the daemon consults the
  advisor (official Claude CLI, fable/high) before every supervisor dispatch; it picks the worker model /
  effort / domains and authorizes the respawns its sheets need. Off: dispatches go out with the slot
  defaults, exactly as without the advisor. Flipped while running without a restart, by editing the roster,
  by `fleetmux advisor on|off [--config <run>/config.json]` (only edits the roster; a run
  started without `--roster` gets `<run>/roster.yaml` from its current workers), or by the supervisor's
  decision key `"advisor": "on"|"off"`. `start --advisor` only writes `advisor: on` into the roster (a
  `--roster` file is edited in place). On at start: Claude workers open their pane but launch the CLI only
  when the first dispatch's sheet arrives. Turned off: workers still waiting for a sheet start with the
  defaults; running workers keep their generation. Logged as `advisor.on` / `advisor.off` (`who`), and the
  daemon tab's `STATE` line shows `adv=on|off`.
- **Companion tabs** (docs/design.md §31): optional top-level `tabs: [{name, command, cwd?, env?}]` (name `^[a-z][a-z0-9-]*$`, unique; cwd relative to the roster, `~` ok; env strings) - terminal programs (not agents) opened as tabs after the control tab of the loading fleet's workspace, `--focus false`. On every fleet start (`start`, `--resume`, a sub-fleet's `daemon --resume`) each running process whose command line exactly equals the tab's program is SIGTERMed (SIGKILL after ~5 s) before the tab opens; matching rule in §31. Closed by `stop` / `close_all`; a tab closed while the daemon runs is not reopened until the next start. Programs run as the fleet's user: the roster is the authorization.
- Needs PyYAML in the Python that runs the daemon.

## Handoff window and ratio per model

The session is rotated when `used >= ratio × window`. Each agent resolves `window` and `ratio`
separately, first hit wins, using its **current** runtime CLI and model (after a profile switch too):

1. the roster worker entry: `{name: exit-1, window: 1000000, handoff_ratio: 0.7}`;
2. the roster `handoff:` table, key `<cli>:<model>` (exact model string) then `<cli>`;
3. the same `handoff:` table in `config.json` (static);
4. built-in: Claude models whose name contains `[1m]`, `opus`, `fable` or `sonnet` → 1,000,000; other Claude
   models → `--claude-window` (default 200,000); Codex / Pi → the window they report themselves;
5. `--handoff-ratio` (default 0.6).

```yaml
handoff:
  claude:opus: {window: 1000000}
  claude: {window: 200000, ratio: 0.6}
  codex: {ratio: 0.6}
```

A bad value (ratio not in (0, 1), window not a positive integer, unknown key or CLI) rejects the
whole roster change. `status` shows `context.handoff_window` / `handoff_ratio` / `handoff_source`
per agent; the daemon tab prints `handoff=<ratio>@<window>`.

## Resume an interrupted run (`start --resume`)

`fleetmux start --resume <run-dir>` reattaches an unfinished Redis v2 run instead of creating one;
`--resume auto` takes the run in `~/.fleetmux/latest.json` and starts a new run when that one is
finished (`<run>/result.json` exists) or missing. A live daemon makes it a no-op. Keeping the daemon
alive is the supervisor's job inside the fleet (design §36), not an external timer's.

- Redis restarts from the run's own directory and socket; AOF brings back everything up to the last
  fsync (about 1 s). A Redis that is still up is reused.
- Official Claude workers whose tab still runs the CLI are reattached; every other agent (the task
  supervisor, Pi agents, closed tabs) starts as its next generation (`worker-1` -> `worker-1.1`) in place.
- Task: verified artifacts and accepted reports are kept; dispatches that were queued or in flight are
  re-queued by the daemon, and the new supervisor gets a summary (goal, artifacts verified/missing,
  completed dispatches with report excerpts, re-queued dispatches). Service: accepted requests without a
  result are routed again; a request with `"replay": false` is published as `timeout`.
- `--max-attempts N` (default 2, stored in config, can be changed on resume): one dispatch / request runs
  at most N times in total; over the cap the task is `blocked` / the request is `timeout`, naming the item.
- A finished task (any terminal status) or an explicit `stop` writes `<run>/result.json`; such a run is
  never resumed. Without `--resume`, a restarted daemon still holds uncertain work and pauses (no replay).
- The fleet looks after itself (docs/design.md §36): a Pi supervisor's fleet extension checks the daemon
  heartbeat every 2 minutes (`fleet.yaml#daemon_watch`); when it is stale and no daemon holds the run, the
  supervisor gets one `[Daemon watchdog]` turn with the facts from the logs and runs the exact `start --resume`
  command its AGENTS.md names for this run. The daemon in turn restarts a frozen supervisor or one whose CLI
  exited, and briefs the next generation from the logs (`supervisor.revived`). No cron or launchd job is needed.

## Interface contracts (task spec `"contracts": true`)

Opt-in per task; without the key nothing changes. Rules: `docs/design.md` §18.

1. **P0 – interfaces.** The supervisor's first reply is `phase: "interfaces"` with a plan (M0…Mn) and
   every interface (path in the producer's worker directory, `contains` / `json_keys`, `writers`,
   `decisions`, a self-passing `sample`), and no dispatch. The daemon rejects each gap by name.
2. **P1 – skeleton run.** The daemon sends `[skeleton]` to `skeleton_worker` (task spec, default the first
   worker); `<worker>/skeleton.md` needs one PASS line per interface and no FAIL. Pass → interfaces frozen
   at v1; fail → back to P0; a second fail blocks the task.
3. **P2 – parallel.** Dispatch items carry `id`, `provides`, `consumes` (`id@version`), `edits`,
   `depends_on` + `serial_reason`; the daemon holds dependent items until their prerequisites are done.
   A worker's `Interfaces: IF-x@n provided: ready` line is checked literally before the interface is
   `ready` and consumers get `[contract] ready`. A trailing `{"interface_change": ...}` object is a
   proposal the supervisor approves or rejects; breaking changes deprecate the old version and mark
   its consumers stale.
4. **COMPLETED** also needs every non-deprecated interface `ready`. Contract tasks may add
   `artifacts[].not_contains` and `artifacts[].cmd_exit` (argv, no shell, run in the shared output
   directory, `timeout_s` default 120 / max 600; output written to `path`).

The program writes `<run>/contracts.json` (copied to `<workspace>/contracts.json`) and
`<run>/contracts.log.jsonl`; agents only read them. Each agent directory gets `CONTRACTS.md`.

**Peer notices** (contract tasks only; `docs/design.md` §21): a producer tells the consumers of an
interface it changes (`UNAPPROVED`) and finishes (`READY`) directly. Two Claude Code workers use
Claude Code's own `SendMessage` (session names `f<root token>-…-worker-<n>`, listed per consumer in
`contracts.json` `consumers_runtime`); any pair with a Pi / Codex end uses the broker
`fleet.py peer --from <me> --to <worker|consumers:IF-x> --iface IF-x --text-file <own file>`, which
validates the fixed first line, 600 chars, READY-by-producer and 20/hour, puts `[peer] …` in the
target's inbox and copies the supervisor (`[peer CC]`, appended to its next business message). A Claude
turn woken by a peer message answers no delivery: logged as `unsolicited`, never a report.

## Sub-fleets (a worker becomes a sub-fleet supervisor; development)

A busy worker can be turned into the supervisor of its own fleet for one numbered piece of work
(`M0`, `M1`, ...; a sub-fleet of `M0` numbers `M0.0`, `M0.1`, ...). Details: `docs/design.md` §19.

- Each fleet has its own cmux registry (`fleetmux`, `fleetmux:M0`, ...), daemon, private Redis and run
  directory (`<main run>/sub/M0`). Only the main fleet is written to `latest.json`.
- The sub-fleet is a separate workspace right after its parent in the sidebar, titled
  `<main fleet name> › worker-1 › 子 fleet M0` ("sub-fleet M0"; `start --name` sets the name; default: the task goal or
  the run token). Its supervisor is the worker's live tab (same Claude session, moved top-left); the
  parent keeps a placeholder tab in its place. When the sub-fleet finishes, the tab moves back and the
  worker continues as `worker-1` of the main fleet.
- Limits (program-enforced, reasons returned): `max_depth` 2, `max_children` 3, `max_agents_total` 12,
  `child_workers_max` 3; override in config `limits` or in the roster YAML `limits:` (read on each check).
- Only official Claude Code worker tabs can be handed over (a Pi bridge cannot reconnect to another Redis).
- `status` shows the whole tree; `stop` on the main fleet stops sub-fleets first; `start --resume` on the
  main fleet restarts sub-fleets that were running.
- Test / operator entry (the owning daemon executes it):
  `fleetmux subfleet spawn --slot worker-1 --task-id M0 --goal "..." --workers 2 [--output /abs/path]`.
- Normal path (§20): a worker whose task is too big ends its report with
  `{"escalate": "<why>", "proposal": [{"id": "M0.0", "instruction": "...", "output": "m0.0.md"}], "workers": 2}`;
  the supervisor answers `"escalations": [{"task": "M0", "decision": "approve|revise|reject", "workers": 2, "note": "..."}]`;
  on approve the program starts the sub-fleet. The task needs an id: contract tasks always have one, plain tasks
  need `"id": "M0"` on the dispatch item. In contract tasks the sub-fleet runs its own P0 / P1; its interfaces take
  effect at once and are reported to the top supervisor, which may `veto` them.

## Claude Code workers (optional)

On Redis v2 the default supervisor is Pi on `openai-codex/gpt-6-astra` with official Claude Code workers (see "Redis v2
development runtime"); the official Codex supervisor stays available with `--supervisor-cli codex`. This section describes the
legacy v1 Claude worker path. Workers can be official [Claude Code](https://claude.com/claude-code) CLI sessions instead:

```bash
fleetmux start --workers 2 --worker-cli claude --worker-model sonnet --worker-kit ~/my-kit \
  --lifecycle <mode> --allow-unsandboxed-pi --allow-development-v2
```

With no `--supervisor-cli` this now gets the default Pi supervisor (`openai-codex/gpt-6-astra`), hence
`--allow-unsandboxed-pi`.

- Each worker runs `claude --dangerously-skip-permissions --model <model> --settings <hook> --add-dir <workspace> [--mcp-config mcp.json] -- <prompt>`.
- Turn ends come from a Claude Code **Stop hook** (`fleet.py claude-stop`) instead of Codex `notify`. A Stop that still lists
  `background_tasks` (subagents running in the background) is ignored; the session stops again when they finish.
- Context usage and the stall watchdog read the session transcript (plus its `subagents/` transcripts).
- Multi-line messages are written to `<worker>/.inbox/msg-*.md` and the tab gets a one-line pointer: Claude Code would
  submit a pasted newline as Enter.
- The "trust this folder?" prompt is pre-answered in `~/.claude.json`; the scope file is also written as `CLAUDE.md`.
- `--worker-kit DIR` is copied into every worker cwd: `DIR/.claude/{agents,skills}` and `DIR/mcp.json`.

## Pi agents (v1 compatibility by default, Pi >= 1.1.0)

Pi can run the supervisor, workers, or both. Claude workers still run the **official `claude` CLI
as a separate process started by fleet**. Pi does not proxy Claude requests or consume Claude Code's
subscription credentials. This separation is not a guarantee against account limits or enforcement;
follow the providers' terms.

Example: a local-model Pi supervisor with official Claude Code workers:

```bash
fleetmux start --lifecycle service --workers 2 \
  --supervisor-cli pi --pi-model local/my-model --pi-thinking off \
  --worker-cli claude --worker-model sonnet \
  --allow-unsandboxed-pi --allow-development-v2
```

The explicit `--supervisor-cli pi` takes its model from `--pi-model` (the table default is not read).

Configure `local/my-model` in Pi's `models.json` first. `--pi-model` is required (no implicit
fallback to a personal/cloud default) for every Pi agent that has no model of its own. To use the same Pi model for workers instead, select
`--worker-cli pi`. An all-Pi fleet needs neither a Codex binary nor a Codex profile.

- Per-agent Pi models. Model, highest first:
  - supervisor: `--supervisor-pi-model` (or the model an old profile maps to, `--supervisor-profile fleet-astra` →
    `openai-codex/gpt-6-astra`, only when `--supervisor-cli` is not given); then an explicit `--pi-model`; then, **only
    when `--supervisor-cli` is not given** (pi taken from the `fleet.yaml#cli_pairs` default), `default.supervisor_pi_model`.
    A flag you typed is never overridden by the table default. An explicit `--supervisor-cli pi` does **not** read the
    table default: give `--supervisor-pi-model` or `--pi-model`. So `start --pi-model X` with no other CLI/model flag
    runs the default Pi supervisor on X; to keep Astra while workers use X, also pass `--supervisor-pi-model`.
  - worker: a live profile card's `model`; then its roster row `pi_model:` (Pi workers only, not together with
    `profile:`); then `--pi-model`.
  - Thinking is resolved separately in the same way: supervisor = `--supervisor-pi-thinking` (or the mapped profile's)
    > explicit `--pi-thinking` > `default.supervisor_pi_thinking` (defaulted supervisor only) > `off`; worker = card >
    roster `pi_thinking:` > `--pi-thinking` > `off`. E.g. `--pi-model X` alone gives the default supervisor X with
    thinking `high`; adding `--pi-thinking low` gives X / low.
  - A Pi agent left without a model refuses the start: `--pi-model provider/model is required for pi agents (or
    --supervisor-pi-model / roster pi_model); no model for: ...`.
  - `--supervisor-pi-model` / `--supervisor-pi-thinking` with a non-Pi supervisor (`--supervisor-cli codex|claude`) are
    refused, not ignored. With an explicit `--supervisor-cli pi`, `--supervisor-profile X` is refused (use
    `--supervisor-pi-model`); old-profile mapping applies only to a defaulted supervisor.
  - Changing a row's `pi_model` / `pi_thinking` in the roster file moves that worker to its next generation on the new
    model once idle (same slot name).

- `--pi PATH`: Pi CLI binary, >= 1.1.0 (the `agent_settled` lifecycle is required).
- `--pi-agent-dir DIR`: source Pi directory, default `$PI_CODING_AGENT_DIR` or `~/.pi/agent`.
  Only its `auth.json` and `models.json` are linked into a fresh `<run>/pi-home`. Credentials are
  not printed. No Claude configuration/credentials are copied. Model-file credential commands
  are executable configuration: use a trusted source.
- Discovered extensions, skills, MCP, prompt templates, themes, project settings and context
  files are disabled. Only the bundled bridge extension loads, and the generated `AGENTS.md`
  is explicitly appended to the system prompt. Pi's read/bash/edit/write tools remain enabled.
- **Pi has no OS sandbox.** `--allow-unsandboxed-pi` is an explicit acknowledgement, not a sandbox
  switch. Role instructions are not a security boundary. Use a dedicated account/VM for untrusted tasks.
- Pi session names and terminal titles use the fleet's existing label (`supervisor-1.1`, `worker-1.1`,
  etc.), not the cwd or a separate naming scheme. The original slot/generation/history rules stay unchanged.
- In the supervisor TUI, ordinary typed text enters the daemon's existing request/task queue instead
  of bypassing it. In service mode Python assigns a real request ID, the worker answers in a `reply`
  JSON field, and the result is queued back to the supervisor for reporting. In task mode text starts
  a task or supplies a follow-up to the current task. Slash commands and daemon inbox pointers are
  not re-enqueued; images are rejected with a message (supply a file path instead).
- Pi reports completion only on `agent_settled`, after tools, retries and automatic continuations.
  Generation plus inbox delivery ID reject stale/duplicate completions, including after Escape.
  The ID comes only from the final daemon pointer in this agent's own `.inbox`, never an inbox path
  quoted in the instruction text (e.g. a supervisor's path forwarded to a worker).
  An unsuccessful startup never counts as READY; error/aborted partial JSON is never accepted as a result.
- Context usage comes from Pi's `getContextUsage()`; each generation writes its session under
  `<run>/pi-sessions/<slot>/g<N>/`. Fleet still handles idle-boundary rotation and history; Pi's
  automatic compaction remains a safety net within a long turn. `--compact-limit` is Codex-only.
- The Pi bridge callback runs through the fleet Python interpreter and **only its private Redis
  Unix socket**, not `localhost:6379`. A callback failure is shown by Pi; the fleet watchdog
  eventually handles a missing completion.

Optional live adapter smoke (uses a temporary private Redis, fake cmux, real Pi/model; **no Claude calls**):

```bash
python tests/pi_smoke.py --model local/my-model
```

This tests READY, JSON request/result and handoff to a new generation. It is not a real cmux TUI test.

## Redis v2 development runtime

V2 now connects the existing Fleet dispatcher to persistent Pi bridges. Select it explicitly
on a fresh isolated development run with `--transport-version 2 --allow-development-v2`
and `--supervisor-cli pi --worker-cli pi`, plus the existing explicit model/permission options.

**Default pair: Pi supervisor + official Claude Code workers.** Codex models and FlashNext
run through Pi (ChatGPT login via Pi's `openai-codex` provider); Claude agents only ever run the official `claude` CLI, and
Pi never uses Claude subscription credentials. With no `--supervisor-cli` the supervisor is Pi on
`openai-codex/gpt-6-astra`, thinking high (`fleet.yaml#cli_pairs` `default.supervisor_pi_model` / `supervisor_pi_thinking`;
`--supervisor-pi-model` / `--supervisor-pi-thinking`, or an explicit `--pi-model` / `--pi-thinking`, override them;
an explicit `--supervisor-cli pi` skips the table default and needs `--supervisor-pi-model` or `--pi-model`; full order
under "Pi agents"). No `--pi-model` is needed while
the workers are Claude. `--allow-unsandboxed-pi` is still required:

```bash
fleetmux start --worker-model sonnet --workers 2 --lifecycle <mode> \
  --allow-unsandboxed-pi --allow-development-v2
```

The `pi+claude` task/service rows are `verified` (real `scripts/smoke_pair.sh pi claude task|service`, 4/4 checks,
runs `fleet-20261013-115145-828661b9` / `fleet-20261013-115347-7e61f9af`, 2026-10-13, uncommitted branch tree). Night pair: Pi Astra supervisor + Pi FlashNext workers
(the verified `pi+pi` rows, per-agent models; `--supervisor-pi-model` is required here, otherwise `--pi-model` would put the
supervisor on FlashNext too):

```bash
fleetmux start --supervisor-cli pi --worker-cli pi \
  --supervisor-pi-model openai-codex/gpt-6-astra --supervisor-pi-thinking high \
  --pi-model flashnext/<local-model> --workers 2 --lifecycle <mode> \
  --allow-unsandboxed-pi --allow-development-v2
```

`--supervisor-profile fleet-astra` (or `--profile fleet-astra` while the workers are not Codex) without
`--supervisor-cli` is the old Codex-profile spelling: it now starts the Pi supervisor on `openai-codex/gpt-6-astra`
(thinking high) and prints a notice to stderr; combined with a different `--supervisor-pi-model` it is refused. Any other
profile name there is refused with the two new usages (`--supervisor-cli codex --supervisor-profile X`, or
`--supervisor-pi-model provider/model`). It never silently starts the official Codex TUI.

Optional: official Codex supervisor + official Claude Code workers (task and service; the previous default)
also run on v2.
The daemon receives each delivery from Redis and types only its business text into the official
TUI (Claude panes get a bracketed paste); no `.inbox` file and no transport ID reach the model.
The Codex `notify` / Claude `Stop` hooks bind the session on READY and then settle only the delivery
this generation received, only when the typed text started that turn; the daemon validates it as for Pi.
Agents keep the original AGENTS.md reply formats. Six parallel Claude workers with an official Codex Astra supervisor:

```bash
# once: a Codex profile file for the supervisor model (Codex >= 0.156 reads ~/.codex/<name>.config.toml)
printf 'model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n' > ~/.codex/fleet-astra.config.toml
fleetmux start --supervisor-cli codex --supervisor-profile fleet-astra \
  --worker-cli claude --worker-model sonnet --workers 6 --max-busy 0 \
  --lifecycle <mode> --allow-development-v2
fleetmux task goal.json
```

Validated locally with `--claude` pointing at the real Claude Code binary; the cmux `claude` shim found
first on PATH inside cmux was not exercised. Pairs and their status (verified / unverified = `--lifecycle smoke`
only / refused) live in `fleetmux/config/fleet.yaml#cli_pairs`; Codex workers (`codex+codex`) are still unverified, and any
pair the table does not list is refused with a reason before provisioning or profile retirement.
New `start` commands now require Redis v2 (the default); `--transport-version 1` is rejected. Existing v1 configs remain readable for draining/status/stop only: do not edit their transport version in place. The file-delivery and native-Codex/Claude startup descriptions above are legacy behavior, not a supported new v2 launch path. Unsupported CLI combinations fail rather than silently falling back to files. The development opt-in is still required.

V2 deliveries use run-scoped Redis Streams and full business context rather than message files
or cmux paste. Structured `fleet_decision` / `fleet_result` tools collect candidates; settled plus
daemon validation commits them. UI cards are non-context session entries. Simple replies and
mechanical failures cause no supervisor reporting turn. Task reports still require supervisor
judgment and independent artifact acceptance. External request/result JSON remains compatible.

Timeout, interruption or disconnection holds the slot/PEL until reliable retirement confirms stop.
In task mode a Claude worker's API error (StopFailure) or a lease the daemon could not renew (e.g. system
sleep) holds only that slot, never the whole fleet; the API-error turn is retried on the same slot after a
backoff, within `--max-attempts` (docs/design.md §24). In service mode the same StopFailure fails the request at
once (never re-sent by the fleet) and holds only that slot for a backoff; a model refusal also gives the slot a fresh
session, and `service_stopfail_max` (default 3) consecutive failures keep it held and tell the supervisor.
A same-run daemon restart retains sessions and pauses uncertain work; it never starts a second set
of agents or shuts Redis down as crash cleanup. Explicit v2 fleet stop retires runtimes before
shutting down Redis. New v2 Redis uses AOF everysec, with about a one-second host-crash loss window.

`fleetmux logs --config <isolated-config> --request <id> --slot worker-1 --generation 0
--event result --level error --json --follow` reads audit detail through the independent control
reader; it never ACKs work. Control failure does not determine scheduler commit.

**This is a development integration, not production acceptance.** The local merge candidate passed
the full regression suite, independent real-Redis race probes and installed Pi SDK/TUI contracts.
An earlier frozen candidate passed a real Redis + Fleet + Pi/FlashNext TUI smoke on owned PTYs
(cmux layout simulated); the final error-audit rerun was refused by the model server's memory guard.
Prepared effect intents can replay; ambiguous Redis script failures hold the run for inspection.
Full crash/slow-UI coverage, 1000-message end-to-end accounting, performance, capacity/AOF and
mixed-CLI validation remain gates. V2 browser-broker execution currently rejects explicitly.
See [the design baseline](docs/design.md#11-redis-communication-and-layered-display-v2-development-wiring-done-acceptance-pending).

Run the isolated acceptance suite on a parent that permits temporary Redis sockets:

```bash
python3 -m pytest -q tests/test_runtime_v2.py tests/test_message_bus_v2.py
python3 -m pytest -q tests/test_pi_v2_contract.py tests/test_runtime_v2_planning.py
python3 -m pytest -q tests
```

These tests use freshly created `/tmp/fleetmux-<token>.sock` instances with `port=0`, fake cmux
and no model/GUI/production registry. The installed Pi packages are required by the TS harness.

## How a task flows

1. `task` puts the goal in the supervisor's inbox. The daemon pastes it into the supervisor tab.
2. The supervisor replies with one JSON object:
   `{"status": "RUNNING", "completion": 30, "reason": "...", "dispatch": [{"to": "worker-1", "instruction": "..."}], "artifacts": [...]}`.
   Bad JSON three times in a row blocks the task.
3. Each `dispatch` item goes to that worker's inbox and is delivered when the worker is idle.
4. A worker's reply comes back to the supervisor tagged `[worker-1 report]`.
5. `COMPLETED` is checked against the declared artifacts (user-declared ones count). Failures bounce
   back as "Independent verification failed"; three bounces block the task.
6. `BLOCKED` stops the task and leaves the reason in `status` for a human.

## Agent scope files

`fleetmux/agents/supervisor.md` and `worker.md` are copied into each agent's cwd as `AGENTS.md` at
spawn (`{{WORKSPACE}}` and `{{FLEET_CHROME}}` are filled in). They contain only prohibitions, a command
table and the reply format; no methodology. Models follow prohibitions and tables far more reliably than
prose about "how to think".

## Optional: browser broker

Sandboxed agents cannot drive the user's browser (and should never be able to kill it). If you have a
script that implements the verbs `check`, `tabs`, `newtab <url>`, `go <tab> <url>`, `url <tab>` and
`js <tab> <file>`, pass it with `start --chrome-script /path/to/broker`. Workers then call
`fleet.py chrome <verb> ...` (the exact command is written into their `AGENTS.md`), the daemon validates
the verb, confines `js` files to the run directory, paces page actions (`--chrome-min-interval`, default
20 s) and runs the script on the host. Without `--chrome-script` the verbs are refused.

## Redis keys (private instance only)

| Key | Type | Content |
|---|---|---|
| `fx:events` | list | daemon BLPOP: `turn` (CLI callback), `pi_session`; `task` / `request` / `operator_input` / `pause` / `resume` / `stop` / `chrome` (with `request_id`) |
| `fx:reply:<request_id>` | string | request reply (24 h) |
| `fx:agent:<name>` | hash | role, pane, thread, generation, state (starting/idle/busy/handoff/dead), ctx_used, ctx_window, busy_since, stalls, handoff_* |
| `fx:inbox:<name>` | list | pending messages; delivered only when the agent is idle |
| `fx:replies:<name>` | list | last 5 replies (used for the handoff skeleton) |
| `fx:task` | hash | id, goal, status, completion, reason, deadline, artifacts, bad_replies, bounces |
| `fx:contracts` | string | contract tasks only: JSON phase / plan / registry / proposals / task table / per-worker report owners |
| `fx:log` | list | event log, last 2000 |
| `fx:seen:<sha256>` | string | notify de-duplication (SET NX, 24 h) |
| `fx:service` | hash | paused, reason, history_pane, bad_replies |
| `fx:req:<id>` | hash | an open request: spec, status (routing/dispatched), worker, created, attempts |
| `fx:reqq` | list | ids of open requests (timeout scan) |
| `fx:result:<id>` | string | finished request result JSON (7 days) |
| `fx:results` | list | finished results in order, last 1000 (`results --drain` pops them) |

## Test status

- **2,000+ tests** on macOS (Python 3.14): `python -m pytest -q tests` takes about 13 minutes. Tests that
  need `redis-server`, `node`, `tmux` or an installed Pi skip themselves when those are missing.
- Every supervisor × worker CLI pairing marked `verified` in `fleetmux/config/fleet.yaml#cli_pairs` passed a
  real smoke run (`scripts/smoke_pair.sh`: 1 supervisor + 2 workers, 4 checks); the run id is recorded
  in the row.
- The fleet has been run as resident service fleets and scheduled task fleets on three Macs since
  October 2026; the incidents those runs found are pinned as regression tests (search the tests for
  the dated comments).

## Module map

| Module | Role |
|---|---|
| `fleet.py` | CLI entry point and the daemon: dispatch, acceptance, rotation, brake hooks (large; split by concern is planned) |
| `runtime_v2.py`, `message_bus.py`, `agent_bridge.py` | Transport v2: private Redis message bus and the per-agent bridges |
| `cmux_manager.py` | cmux tabs and splits; every agent can be closed with the ✕ |
| `watchdog.py` | Five-state stall detection per slot |
| `fleet_config.py` | The one config file: shipped blocks plus the private override |
| `brake.py` | Budget cards, private account pools, usage-wall table |
| `contracts.py` | Interface contracts between parallel workers |
| `subfleet.py`, `named_subfleet.py` | Child fleets supervised by a worker |
| `roster.py`, `router.py`, `agent_registry.py`, `model_usage.py` | Worker roster, profile routing, model cards |
| `codex_queue.py`, `opencode_adapter.py`, `pi_extension_v2.ts` | CLI-specific delivery adapters |
| `control_view.py`, `history.py`, `presentation.py` | Read-only control page and the history of retired sessions |
| `lifecycle.py` | Lifecycle modes: what "done" means for a run |
| `lifecycle_deliver.py`, `lifecycle_close.py` | dev_task delivery gate (review / tests / merge / enqueue) and run closing |
| `session_board.py` | Cross-session board: independent Claude Code / pi sessions record what they touch and get one collision notice per side when two edit the same repository file or merge into the same branch — first come keeps going, the later one waits for the fleet supervisor's release (`session_board.py list` / `note`); hook `session_board.py hook claude`, pi `extensions/session-board.ts`; [contracts §7](docs/contracts.md) |

Every `dev_task` gate writes its `test_cmd` run to `<run>/gate/<task_id>-tests.log`: a header with the
command, cwd, start time and exit code (or timeout / start failure), then the full stdout+stderr,
capped at 20 MiB as the first 1 MiB plus the last 19 MiB with a truncation marker. A failed or
timed-out gate names that path in its problem (`tests failed (exit N); log: <path>`).

## What the live run taught us (already fixed)

- Typing a ~1.5 kB command into a fresh login shell truncates it (macOS tty line limit before the
  line editor is up). Spawns now go through a short launcher script and cmux's `--command`.
- Codex asks "Trust this folder?" per exact directory and does not accept the trust table as a `-c`
  override. The fleet runs agents in its own `CODEX_HOME` and writes the trust entry per agent.
- Running agents in your personal Codex home also runs your hooks, MCP servers and plugins inside
  them; a failing Stop hook blocked the READY turn. The isolated home fixes this too, and the
  hook-trust bypass flag is gone.
- A fresh session whose baseline usage already exceeds `--handoff-ratio` would be rotated forever
  (observed: 21 generations in ten minutes at a 1 % test ratio). Fresh sessions are never rotated;
  the daemon logs `rotate.impossible` and delivers anyway.

## Known limitations

- On macOS the default backend is cmux (WezTerm without cmux); on Linux it is fleetmux's own terminal (see
  *Terminal backend*). On macOS the daemon uses `caffeinate` when available so the machine does not sleep.
- A new cmux workspace briefly steals focus when it is created (cmux ignores `--focus false` for
  `new-workspace`); the daemon switches you back.
- Codex keeps no streaming log of a tab; when a tab is retired its final screen is saved next to the
  handoff note. The full transcript is Codex's rollout file under `~/.codex/sessions`.
- Models occasionally do more than one dispatched step in a turn. The artifact check catches the
  outcome, not the overreach.

## Development

```bash
pip install -e '.[dev]'
pytest
```

Most tests use in-memory Redis, fake cmux and synthetic transcripts. Redis v2 integration tests
start only owned temporary Redis processes; the TS contract harness starts a JSONL fixture child.
They do not start a model or a real cmux fleet.

## License

MIT
