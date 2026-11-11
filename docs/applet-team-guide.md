# Build an applet team (guide for a coding agent)

Audience: a coding agent (or a person) that is handed this page, [applet-interface.md](applet-interface.md) and
[applets.md](applets.md) and nothing else, and must produce a complete applet-based team: the fleet, its
workers, the applet project, its configuration, and the commands to run and verify it.

Every item is tagged:

- **[existing]** works in fleetmux today (roster keys, start flags, sub-fleets, channels).
- **[proposed]** is part of the applet interface, version 1, not implemented yet. A roster with `applets:` is
  rejected by a release without it. Do not present a proposed item as working until
  `fleetmux applet list` exists in the release you run.

Section numbers such as "section 29" refer to [design.md](design.md).

## 1. The shape of a team

An applet is hot-loadable and its host is the owner's choice. Two shapes:

```
Shape A: the applet on a root fleet               one machine, one OS user
root fleet "rollout" (service lifecycle)          <- the team
+- supervisor            grants and takes back leases, rules on escalations
+- daemon                the fleet's daemon + private Redis (Unix socket); watches the applet
+- applet-rollout-1      the deterministic program                [proposed]
+- fix-1                 worker
+- fix-2                 worker

Shape B: the applet on a named sub-fleet
root fleet (service lifecycle)
+- supervisor            root supervisor: rules on access requests, talks to channels
+- lead-1                a worker slot; it becomes the supervisor of the team below
+- sub-fleet "rollout"   (named, service, autostart)            <- the team
   +- supervisor         = lead-1's session: grants and takes back leases
   +- daemon             the team's own daemon + private Redis; watches the applet
   +- applet-rollout-1                                            [proposed]
   +- fix-1, fix-2       workers
```

| Choose | When |
|---|---|
| Shape A | Default. One team on its own: one fleet, one roster, the fewest moving parts. |
| Shape B | The machine already runs a root fleet for other work and the team should sit under it, or several teams share one root (one set of channels, one root supervisor for `access` requests). |

The applet code is the same for both. Changing shape later is a move (applets.md 2.1): remove the `applets:` row
from one roster, add it to the other; no fleet is restarted.

Fixed facts you must design around:

| Fact | Tag | Consequence |
|---|---|---|
| The host runs in service mode: a root fleet started with `--lifecycle service`, or a sub-fleet entry with `mode: service`. | [proposed] | A task-lifecycle fleet cannot host an applet. |
| The host's own daemon starts, watches and restarts the applet. | [proposed] | No watcher to set up, no system service. |
| When every worker is busy, the fleet wakes or adds a worker for a lease, up to `hot_add_max`. | [proposed] | Size the roster for the normal load, not for the peak. |
| A fix handed back by a leased worker is reviewed by the fleet in another slot. | [proposed] | The applet has no review code. A team that fixes things needs room for 2 workers (declared or addable). |
| Channels are declared on a root roster only. | [existing] | Shape B: on the root, never in the team roster. |
| Shape B: the team's supervisor is the root worker named in `supervisor_slot`, and it must be an official Claude Code session. | [existing] | The root fleet's workers run `--worker-cli claude`. |
| Shape B: team workers run `claude` (default) or `pi` only; one CLI per team; a `pi` team uses one `pi_model`. | [existing] | opencode and dsh workers cannot be team workers. |
| Shape B: `clear_after_result: true` is rejected in a sub-fleet entry. | [existing] | Use `fresh_after_result: true` if needed. |
| Limits (defaults): 12 agents in the whole tree; in shape B also 3 workers per sub-fleet, 3 live sub-fleets per fleet, depth 2. | [existing] | Hot-added workers count. Raise limits only in the root roster's `limits:` block. |
| Sub-fleets are not supported on the wezterm backend. | [existing] | Shape B needs the `fleetmux` or `cmux` backend. |
| A night roster's `subfleets:` and `applets:` blocks must equal the day roster's. | [existing] / [proposed] | The team does not change at night; only worker profiles may. |

## 2. Decisions, in order

### 2.1 How many workers, and which

| The applet needs | Workers declared in the roster | `lease.max_active` | `lease.hot_add_max` |
|---|---|---|---|
| Model work that changes nothing (read, classify, summarize), one job at a time | 1 | 1 | 1 |
| Fixes, one job at a time (each fix is reviewed in another slot) | 2 | 1 | 1 |
| Two jobs in parallel, each fixed and reviewed | 3 | 2 | 1 |
| Rare bursts above that | as above | as above | raise it; check the limits in section 1 |

Rules:

- Declare the workers the normal load needs. The fleet adds one when a lease or a review finds none free, and
  the idle reclaim puts it to sleep afterwards, so there is no need for spare workers "just in case".
- A reviewer must be a different slot from the fixer of that job. With one declared worker and `hot_add_max: 1`
  the fleet adds the reviewer on the first fix (a start-up delay of about a minute); declaring 2 avoids that.
- Any worker can review: the review rules are in `REVIEWER.md`, which the fleet puts in every worker directory.
  Do not create a reviewer role or a reviewer worker.
- `hot_add_max` is a cost ceiling. `0` means "never add". It can be changed while the fleet runs
  (`fleetmux applet set <name> hot_add_max <n>`, or edit the roster row).
- More than 3 workers in shape B: raise `limits.child_workers_max` and `limits.max_agents_total` in the root
  roster, or use shape A.

### 2.2 Which CLI and model for each seat

| Seat | Allowed | Choose | Why |
|---|---|---|---|
| Host supervisor, shape A | `pi`, `codex`, `claude` (the pair with the worker CLI must be `verified` in the CLI pair table) | Leave unset: the release's default pair (the first usable model of the supervisor ranking) | It decides who gets a lease and for how long; errors here cost the most, and the default ranking puts the strongest model first. |
| Root supervisor, shape B | as above | Leave unset | It only rules on rare `access` requests and operator input. |
| `lead-1` = team supervisor, shape B | official `claude` only | The strongest Claude model you have (`--worker-model`, or a roster `profile`) | It grants the leases. |
| Declared worker | shape A: the worker CLI of the pair; shape B: `claude` or `pi` | Leave unset: the first usable model of the worker ranking; `pi` + a cheaper model when the fixes are small and frequent | Fix quality decides how many attempts and review rounds a job needs. |
| Worker the fleet adds for a lease or a review | not configurable per worker | The host's default worker CLI and the first usable model of the default worker chain; the role file of `hot_add_like` | It must work without anyone choosing. |

Do not hard-code models in start scripts when the default pair is acceptable; the release ranks what is installed
and logged in (section 52) and moves down its chain when a model hits a usage wall (section 23). Review independence comes
from the separate slot and the clean session; a different model is not required.

Thinking level: Claude workers get the release default effort (`high`, `fleet.yaml#discovery.worker_thinking`);
`pi` workers take `pi_thinking` (`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`) from the sub-fleet
entry or their roster row.

### 2.3 Per-worker settings (roster rows) **[existing]**

| Key | Type | Default | Allowed | Example | Use it when |
|---|---|---|---|---|---|
| `name` | string | required | `[a-z][a-z0-9-]{0,30}`, must end in `-<number>`, not `supervisor` / `advisor` | `fix-1` | Always. Name by function. |
| `role` | path | none | existing file, relative to the roster's directory | `roles/fixer.md` | The worker has a standing duty. Appended to its rule file. |
| `writable` | list of paths | none | non-empty strings; `host:path` = that path on that host over SSH | `["/srv/rollout/work"]` | The worker always needs a directory outside its own. For per-job directories use the lease's `paths` instead. |
| `profile` | string | none | an agent card id `[a-z][a-z0-9-]{0,63}` | `opus-coder` | The slot should run a specific card. Not allowed in a `pi` team. |
| `pi_model` / `pi_thinking` | string | the entry's | `provider/model`; thinking values above | `local/my-model` | `pi` workers only; not together with `profile`. |
| `window` | int | by model | positive | `1000000` | The model's context window is not the built-in guess (section 22). |
| `handoff_ratio` | number | `0.6` | (0, 1) | `0.7` | You want rotation later or earlier for this worker. |
| `split` | map | roster-level | `check_minutes`, `window_minutes`, `min_calls`, `min_distinct`, `restore_model` | `{check_minutes: 0}` | Team workers should not be nudged to split long tasks (leased slots are exempt anyway). |

Roster top-level keys **[existing]**: `workers` (required), `limits`, `handoff`, `advisor` (`on` / `off`, default
`off`; keep `off` for a team), `subfleets`, `tabs`, `split`, `max_busy`, `channels` (root only) - and
**[proposed]** `applets`.

A row the fleet adds for a lease has only `name` and `role`. To give added workers other settings, edit the row
after it appears; the change applies to the slot's next generation (section 16).

### 2.4 Shape B only: the sub-fleet entry in the root roster **[existing]**

| Key | Type | Default | Allowed | Set it to |
|---|---|---|---|---|
| `name` | string | required | `[a-z][a-z0-9-]{0,30}`, not a worker name | the team's name, e.g. `rollout` |
| `supervisor_slot` | string | required | a root worker; one sub-fleet per worker | `lead-1` |
| `supervisor_role` | path | required | existing file | `roles/team-supervisor.md` (section 4.4) |
| `roster` | path | required | existing, valid roster | `team.yaml` |
| `autostart` | bool | `false` | | `true` (the team comes up with the fleet and is relaunched if its daemon dies) |
| `mode` | string | `service` | `service`, `task` | `service` (required for applets) |
| `worker_cli` | string | `claude` | `claude`, `pi` | per 2.2 |
| `worker_model` | string | fleet default | a Claude `--model` value | optional |
| `pi_model` / `pi_thinking` | string | none | required / optional when `worker_cli: pi` | per 2.2 |
| `timeouts` | map | none | `stall`, `stall_window`, `request`: positive seconds | leave unset (no global request deadline, section 43); set per message `timeout_s` in the applet instead |
| `max_busy` | int | `0` (no cap) | >= 0 | unset, or `2` for a `pi` team on a small local model |
| `drain_s` | number | `600` | > 0 | at least the longest step the applet cannot interrupt |
| `fresh_after_result` | bool | `false` | | `false`: leases already start and end in a clean session |
| `worker_domains` | list | parent's | domain names of the worker kit; `[]` = none | `[]` unless the team needs the root's domain skills |
| `clear_after_result`, `claude_hook_delivery` | | | retired values are rejected | do not set |

### 2.5 Channels, timeouts, night roster

| Topic | Decision table |
|---|---|
| Channels **[existing]** | Need a human to approve deletions or see alerts away from the terminal -> add `channels:` to the **root** roster (`[{type, account, push?}]`, at most 8; credentials live in `~/.fleetmux/config/`, never in the roster). Otherwise none: the operator command works without any channel. |
| Request deadline | Leave the fleet-wide one unset. Give each lease message its own `timeout_s` from an applet parameter. |
| Lease term | `ttl_s` about the time a normal job takes x 1.5; `ttl_max_s` about the longest job you would still let run unattended. Start from 1800 / 7200 and adjust the parameters from audit data (`held_s` in `applet.lease.released`). A lease does not expire while its fix is under review; afterwards its end is at least `renew_margin_s` away. |
| Review bounds **[proposed]** | `review: {max_rounds: 3, timeout_s: 1800}` in the applet row. Raise `timeout_s` when a review must read a large change. |
| Idle reclaim | Leave the service default (`worker_idle_reclaim_min`, 30 minutes): it is what puts lease-added workers to sleep. |
| Night roster **[existing]** | Only if workers should run cheaper models at night: `--night-roster` with the same worker names and the same `subfleets:` / `applets:` blocks. |
| Advisor | `off`. |

## 3. Deployment and networking

### 3.1 What runs where

| Thing | Machine | Notes |
|---|---|---|
| The host fleet (and in shape B its root), every daemon and Redis server, all agent CLIs, the applet | **One machine, one OS user** | A fleet does not span machines. |
| Things the applet acts on (deploy targets, remote test hosts) | Any | Reached by the applet's own deterministic code, normally SSH. |
| The human | Anywhere | Terminal on the machine, `fleetmux attach` over SSH, or a channel. |

### 3.2 Do I need an IP, a port or a host setting?

| Situation | Needed | Not needed |
|---|---|---|
| Applet <-> fleet, fleet <-> agents, parent <-> sub-fleet | Nothing. Each fleet's Redis listens only on a Unix socket (`--port 0`, permissions 700) under the run's runtime directory. | No IP, no port, no hostname, no firewall rule. |
| Outside caller on the **same machine** enqueues work | The fleet home (shape A) or the team's `config.json` path (shape B) for `fleetmux applet send` | No network. |
| Outside caller on **another machine** enqueues work | SSH to the fleet's machine, then the same command. A VPN or overlay network (for example Tailscale) is only the way SSH reaches the machine. | Do not expose Redis; do not write an HTTP front end unless the applet's own project needs one, and then it is that project's responsibility. |
| The applet installs or tests on **remote hosts** | SSH host aliases in the OS user's `~/.ssh/config` (host name or overlay IP, user, key) and the host list in the applet's YAML (`targets:` block). Non-interactive key auth; no password prompts. | No inbound port on the fleet's machine. |
| A leased worker must write on a remote host | Roster `writable: ["host:/path"]` for that worker **[existing]**, same SSH aliases. | |
| Channels (Slack, Telegram, Feishu, WeChat, email) | **Outbound** internet from the fleet's machine; the account's token in `~/.fleetmux/config/` (file mode 600). Slack uses Socket Mode, Telegram long polling: no inbound webhook. | No public IP, no inbound port. |
| Model access | Each agent CLI's own login on that machine (`fleetmux setup` lists what is usable). A local model gateway is a `baseUrl` in the `pi` models file, typically `127.0.0.1`. | fleetmux stores no model keys. |
| Watching the fleet from another machine | SSH, then `fleetmux attach --runtime <dir>` | No port. |

### 3.3 Never expose

- A Redis socket or any TCP forward of it. Anyone who can open it can submit requests as the fleet user.
- `~/.fleetmux/config/` (channel tokens, allowlists), `FLEETMUX_APPLET_TOKEN`, deploy keys.
- An applet-made listener bound to anything but `127.0.0.1`.
- Secrets inside a lease `purpose`, `context`, message `payload`, `notify` text or audit `detail`: models read
  those and logs keep them.

### 3.4 Cross-machine rules

- One release of fleetmux per machine; every fleet on it, parent and sub-fleet, runs from that release.
- Agent CLIs should be the same versions on every machine that runs fleets.
- An applet that manages other machines does so from one home machine; do not run two copies of the same applet
  against the same targets.
- No launchd, cron or systemd for the fleet or the applet, on any machine. The fleet's own processes watch each other.

## 4. Starter project (copy, then fill in)

The starter uses shape A. Section 4.6 lists what changes for shape B.

### 4.1 Layout of the applet project (its own repository, not inside fleetmux)

```
rollout/
+-- rollout.yaml            the one configuration file (blocks: team, applet, targets)
+-- roles/
|   +-- supervisor.md       appended to the host supervisor's rule file
|   +-- fixer.md
+-- rollout/                the Python package
|   +-- __init__.py
|   +-- applet.py           entry point: python -m rollout.applet
|   +-- steps.py            deterministic steps (merge, test, install, health, rollback)
|   +-- state.py            durable state under FLEETMUX_APPLET_STATE
+-- bin/
|   +-- env.sh  start.sh  stop.sh  resume.sh  status.sh
+-- tests/                  tests of the deterministic steps with a fake Applet / Lease
```

### 4.2 `rollout.yaml` (one YAML, blocks)

Block addressing `file.yaml#block` for rosters is **[proposed]** (applets.md open question 3). Until it exists,
put the `team` block into its own file `team.yaml` with the same content and point `--roster` at it; everything
else is unchanged.

```yaml
# ---- block: team (the host fleet's roster) ----------------------------------
team:
  workers:                                  # [existing keys]
    - {name: fix-1, role: roles/fixer.md}
    - {name: fix-2, role: roles/fixer.md}
  applets:                                  # [proposed]
    - name: applet-rollout-1
      command: ["{python}", "-m", "rollout.applet"]
      cwd: .
      params: {file: rollout.yaml, block: applet}
      lease: {max_active: 1, ttl_s: 1800, ttl_max_s: 7200, max_renewals: 3, hot_add_max: 1, hot_add_like: fix-1}
      review: {max_rounds: 3, timeout_s: 1800}
  # channels:                               # optional, root only
  #   - {type: telegram, account: ops}

# ---- block: applet (the applet's own parameters; fleetmux does not read it) --
applet:
  hot: [fix_ttl_s, fix_timeout_s, max_fix_attempts, renew_below_s, poll_s]   # loaded into Redis, written back
  writeback_s: 300
  fix_ttl_s: 1800
  fix_timeout_s: 1200
  max_fix_attempts: 3
  renew_below_s: 300
  poll_s: 5
  test_command: ["python3", "-m", "pytest", "-q"]

# ---- block: targets (cold; read at start) ------------------------------------
targets:
  local: {kind: local, install: ["scripts/install_release.sh", "--activate"]}
  # build-host: {kind: ssh, host: build-host, install: [...]}   # host = an alias in ~/.ssh/config
```

Which values are hot, and where they live:

| Value | File and block | In Redis | Changed while running by | Written back |
|---|---|---|---|---|
| The applet's own parameters listed under `hot:` | `rollout.yaml#applet` | `fx:applet:applet-rollout-1:params` | `fleetmux applet param` | by the applet, every `writeback_s` |
| `hot_add_max` | `rollout.yaml#team`, the applet row's `lease` | `fx:applet:applet-rollout-1` field `hot_add_max` | `fleetmux applet set`, or a roster edit | by the host daemon, into the row |
| Other `lease` / `review` bounds, `workers:`, `max_busy` | `rollout.yaml#team` | read from the roster on hot reload | a roster edit (section 16) | - |
| `targets` | `rollout.yaml#targets` | no (cold) | restart the applet | - |

### 4.3 `rollout/applet.py`

Start from the skeleton in [applet-interface.md section 8](applet-interface.md#8-minimal-applet-skeleton). Rules for the
code:

| Rule | Check |
|---|---|
| No model calls: no model SDK import, no `claude`, `codex`, `pi`, `opencode` subprocess. | `grep -rnE "anthropic\|openai\|claude -p\|codex exec\|pi --" rollout/` prints nothing. |
| Every threshold is a parameter in the `applet` block. | No numeric literal for a time, count or ratio in `steps.py` outside defaults passed to `params.get`. |
| State is written before the side effect it guards. | Kill the process at each step in a test; restart continues or verifies. |
| The applet decides pass / fail by its own command's exit code. | No branch on a worker's `status` text alone. |
| A worker's change is used only after the applet's own check passed and a `fix` message for it came back `review.verdict == "pass"`. | Every path in `steps.py` that merges, installs or ships a worker's change is behind both conditions. |
| No review code: the applet never asks for a reviewer and never sends a message asking a worker to review. | `grep -rniE "review" rollout/` shows only reads of `reply.review`. |
| Deletion or move -> `app.escalate(cls="delete", ...)` and wait. | `grep -rnE "rmtree\|os\.remove\|unlink\|shutil\.move\|os\.rename" rollout/` shows only lines guarded by an approved ruling (atomic `os.replace` of the applet's own state files is allowed). |
| SIGTERM -> release leases, exit 0 within `stop_grace_s`. | `kill -TERM <pid>`; `fleetmux applet leases` is empty afterwards. |

### 4.4 Role files (minimal content)

`roles/supervisor.md` (shape B: the same text as `roles/team-supervisor.md`, named in `supervisor_role`):

```
This fleet is the team "rollout". Its applet applet-rollout-1 runs the release train; you do not run it.
- "[applet lease request]": grant it when the purpose is one job and the term is plausible. Name an idle or
  asleep worker when the request lists one. When it says a grant adds a worker, grant without a slot. Deny with
  a note when the purpose is vague or wider than one job.
- "[applet lease renewal]": grant when the progress text shows movement; deny when two renewals showed none.
- "[applet escalation]" class plan / scope: rule on it. Class access: rule on it under your sandbox rules if you
  are the root supervisor; otherwise forward it with report_up and never approve it yourself. You are never asked
  about deletions.
- Never dispatch to a worker that the board shows as leased or reviewing. Fix reviews are run by the fleet
  program; do not arrange them.
```

`roles/fixer.md`: "You fix what the applet's messages describe, inside the lease purpose. Smallest change that
makes the stated check pass. Report what you changed; the applet runs the check."

No reviewer role file: a reviewing worker follows `REVIEWER.md` in its own directory **[existing]**.

### 4.5 Commands

All commands run on the fleet's machine as the fleet's OS user, from the project root.

```bash
# bin/env.sh -- sourced by every other script; without it the commands act on the default fleet home
export FLEETMUX_HOME="$HOME/.fleetmux/fleets/rollout"
export FLEETMUX_STATE="$FLEETMUX_HOME/state"

# bin/start.sh ---------------------------------------------------------------- [existing flags]
. "$(dirname "$0")/env.sh"
fleetmux start --lifecycle service --name rollout \
  --roster "rollout.yaml#team" --backend fleetmux --allow-development-v2
#   --roster team.yaml             # fallback without block addressing
#   no --supervisor-model / --worker-model: the release picks its default pair

# bin/status.sh
. "$(dirname "$0")/env.sh"
fleetmux status                    # the fleet, its workers, applets and leases (JSON)
fleetmux applet list               # [proposed]
fleetmux applet leases             # [proposed]
fleetmux applet reviews            # [proposed]
fleetmux logs --limit 50           # audit

# bin/stop.sh      (the applet gets host.draining, then SIGTERM)
. "$(dirname "$0")/env.sh"
fleetmux stop

# bin/resume.sh    (same run, same sessions where possible)
. "$(dirname "$0")/env.sh"
fleetmux start --resume auto --allow-development-v2

# any other shell: load the environment first
. bin/env.sh

# enqueue work for the applet                                                   [proposed]
echo '{"kind": "branch", "ref": "feat/example"}' | fleetmux applet send applet-rollout-1 -

# restart only the applet after changing its code                               [proposed]
fleetmux applet restart applet-rollout-1

# detach the applet while the fleet runs: delete its row under applets: in rollout.yaml   [proposed]
# attach it again (here or on another fleet): add the row back
# raise the worker ceiling while the fleet runs                                 [proposed]
fleetmux applet set applet-rollout-1 hot_add_max 2
```

### 4.6 What changes for shape B

| Item | Shape B |
|---|---|
| `rollout.yaml` | Two roster blocks. `fleet` (root roster): `workers: [{name: lead-1}]` and `subfleets: [{name: rollout, supervisor_slot: lead-1, supervisor_role: roles/team-supervisor.md, roster: rollout.yaml#team, autostart: true, mode: service, drain_s: 900, worker_domains: []}]`; `channels:` go here. `team`: as in 4.2, without `channels:`. |
| `bin/start.sh` | `--roster "rollout.yaml#fleet" --worker-cli claude`; add `--allow-unsandboxed-pi` only when the team's `worker_cli` is `pi`. |
| Commands about the team | Need the team's config: add to `bin/env.sh` the function `team_cfg() { fleetmux status \| python3 -c 'import json,sys; print(next((v["run"] + "/config.json" for v in json.load(sys.stdin).get("subfleets", {}).values() if v.get("name") == "rollout"), ""))'; }` and pass `--config "$(team_cfg)"` to every `fleetmux applet ...`, `fleetmux logs` and `fleetmux status` about the team. |
| `access` escalations | Go to the root supervisor through `report_up`. |
| Supervisor rotation of the team | Is a down/up of the sub-fleet: leases end, the applet starts again (applets.md open question 2). Set `drain_s` to cover the longest uninterruptible step. |

## 5. Checklist with proofs

Do the steps in order. Each has a command and the output that proves it. Stop at the first step whose proof fails.
The commands are for shape A; for shape B add `--config "$(team_cfg)"` as in 4.6.

| # | Step | Command | Proof |
|---|---|---|---|
| 1 | fleetmux is installed and can find agent CLIs | `fleetmux --version && fleetmux setup` | A version line; the table lists at least one logged-in supervisor candidate and one worker candidate. |
| 2 | The release implements applets | `fleetmux applet --help` | Usage text, exit 0. Exit 2 "unknown" -> the release has no applet support: stop, the team cannot run yet. |
| 3 | Project layout exists | `ls rollout.yaml roles/ rollout/applet.py bin/env.sh bin/start.sh` | All listed, no error. |
| 4 | Names are valid | `grep -nE "name: " rollout.yaml` | Every worker ends in `-<number>`; the applet matches `applet-<name>-<n>`; no name is used twice. |
| 5 | The applet makes no model calls and has no review code | the two `grep` checks of 4.3 | No output / only `reply.review` reads. |
| 6 | Deterministic steps pass their tests without a fleet | `python3 -m pytest -q tests` | Exit 0. |
| 7 | The fleet starts | `bin/start.sh` | Exit 0; prints the run directory. |
| 8 | Agents are ready | `. bin/env.sh; fleetmux status` | The supervisor, `fix-1` and `fix-2` are listed; workers `idle`; no slot is `held`. Keep this shell for the steps below. |
| 9 | The applet is up | `fleetmux applet list` | `applet-rollout-1`, `state: up`, heartbeat age < 60 s, `restarts: 0`. |
| 10 | A lease round trip works | enqueue one small real job (4.5), then `fleetmux applet leases --all` | One lease with `state: released`, `messages` >= 1. |
| 11 | The fix was reviewed in another slot | `fleetmux applet reviews --all` | One review for that job: `verdict: pass`, and `reviewer` is a different slot from `fixer`. |
| 12 | Exclusivity holds | while a lease is active: `echo '{"id":"probe-1","to":"fix-1","payload":{},"reply":{"required":["x"]}}' \| fleetmux request -` | Result `rejected`, reason `slot_leased`. |
| 13 | A worker is added when none is free | keep both workers busy with direct requests (`for w in fix-1 fix-2; do echo '{"id":"busy-'$w'","to":"'$w'","payload":{},"instruction":"Run: sleep 240. Then reply.","reply":{"required":["done"]}}' \| fleetmux request - & done`), then enqueue one job; `fleetmux logs --limit 200 \| grep applet.worker_added` | One event naming `fix-3`; `fleetmux status` lists `fix-3` with `applet_added_for`. |
| 14 | The applet survives a kill | `kill -KILL <pid from step 9>`; wait 60 s; repeat step 9 | `state: up`, `restarts: 1`; `fleetmux applet leases` shows no lease left from before the kill; the interrupted job continues or is redone. |
| 15 | Detach and attach work while the fleet runs | delete the row under `applets:`; wait 90 s; `fleetmux applet list`; `pgrep -fl "rollout.applet"`; `fleetmux status`; then put the row back and repeat step 9 | After the delete: no applet listed, no process, the workers still listed and none restarted (same labels as before). After the add: `state: up`. |
| 16 | Stop is clean | `bin/stop.sh`, then `pgrep -fl "rollout.applet"` | No process. |
| 17 | Resume works | `bin/resume.sh`, then steps 8-9 | Same proofs. |
| 18 | Nothing is exposed | `lsof -nP -iTCP -sTCP:LISTEN \| grep -E "redis\|python"` | No Redis listener; no applet listener except, if the project has one, `127.0.0.1`. |
| 19 | No system service was installed | `crontab -l; ls ~/Library/LaunchAgents 2>/dev/null; systemctl --user list-units 2>/dev/null \| grep -i rollout` | Nothing that names the applet or the fleet. |

Steps 10-15 use a real job on purpose: they prove the function works. They are not a trial period; once they
pass, the team is in service.

## 6. Common mistakes

| Mistake | What happens | Fix |
|---|---|---|
| `applets:` in the roster of a task-lifecycle fleet or a `mode: task` sub-fleet | Whole roster rejected | Host it on a service fleet (applets.md 1.3). |
| The same applet row in two rosters | The second host waits (`applet.attach_wait`) | Keep one row; to move, remove first, then add. |
| Writing review code in the applet, or adding a `review-1` worker with a reviewer role | Duplicates what the fleet does; the fleet still reviews every `fix` message | Remove it; read `reply.review`. |
| Using a worker's change without a `fix` message | The change is never reviewed; the audit shows a lease released with no review | Hand the fix back with `fix` and use it only on `pass`. |
| `hot_add_max: 0` with one worker and fixes | Reviews end `none` / `no_reviewer`; lease requests answer `worker_limit` when the worker is busy | Declare 2 workers or allow adding. |
| Retrying at once on `worker_limit` | Busy loop | Wait `retry_after_s`; raise `hot_add_max` if it keeps happening. |
| Keeping a lease across jobs "to save time" | Other work starves; the supervisor will deny renewals | Release when the job is done; request again. |
| Running `fleetmux stop` / `status` without `bin/env.sh` | The command acts on the default fleet home, possibly another fleet | Always source `bin/env.sh` first. |
| An applet with third-party packages started with `{python}` | Import error at start, exit 1, crashloop | Use the project's own environment (applet-interface.md section 2.3). |
| Job state only in Redis | Lost when the host goes down, or on a move | Write it under `FLEETMUX_APPLET_STATE`. |
| Asking the worker "did it pass?" | Unreliable verdicts | Run the check in the applet. |
| `applet.notify` for every step | Supervisor context fills; rate limit refuses | Use `applet.audit`; notify only on events the supervisor must know. |
| Shape B: a `pi` team without `--allow-unsandboxed-pi` on the root | The team's `up` is refused by the program | Add the flag to `bin/start.sh`. |
| Shape B: team roster with `channels:` | Whole roster rejected | Declare channels on the root roster. |
| Hard-coded absolute home paths in rosters or prompts | Break on another machine | Paths relative to the roster, or `~`. |
