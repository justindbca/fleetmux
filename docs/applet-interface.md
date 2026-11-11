# fleetmux applet interface (developer reference)

Status: **proposed, interface version 1, not implemented.** Everything on this page is new unless a row says
*existing*. A roster that contains `applets:` is rejected as a whole by releases that do not implement this page
(the roster parser refuses unknown top-level keys).

Read with: [applets.md](applets.md) (design: why each rule exists, state machines, failure modes) and
[applet-team-guide.md](applet-team-guide.md) (how to size, configure, deploy and start a whole team).
This page is the single source for key names, field tables, commands and signatures; the other two link here.

## 0. Terms

| Term | Meaning |
|---|---|
| applet | A deterministic program that runs as a process of a fleet next to its daemon and carries a team's custom function. It never calls a model. It can be attached, detached and moved while fleets run. |
| host | The fleet whose roster declares the applet: a root fleet or a named sub-fleet, in service mode (section 1). Its supervisor, workers, daemon and private Redis are the applet's whole world. |
| applet lease | A grant by the host's supervisor: one worker slot, for a limited time and a stated scope, is driven exclusively by one applet. |
| leased worker | The worker slot named in an active applet lease. |
| fix review | The independent review the host runs, with a worker in another slot, when a leased worker hands back a fix. The applet takes no part in it (section 3.9). |
| op | A JSON object the applet submits to the host fleet (lease request, renewal, release, escalation, ...). |

Do not confuse an **applet lease** with two older things that also say "lease": the bus delivery lease of a slot
(`lease.reclaimed`, `--lease-reclaim`, design.md section 24) and the channel credential lease (`channel.lease_*`, design.md section 53).
Every key, audit event and field of this interface is prefixed `applet` for that reason. An applet is also not a
worker kit (design.md section 30): a kit is a read-only data package of rules and roster templates; an applet is a process.

## 1. Roster: the `applets:` block

Where: the roster of the **host**. Allowed hosts:

| Host | Roster file | Condition |
|---|---|---|
| Root fleet | the file given to `start --roster` | started with `--lifecycle service` |
| Named sub-fleet (design.md section 29) | the file its parent's `subfleets[].roster` points to | entry `mode: service` |

A fleet in a task lifecycle or a sub-fleet with `mode: task` refuses a roster with `applets:`: ops and lease
messages are service requests, and such a fleet has no request entry. Transport v2 is required.

Hot reload follows design.md section 16 and is how an applet is attached and detached while the fleet runs:

| Roster change | Effect |
|---|---|
| Row added | The applet is declared and (if `autostart`) started. The fleet is not restarted; no worker is touched. A name used earlier in the run may come back. |
| Row removed | Detach: new lease requests are refused, the applet gets `host.draining` (`reason: detach`) and up to `detach_wait_s` to release its leases, leases still open are revoked (`applet_detached`), then SIGTERM, `stop_grace_s`, SIGKILL. Putting the row back before the revoke cancels it. |
| Row edited | `lease` and `review` values apply from the next op on. Every other field applies at the applet's next start. |
| Move to another host | Remove the row from one roster and add it to the other. Leases do not move; the applet applies again on the new host. The new host starts the applet only after the old process has exited (owner record `host.json` in the state directory). |
| Any invalid row | The whole roster is rejected and the running roster stays. |

With a night roster (design.md section 25) the `applets:` block must be equal in both files, as for `subfleets:`.
An applet name must be unique among the fleets that share one fleet home.

```yaml
workers:
  - {name: fix-1, role: roles/fixer.md}
  - {name: fix-2, role: roles/fixer.md}
applets:
  - name: applet-rollout-1
    command: ["{python}", "-m", "rollout.applet"]
    cwd: ..
    params: {file: ../rollout.yaml, block: applet}
    lease: {max_active: 2, ttl_s: 1800, ttl_max_s: 7200, hot_add_max: 2}
```

### 1.1 Entry fields

| Field | Type | Default | Allowed values | Example | Meaning |
|---|---|---|---|---|---|
| `name` | string | required | `^applet-[a-z][a-z0-9]*(-[a-z0-9]+)*-[0-9]+$`, at most 48 characters, unique in the roster, not a worker or sub-fleet name | `applet-rollout-1` | Identity everywhere: Redis keys, audit, status, tab title. |
| `command` | list of strings | required | non-empty; run with no shell; placeholders `{python}` (the release's Python), `{state}`, `{run}` are substituted | `["{python}", "-m", "rollout.applet"]` | The program. One process; it may start children it stops itself. |
| `cwd` | string | the roster's directory | an existing directory; relative paths resolve against the roster's directory; `~` expanded | `..` | Working directory. |
| `env` | map string -> string | `{}` | names must not start with `FLEETMUX_` | `{LOG_LEVEL: info}` | Extra environment. |
| `params` | map | none | `{file: <path>, block: <name>}`; `file` must exist | `{file: ../rollout.yaml, block: applet}` | Where the applet's own parameters live. fleetmux does not parse the block; it passes the pair in the environment (section 2.2, use in 2.5). |
| `autostart` | bool | `true` | | `true` | Start whenever the host comes up. `false`: start only on `fleetmux applet start`. |
| `interface` | int | `1` | versions the release supports | `1` | Interface version the applet was written for. A mismatch refuses the start (`applet.config_error`). |
| `heartbeat_s` | number | `10` | > 0 | `10` | How often the applet must write its heartbeat. |
| `stale_after_s` | number | `60` | > `heartbeat_s` | `60` | Heartbeat older than this with a live process = stalled (restart). |
| `stop_grace_s` | number | `30` | > 0 | `30` | Seconds between SIGTERM and SIGKILL. |
| `detach_wait_s` | number | `60` | >= 0 | `60` | On a detach or a root host stop: how long the host waits for the applet to release its leases before it revokes them. |
| `lease` | map | see 1.2 | | | Lease policy bounds for this applet. |
| `review` | map | see 1.3 | | | Bounds of the fix reviews the host runs for this applet. |
| `notify` | map | `{max_per_hour: 6}` | `max_per_hour` integer >= 0 | `{max_per_hour: 6}` | Cap on `applet.notify` ops (each costs the supervisor a turn). |

### 1.2 `lease` fields

| Field | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `max_active` | int | `2` | >= 1 | Leases this applet may hold at once (requested + granted + active). |
| `ttl_s` | number | `1800` | 60 ... `ttl_max_s` | Term used when a request or a grant names none. |
| `ttl_max_s` | number | `7200` | >= 60 | Upper bound of any single term (request, grant or renewal); larger values are clipped and the clip is reported. |
| `max_renewals` | int | `3` | >= 0 | Renewals one lease may receive. Past it the program denies without asking the supervisor. |
| `renew_margin_s` | number | `300` | > 0, < `ttl_s` | The host sends `lease.expiring` this long before expiry. |
| `renew_grace_s` | number | `0` | >= 0 | A renewal still waiting for the supervisor at expiry keeps the lease alive this much longer. `0` = strict expiry. |
| `prepare_timeout_s` | number | `300` | > 0 | A granted lease whose worker is not ready within this is ended (`prepare_failed`). |
| `eligible` | list of worker names | all workers of this roster | names in `workers:` | Slots that may be leased to this applet. Workers the host added for this applet (below) are eligible too. |
| `hot_add_max` | int | `2` | >= 0 | Ceiling of workers the host may add to the roster for this applet's leases and reviews when no worker is free (section 3.3). Counts lease-added rows present in the roster, awake or asleep. `0` = never add. Hot value: loaded into `fx:applet:<name>` (`hot_add_max`) on every roster load; `fleetmux applet set <name> hot_add_max <n>` changes the running value and the host daemon writes it back into this row. |
| `hot_add_like` | worker name | first name of `eligible` | a name in `workers:` | Template for an added worker: the new row takes this worker's name base and `role`. |
| `decision_wait_s` | number | `600` | 10 ... 86400 | How long a lease request or renewal waits for the supervisor before it is answered `timeout`. |

### 1.3 `review` fields

| Field | Type | Default | Allowed | Meaning |
|---|---|---|---|---|
| `max_rounds` | int | `3` | >= 1 | FAIL verdicts one `job` may collect. After that the result says `capped` and further `fix` messages of the job are refused. |
| `timeout_s` | number | `1800` | 60 ... 86400 | Upper bound of one review, from the worker's reply to the verdict, waiting for a reviewer slot included. |

The applet's own values that should change while the fleet runs belong in its parameter block (section 2.5), not here.

## 2. Process contract

### 2.1 Who starts it, when, and where

| Moment | What happens |
|---|---|
| Host supervisor is READY, or a row is added to a running host | For each `autostart` entry, in roster order: the process is started in its own terminal tab of the host's workspace, titled with the applet name, after the daemon's tab. It is started and watched by the **host's own daemon**, on a root host and on a sub-fleet host alike. It is not a child process of the daemon. |
| Host daemon dies | The applet keeps running, unwatched: nobody restarts it if it exits, ops stay `pending`, messages are not delivered, lease clocks keep running. |
| Host daemon is back (`daemon --resume`; brought back by the parent daemon for a sub-fleet, by the supervisor or an operator for a root fleet, design.md section 29 / section 36) | The host Redis is the same, so leases and applet keys survive. An applet whose pid is alive and whose heartbeat is fresh is adopted without a restart. One that is gone has its leases ended (`applet_down`) and is started again (counted as one crash). Pending ops are processed; resend with the same `id` if unsure. Leases past their end expire. A lease message that was in flight follows the resume rules of design.md section 17: it is stopped and delivered again when `replay` allows, else answered `timeout`; a worker that cannot be reattached starts its next generation (`worker.rotated`). |
| Row removed (detach) | Section 1. |
| Host drains or stops (root: `fleetmux stop`; sub-fleet: external `subfleet.down`, removed sub-fleet row, supervisor rotation of the host, parent stop) | `host.draining` is sent to the applet. The host waits until the applet holds no lease: a sub-fleet at most the sub-fleet entry's `drain_s` (design.md section 29), a root fleet at most `detach_wait_s`. Then SIGTERM, `stop_grace_s`, SIGKILL. Leases still open are ended with reason `host_down`. |
| Host Redis is gone and the applet still runs (an unclean stop) | The SDK exits `0` after `stale_after_s` without Redis. |
| Host comes up again | A sub-fleet host has a fresh Redis (the previous run's files are archived); a resumed root host keeps its own. The applet is started again and must rely only on what it kept under `FLEETMUX_APPLET_STATE`. |

No launchd, cron, systemd or other system facility is involved, and an applet must not install one.

### 2.2 Environment

| Variable | Example | Meaning |
|---|---|---|
| `FLEETMUX_APPLET_NAME` | `applet-rollout-1` | The entry's `name`. |
| `FLEETMUX_APPLET_INTERFACE` | `1` | Interface version the host speaks. |
| `FLEETMUX_APPLET_CONFIG` | `<run>/config.json` (root host) or `<parent run>/sub/rollout/config.json` (sub-fleet host) | The host's run config; what `--config` takes for every `fleetmux` command about this host. |
| `FLEETMUX_APPLET_REDIS` | `/.../redis.sock` | Unix socket of the host's private Redis. There is no TCP port. |
| `FLEETMUX_APPLET_TOKEN` | 32 hex characters | Proves ops come from this applet instance. New on every start. Never log it. |
| `FLEETMUX_APPLET_STATE` | `<fleet home>/applets/applet-rollout-1/` | Durable directory owned by the applet. Survives restarts, detach and attach, host down/up and run archiving. Fleets that share a fleet home (a root and its sub-fleets) share it, so it follows a move between them. fleetmux creates it, keeps the owner record `host.json` in it, and never deletes or copies it. |
| `FLEETMUX_APPLET_RUN` | `<host run>/applets/applet-rollout-1/` | Per-run scratch and logs; archived with the run. |
| `FLEETMUX_APPLET_PARAMS_FILE` / `FLEETMUX_APPLET_PARAMS_BLOCK` | `/abs/rollout.yaml` / `applet` | From `params`; unset when the entry has none. |
| `FLEETMUX_APPLET_SDK` | `<release>` | Directory containing the release's `fleetmux` package; also prepended to `PYTHONPATH` (section 2.3). |
| `FLEETMUX_APPLET_WORKERS` | `fix-1,fix-2` | The `eligible` slots at start (informational; the supervisor picks, and the host may add workers). |
| `FLEETMUX_HOME`, `FLEETMUX_STATE` | | *Existing* (contracts.md section 5), same values as the host's. |

argv is exactly `command` after placeholder substitution. stdin is closed. stdout and stderr go to the tab and to
`$FLEETMUX_APPLET_RUN/applet.log`.

### 2.3 Interpreter and dependencies

| The applet | `command` | How it imports `fleetmux.applet` |
|---|---|---|
| Is Python and needs only the standard library and what the release already ships (`redis`, `PyYAML`) | `["{python}", "-m", "<package>.applet"]` with `cwd` = the project root | `{python}` is the release's own interpreter; the package is already importable. |
| Is Python and has its own third-party packages | `[".venv/bin/python", "-m", "<package>.applet"]` (the project's own virtual environment, created by the project) | The host sets `FLEETMUX_APPLET_SDK` to the directory that contains the running release's `fleetmux` package and prepends it to `PYTHONPATH`, so the SDK always matches the host. The project's environment must provide `redis`. `fleetmux.applet` imports nothing else from the release. |
| Is not Python | any executable | Implements section 3 directly with a Redis client; `fleetmux request` and `fleetmux applet ...` are available as commands. |

fleetmux never installs packages for an applet and never modifies its environment.

### 2.4 Signals, heartbeat, exit codes

| Signal | Meaning for the applet |
|---|---|
| SIGTERM | Stop: finish or checkpoint the current step, release leases, exit `0` within `stop_grace_s`. The only stop signal. |
| SIGKILL | Sent after `stop_grace_s`. The applet must be able to resume from `FLEETMUX_APPLET_STATE` after it. |
| SIGINT, SIGHUP | Never sent. Do not rely on them. |

Heartbeat: every `heartbeat_s` the applet writes `hb_at` (Unix seconds) and `pid` into the hash `fx:applet:<name>`
(section 3.1). The SDK does this on a background thread.

| Exit code | Name | The host then |
|---|---|---|
| `0` | done | Does not restart it until the next host up or `fleetmux applet start`. State `exited`. |
| `75` | restart me | Restarts at once; not counted as a crash. Use after a self-update or a parameter change that needs a new process. |
| `78` | config error | Does not restart. State `config_error`; the reason is the last stderr line. |
| any other, or a signal | crash | Its leases are ended (`applet_down`) and it is restarted at once by the host daemon. The third crash or stall within 3600 s -> state `crashloop`, no more restarts until `fleetmux applet start`. There is no backoff: two fast restarts, then stop and say so. |

### 2.5 Parameters (one YAML, blocks, hot values in Redis)

The applet project keeps **one** YAML file; each concern is a block. Blocks that are read or changed often are
loaded into the host Redis at start and written back to the same block on a timer.

| Step | Who | What |
|---|---|---|
| Start | applet (SDK `Params`) | Read block `FLEETMUX_APPLET_PARAMS_BLOCK` from the file; `HSET fx:applet:<name>:params <key> <JSON value>` for every key listed in the block's `hot:` list. |
| Run | applet | Read hot keys from the hash on every use; cold keys from the loaded block. |
| Change | operator, or the supervisor through an approved escalation | `fleetmux applet param <name> <key> <json>` -> `HSET` + audit `applet.param_changed`. |
| Write-back | applet (SDK) | Every `writeback_s` (a key of the same block, default 300) rewrite only the changed hot keys of that block in the YAML, atomically (`os.replace`). |

The host Redis does not outlive a host down/up, so the YAML is the durable copy; at most `writeback_s` of changes
can be lost on a kill.

When the host roster is a block of the same file, the host daemon writes that file too (a worker row it adds,
`hot_add_max`). One rule serialises the writers: every writer, SDK and daemon, takes an exclusive `flock` on
`<file>.lock`, reads the file again, changes only its own block or row, and replaces the file atomically. A
writer never writes back a copy it read before taking the lock.

## 3. Wire protocol

Transport: the host's private Redis over its Unix socket. **Writes go through one door** (ops and messages are
submitted as request events, exactly like `fleetmux request`, design.md section 7 / contracts.md section 4); **reads use the
keys below**. The applet never writes any `fx:*` key except those marked *applet-writable*.

### 3.1 Keys

| Key | Type | Written by | Content |
|---|---|---|---|
| `fx:applet:<name>` | hash | the host daemon writes `state`, `started_at`, `restarts`, `crashes`, `token_sha256`, `interface`, `paused`, `hot_add_max`; fields `pid`, `hb_at`, `phase` are *applet-writable* | `state` (`declared` / `starting` / `up` / `stalled` / `stopping` / `exited` / `crashloop` / `config_error`), `pid`, `started_at`, `hb_at`, `restarts`, `crashes` (JSON list of times), `interface`, `token_sha256`, `phase` (free text <= 200 characters shown in status), `paused` (`0`/`1`), `hot_add_max` |
| `fx:applet:<name>:leases` | set | host | Ids of leases not yet ended. |
| `fx:applet:<name>:lease:<lease_id>` | hash | host | The lease record (3.3). Kept 7 days after it ends. |
| `fx:applet:<name>:review:<review_id>` | hash | host | A fix review (3.9): `id`, `job`, `lease`, `message`, `fixer`, `reviewer`, `round`, `state` (`waiting` / `running` / `done`), `verdict`, `reason`, `started_at`, `ended_at`. Kept 7 days after it ends. |
| `fx:applet:<name>:events` | stream | host | Host -> applet events (3.6), capped at about 1000 entries. Read with `XREAD`. |
| `fx:applet:<name>:inbox` | list | `fleetmux applet send` (anyone with the socket) | JSON items for the applet from outside callers (3.8). The applet `BLPOP`s and must persist an item before acting on it. |
| `fx:applet:<name>:params` | hash | *applet-writable*; also `fleetmux applet param` | Hot parameters, JSON-encoded values (2.5). |
| `fx:applet:<name>:esc:<op id>` | hash | host | An open or answered escalation (3.7). |
| `fx:agent:<slot>` (*existing*) | hash | host | Gains `applet_lease` (lease id or empty), `applet_lease_holder`, `applet_lease_until`; `applet_review` (review id or empty) while the slot reviews a fix; `applet_added_for` (applet name) on a row the host added for a lease or a review. The slot's `state` keeps its existing values (`idle`, `busy`, `held`, ...). |
| `fx:result:<id>` (*existing*) | string (JSON) | host | Answer to every op and every message, by its `id`. |

`<lease_id>` is `L-` + 8 lowercase hex digits and a review id is `R-` + 8 lowercase hex digits, both issued by the host. Op and message ids are chosen by the applet and
must match the existing request id rule; use `<applet name>.<counter or uuid>`.

### 3.2 Submitting an op

Submit `{"kind": "request", "spec": <op>}` on the host bus (the SDK does this; from a shell:
`fleetmux request - --config "$FLEETMUX_APPLET_CONFIG" < op.json`). The host recognizes an op by its `op` field and
handles it in its own branch: it does not enter the request queue and is not subject to `--request-timeout` (the
same pattern as `subfleet.up`, design.md section 29). Resending an op with the same `id` is safe: an unfinished one answers
`in_progress`, a finished one answers `duplicate` and the first result stays in `fx:result:<id>`.

Common fields of every op:

| Field | Type | Required | Meaning |
|---|---|---|---|
| `id` | string | yes | Unique id; the answer appears at `fx:result:<id>`. |
| `op` | string | yes | One of the names in the table below. |
| `applet` | string | yes | `FLEETMUX_APPLET_NAME`. |
| `token` | string | yes | `FLEETMUX_APPLET_TOKEN`. |
| `v` | int | yes | `1`. |

| `op` | Decided by | Section |
|---|---|---|
| `applet.lease.request` | program gate, then the supervisor | 3.3 |
| `applet.lease.renew` | program gate, then the supervisor | 3.4 |
| `applet.lease.release` | program | 3.4 |
| `applet.escalate` | the supervisor (delete class: the human owner only) | 3.7 |
| `applet.notify` | program (rate limit) | 3.7 |
| `applet.audit` | program | 3.7 |

Common answer shape at `fx:result:<id>` (*existing* envelope: `id`, `status`, `at`, `elapsed_s`):

| `status` | Meaning | Extra fields |
|---|---|---|
| `ok` | Done / granted / approved. | per op |
| `rejected` | Refused. | `by`: `program` or `supervisor`; `reason` (stable code, table in 3.9); `note` (text); `retry_after_s` (optional) |
| `timeout` | Nobody decided within `decision_wait_s`. | |
| `in_progress` | Same id resent while open. | `op_status`: `awaiting` or `approved` |

### 3.3 `applet.lease.request`

```json
{"id": "applet-rollout-1.42", "op": "applet.lease.request", "applet": "applet-rollout-1", "token": "...", "v": 1,
 "job": "batch-0007", "purpose": "Resolve the merge conflict in src/config.py and make tests/test_config.py pass",
 "ttl_s": 1800, "paths": ["/srv/rollout/work/batch-0007"], "want": {"slot": null, "role": "fixer"},
 "context": {"failing_tests": ["tests/test_config.py::test_load"], "branch": "batch/0007"}}
```

| Field | Type | Required | Default | Allowed | Meaning |
|---|---|---|---|---|---|
| `job` | string | yes | | 1-64 characters `[A-Za-z0-9._-]` | The applet's own unit of work. Fix reviews count their rounds per job and never use a slot that held a lease of the same job. |
| `purpose` | string | yes | | <= 2000 characters | The scope in words. Shown to the supervisor and written into the leased worker's rule file. |
| `ttl_s` | number | no | entry `ttl_s` | 60 ... `ttl_max_s` | Requested term. |
| `paths` | list of absolute paths | no | `[]` | normalized, absolute | Extra directories the leased worker may write for this lease (the worker's own directory is always writable). |
| `want` | map | no | `{}` | `slot`: an eligible worker name; `role`: free text | A hint for the supervisor. Never binding. |
| `context` | JSON object | no | `{}` | <= 20 000 characters as JSON | Facts for the supervisor's decision (not sent to the worker). |

Program gate, in order (first failure answers `rejected`, `by: program`):

| # | Check | `reason` |
|---|---|---|
| 1 | `token` matches, applet state `up` and not being detached, `v` supported | `bad_token` / `applet_not_up` / `applet_detaching` / `bad_version` |
| 2 | Host not paused and not draining | `host_paused` / `host_draining` |
| 3 | Open leases < `max_active` | `max_active` |
| 4 | Shape valid | `invalid` |
| 5 | A slot can be had (table below) | `worker_limit` (with `retry_after_s`) only when none can |

A lease request is never refused because every worker is busy. Finding a slot, in order:

| Step | Condition | The supervisor is told | On a grant |
|---|---|---|---|
| 1 | An eligible slot is idle, READY, has nothing queued, is not leased, is not reviewing and is not the supervisor slot of a sub-fleet. | The worker board. | `slot` names it. |
| 2 | An eligible slot is asleep (`reclaimed` by the idle reclaim, `worker_idle_reclaim_min`). | The board lists it as asleep. | `slot` names it; the host wakes it. |
| 3 | Neither; lease-added rows of this applet < `hot_add_max`; the fleet limits (`child_workers_max`, `max_agents_total`, design.md section 19) allow one more worker. | "No worker is free; a grant adds worker `<name>`." | No `slot` in the answer. The host daemon writes the row `- {name: <base>-<n>, role: <template role>}` into the host roster (and the night roster) and starts it as any hot-added worker (design.md section 16). `<base>` and the role come from `hot_add_like`; `<n>` is the next number never used for that base in this run. The row sets no CLI and no model: the worker runs the host's default worker CLI and the first usable model of the default worker chain (`fleet.yaml#discovery.worker`). |
| 4 | None of the above. | Nothing; no turn is spent. | `rejected`, `by: program`, `worker_limit`, `retry_after_s`. |

After the lease ends an added worker is an ordinary worker of the roster. The idle reclaim puts it to sleep after
`worker_idle_reclaim_min` idle minutes (no session, no pane); step 2 wakes it for a later lease before a new row
is added. Deleting its line from the roster removes it (design.md section 16).

The supervisor gets one message `[applet lease request] ...` (applet, job, purpose, term, paths, context, the
worker board or the name of the worker a grant would add, how to answer) and answers in its service decision
(3.5). The program re-checks the answer: a named slot must be eligible and still free; `ttl_s` is clipped to
`ttl_max_s`; `paths` may only narrow what was requested. A bad answer goes back through the supervisor's usual
correction path.

Answer on grant:

```json
{"id": "applet-rollout-1.42", "status": "ok",
 "lease": {"id": "L-3fa9c210", "slot": "fix-1", "label": "fix-1.2", "job": "batch-0007", "state": "granted",
           "ttl_s": 1800, "paths": ["/srv/rollout/work/batch-0007"], "added": false, "note": "..."}}
```

Lease record (`fx:applet:<name>:lease:<id>`; all values strings, JSON where noted):

| Field | Meaning |
|---|---|
| `id`, `applet`, `job`, `purpose` | As requested. |
| `slot` | Worker slot (stable across generations). |
| `added` | `1` when the host added this worker for the lease. |
| `state` | `requested`, `denied`, `granted`, `active`, `renew_requested`, `released`, `expired`, `revoked`. |
| `requested_at`, `granted_at`, `active_at`, `expires_at`, `ended_at` | Unix seconds. `expires_at` is set when the lease becomes `active`. |
| `ttl_s`, `renewals` | Current term; renewals received. |
| `paths` | JSON list actually granted. |
| `granted_by` | Label of the supervisor generation that decided. |
| `messages`, `last_message`, `last_status` | Count, id and result status of messages sent under the lease. |
| `end_reason` | For `revoked`: `supervisor`, `operator`, `host_down`, `applet_down`, `applet_detached`, `slot_held`, `slot_retired`, `prepare_failed`. For `denied`: the rejection reason. |
| `last_review` | JSON `{id, verdict, round, message}` of the last fix review under this lease; empty when none. |
| `outcome` | JSON the applet passed at release. |

`granted` -> `active`: the host wakes or adds the worker when needed, starts a **clean session** in the slot (design.md section 37 `fresh`; skipped for a session
that has done no work), writes the lease section into the worker's rule file (section 4), issues the `paths` as
program grants (design.md section 46), and when the worker is READY sets `active`, starts the term and emits `lease.active`.
Messages may be submitted while `granted`; they are delivered once `active`.

### 3.4 `applet.lease.renew` and `applet.lease.release`

```json
{"id": "applet-rollout-1.57", "op": "applet.lease.renew", "applet": "applet-rollout-1", "token": "...", "v": 1,
 "lease": "L-3fa9c210", "need_s": 900, "progress": "2 of 3 failing tests pass; the third needs a fixture change",
 "why": "the worker's last reply shows it is still converging"}
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `lease` | string | yes | An `active` lease of this applet. |
| `need_s` | number | yes | Extra seconds wanted, 60 ... `ttl_max_s`. |
| `progress` | string | yes | What is done, <= 2000 characters. Shown to the supervisor. |
| `why` | string | no | Why more time is justified. |

Program gate: token and state; lease `active`; `renewals` < `max_renewals` (else `rejected`, `max_renewals`); one
renewal open per lease (`renew_pending`). Then the supervisor gets `[applet lease renewal] ...` and grants (optionally
a shorter `ttl_s`) or denies. On grant: `expires_at` = time of the decision + granted seconds, `renewals` + 1,
answer `{"status": "ok", "lease": {"id", "expires_at", "renewals"}}` and event `lease.renewed`. Denied or
unanswered: the lease runs to its current `expires_at` (plus `renew_grace_s` while the renewal is still waiting).

While a fix review of this lease runs (3.9) the lease does not expire; when the review ends, `expires_at` is at
least `renew_margin_s` away.

```json
{"id": "applet-rollout-1.60", "op": "applet.lease.release", "applet": "applet-rollout-1", "token": "...", "v": 1,
 "lease": "L-3fa9c210", "outcome": {"result": "fixed", "commit": "9f2c1ab"}, "force": false}
```

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `lease` | string | yes | | A `granted` / `active` / `renew_requested` lease of this applet. |
| `outcome` | JSON object | no | `{}` | Recorded in the lease and the audit. <= 4000 characters as JSON. |
| `force` | bool | no | `false` | `false`: refused with `message_in_flight` while a message of this lease is unanswered. `true`: the running turn is stopped as in section 4.4. |

Release needs no supervisor decision. Answer `{"status": "ok", "lease": {"id", "state": "released"}}`.

### 3.5 What the supervisor answers (service decision JSON)

The host supervisor's service decision (design.md section 11, section 29) gains one optional key. A decision carrying it may
have empty `dispatch`, `answer` and `skip`, like one carrying `escalations`.

```json
{"status": "RUNNING", "dispatch": [], "answer": [], "skip": [],
 "applet": [{"id": "applet-rollout-1.42", "decision": "grant", "slot": "fix-1", "ttl_s": 1800, "note": "merge-caused only"}]}
```

| Item answers | `id` is | `decision` | Other keys |
|---|---|---|---|
| a lease request | the op id | `grant` / `deny` | `grant`: `slot` (required when the request offered free or asleep workers; omitted when the grant adds a worker), `ttl_s`, `paths` (narrow only), `note`. `deny`: `note` (required). |
| a renewal | the op id | `grant` / `deny` | `grant`: `ttl_s` (<= `need_s`), `note`. `deny`: `note`. |
| an escalation | the op id | `approve` / `revise` / `reject` | `choice`: index into the escalation's `options` (required for `approve` when `options` was given; the program refuses an index out of range). `note` (required for `revise` and `reject`). |
| nothing (supervisor's own initiative) | a lease id | `revoke` | `note` (required). |
| nothing (supervisor's own initiative) | an applet name | `pause` / `resume` | `note`. A paused applet's lease requests are refused with `applet_paused`; the applet receives `control`. |

A supervisor **dispatch to a leased or reviewing slot** is refused through the usual correction path, as is a
`fresh` for it. The supervisor is not asked about fix reviews. The pi supervisor's service decision schema
allows the optional `applet` key in the same way it allows `subfleet` (design.md section 29).

### 3.6 Events to the applet: `fx:applet:<name>:events`

Each stream entry has one field `data` holding a JSON object with `type`, `at` (Unix seconds) and the keys below.

| `type` | Keys | Sent when |
|---|---|---|
| `lease.active` | `lease`, `slot`, `label`, `expires_at` | The leased worker is ready; the term starts. |
| `lease.expiring` | `lease`, `expires_at`, `in_s` | `renew_margin_s` before expiry (once per term). |
| `lease.renewed` | `lease`, `expires_at`, `renewals` | A renewal was granted. |
| `lease.renew_denied` | `lease`, `by`, `reason`, `note` | A renewal was denied or timed out. |
| `lease.ended` | `lease`, `state` (`released` / `expired` / `revoked`), `end_reason`, `note` | The lease is over. After this, messages for it are refused. |
| `review.started` | `lease`, `id` (the message), `review`, `round` | The worker replied `ok` to a `fix` message; the result is withheld until the review ends. |
| `worker.rotated` | `lease`, `slot`, `from`, `to` (labels), `why` (`handoff` / `midturn` / `frozen` / `link` / `wall`) | The leased slot started a new generation. The lease continues. |
| `worker.model_changed` | `lease`, `slot`, `cli`, `model` | The slot moved down its model chain (design.md section 23). The lease continues. |
| `message.result` | `lease`, `id`, `status`, `review` (verdict, when the message carried `fix`) | A message of this lease got its final result (also at `fx:result:<id>`). |
| `escalation.ruled` | `id`, `decision`, `choice` (index into `options`, or null), `by` (`supervisor` / `root` / `human`), `note` | An escalation was answered. |
| `control` | `action` (`pause` / `resume`), `by`, `note` | The supervisor or an operator paused or resumed the applet. |
| `host.draining` | `reason` (`stop`, `down`, `rotate`, `detach`), `deadline_at` | The host is going down or the applet is being detached. Checkpoint, release leases, expect SIGTERM. |
| `host.paused` / `host.resumed` | `reason` | The host fleet stopped or resumed delivering. Lease clocks keep running. |

Read from the id saved in `FLEETMUX_APPLET_STATE`, or from `0-0` after a host restart (the stream is new then).

### 3.7 Escalation, notice, audit

```json
{"id": "applet-rollout-1.71", "op": "applet.escalate", "applet": "applet-rollout-1", "token": "...", "v": 1,
 "class": "plan", "what": "Drop branch feat/x from batch-0007 and ship the other three",
 "why": "feat/x fails tests/test_io.py on its own base; not caused by the merge", "job": "batch-0007",
 "options": ["drop feat/x and continue", "hold the whole batch"], "paths": []}
```

| Field | Type | Required | Allowed | Meaning |
|---|---|---|---|---|
| `class` | string | yes | `plan`, `scope`, `access`, `delete` | `plan`: change of plan / target / order. `scope`: wider lease scope. `access`: writing outside the host workspace, network, installs, credentials. `delete`: removing or moving files or directories. |
| `what`, `why` | string | yes | <= 2000 characters each | The request and its reason. |
| `job` | string | no | | Related job. |
| `options` | list of strings | no | <= 5 | Choices the applet can execute. Give them whenever the applet must act on the answer: an approval then names one by index (`choice`), which is the only part of a ruling a program should branch on. `note` is for people and logs. |
| `paths` | list of absolute paths | when `class` is `access` or `delete` | | What would be written, removed or moved. |

| `class` | Who rules | Path |
|---|---|---|
| `plan`, `scope` | host supervisor | `[applet escalation] ...` -> decision key `applet` (3.5). |
| `access` | root supervisor | Sub-fleet host: the host supervisor forwards it with the existing `report_up` (design.md section 46); the root's ruling comes back down. It cannot approve it itself. Root host: the host supervisor is the root supervisor and rules on it in its decision. Either way the applet gets `escalation.ruled` with `by: root`. |
| `delete` | the human owner only | Never shown to a supervisor for approval, on any host. Recorded as `awaiting_human`, pushed on the root fleet's channels (design.md section 53, push kind `escalation`) and shown in `status`. Answered only by `fleetmux applet answer <op id> approve\|reject` (section 5) or the channel's two-step approval. |

Answer: `ok` with `decision` (`approve` / `revise`), `choice` and `note`, or `rejected` with `by` and `note`. `revise` means "not as asked": the applet does nothing on this escalation and may send a new one. The applet executes
only what an `approve` covers. An escalation stays open with no deadline unless the op carries `wait_s`.

`applet.notify` - `{"text": "<= 1000 characters", "job": "..."}`: queues one line `[applet <name>] <text>` for the
host supervisor (information only, no answer expected). Over `notify.max_per_hour` -> `rejected`, `rate_limited`.

`applet.audit` - `{"event": "batch.bounced", "detail": {...}}`: writes one audit record `applet.event` with
`applet`, `event` (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`) and `detail` (<= 4000 characters as JSON). Costs no model
turn. Use it freely; use `notify` sparingly.

### 3.8 Messages to the leased worker, and its replies

A message is an *existing* service request with `to` (design.md section 53 "Direct to a worker", contracts.md section 8) plus four
fields:

```json
{"id": "applet-rollout-1.43", "to": "fix-1", "lease": "L-3fa9c210", "applet": "applet-rollout-1", "token": "...",
 "instruction": "Fix only what the merge broke. Do not touch files outside src/config.py and tests/.",
 "payload": {"conflict_files": ["src/config.py"], "failing": ["tests/test_config.py::test_load"], "log_tail": "..."},
 "reply": {"required": ["status", "summary"], "allowed": ["status", "summary", "commit", "blocked"]},
 "timeout_s": 1200, "replay": true,
 "fix": {"dir": "/srv/rollout/work/batch-0007", "base": "4c1d9e0", "head": null, "files": ["src/config.py"]}}
```

| Field | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `id`, `payload`, `reply`, `instruction`, `timeout_s`, `replay` | | *existing* | | As for any service request: `payload` <= 200 000 characters as JSON; `reply.required` non-empty; `timeout_s` 10 ... 86400 (none = no deadline, design.md section 43); `replay: false` = never re-run after a resume. |
| `to` | string | yes | | Must be the lease's `slot`. |
| `lease` | string | yes | | A `granted` or `active` lease of this applet. |
| `applet`, `token` | string | yes | | As in ops. |
| `fix` | map | no | none | Marks this message as the one whose reply **hands back a fix**. With it, an `ok` reply is reviewed by the host before the result is published (3.9). Without it the reply is plain work or information and is never reviewed. Keys: `dir` (required; absolute path inside the lease's `paths` or the worker's directory), `base` (required; the revision the change starts from), `head` (a revision, or `null` = the state of `dir` when the worker replies), `files` (optional list, relative to `dir`, to narrow the scope). |

Refusals (`rejected`, `by: program`): `bad_token`, `lease_not_active` (ended or unknown), `wrong_slot`,
`host_paused`, `invalid` (a `fix` whose `dir` is outside the lease), `review_cap` (a `fix` for a job that used up
`review.max_rounds`). A service request with `to` naming a leased slot that carries no matching `lease` is refused with
`slot_leased`, whoever sends it.

Delivery order is first in, first out per slot; one message runs at a time. All messages of one lease go to the
same session, so the worker keeps its context between them (clean-session-per-result is suspended for a leased
slot and applied when the lease ends).

Result at `fx:result:<id>` (*existing* envelope, plus `lease`, and `review` for a `fix` message, 3.9):

| `status` | Meaning | What the applet should do |
|---|---|---|
| `ok` | `result` holds the worker's JSON with the required keys. For a `fix` message, `review` holds the verdict. | Verify by its own checks; a worker saying "fixed" proves nothing. Use a fix only when its `review.verdict` is `pass`. |
| `blocked` | The worker says it cannot proceed without a decision (`result.blocked` = one sentence). Not sent to the supervisor automatically. | Escalate (`applet.escalate`) or release. Never answer the question yourself by widening scope. |
| `invalid` | The reply did not satisfy `reply` after the correction rounds. | Resend or release. |
| `timeout` | `timeout_s` passed. The slot is held (design.md section 43) and the lease is revoked (`slot_held`). | Request a new lease. |
| `stalled` | The watchdog judged the turn dead (design.md section 24). If the slot could be rotated the lease continues (`worker.rotated` follows); if the stop could not be confirmed the slot is held and the lease is revoked (`slot_held`, `lease.ended` follows). | Wait for the event that follows; resend under the same lease, or request a new one. |
| `lease_ended` | The lease ended while this message was queued or running. (A message whose worker already replied is not affected: its review finishes and the result is published.) | Request a new lease if work remains. |

### 3.9 Fix review (run by the host)

The applet's part is the `fix` field. Everything else is done by the host daemon; no supervisor turn is spent and
no op exists for it.

| Step | What the host does |
|---|---|
| 1 | The worker replies `ok` to a `fix` message. The result is withheld; event `review.started`; the lease stops expiring. |
| 2 | Pick a reviewer: any worker of the host that is not the fixer's slot and never held a lease of this applet with the same `job`. Free slot first, else an asleep one, else a new row (the order and limits of 3.3; at the limit the review waits for a slot). A different slot is required; a different model is not. Round 2 and later prefer the previous reviewer. |
| 3 | Clean session in the reviewer slot; mark it `applet_review`. |
| 4 | Send one program-written message: `[Fix review R-5b1e22c0, applet-rollout-1, job batch-0007, round 1]`, the scope (`dir`, `base`, `head`, `files`), the open findings of the previous round, "follow REVIEWER.md in your directory", and the reply shape. The reviewer gets no write grant. |
| 5 | Read the reply: `verdict` (`PASS` / `FAIL`) and `findings`. PASS = zero P0 and zero P1; a PASS that lists a P0 or P1 counts as FAIL. |
| 6 | Publish the withheld result with `review` attached; clear `applet_review`; clean session for the reviewer slot; the lease's end is moved to at least `renew_margin_s` from now. |

```json
{"id": "applet-rollout-1.47", "status": "ok", "lease": "L-3fa9c210",
 "result": {"status": "done", "summary": "resolved the conflict in src/config.py", "commit": "9f2c1ab"},
 "review": {"id": "R-5b1e22c0", "verdict": "fail", "round": 1, "reviewer": "fix-2.1", "capped": false, "reason": null,
            "findings": [{"level": "P1", "file": "src/config.py", "line": 42,
                          "what": "after the merge load() ignores the override file, so a deployed override is lost",
                          "repro": "python3 -m pytest tests/test_config.py::test_override -q"}],
            "report": "/abs/path/to/review-R-5b1e22c0.md"}}
```

| `review` key | Type | Meaning |
|---|---|---|
| `id` | string | Review id. |
| `verdict` | `pass` / `fail` / `none` | `pass`: the fix may be used (after the applet's own check). `fail`: see `findings`. `none`: no verdict could be had; see `reason`. |
| `round` | int | 1 for the first review of the job; +1 per FAIL. |
| `reviewer` | string | Label of the generation that reviewed. Information only. |
| `findings` | list | For `fail`: objects with `level` (`P0` / `P1`), `file`, `line`, `what`, `repro` (a command or test id). Empty for `pass`. |
| `report` | string | Path of the reviewer's report file, for people. |
| `capped` | bool | `true` on the FAIL that used up `review.max_rounds`. Further `fix` messages of the job are refused `review_cap`. |
| `reason` | string or null | For `none`: `reviewer_timeout`, `reviewer_invalid`, `no_reviewer` (no slot within `review.timeout_s`). Not a round; the applet may hand back again. |

The message's `timeout_s` covers the leased worker's turn only; the review is bounded by `review.timeout_s`.

What the applet does with it: `pass` -> go on; `fail` -> send the findings to the fixer under the same lease, run
its own check, send a new `fix` message; `capped` or repeated `none` -> stop the job (bounce it or `applet.escalate`).

## 4. What the leased worker sees

### 4.1 Rule file section

While a lease is `granted` or `active`, the program appends this section to the worker's rule file (the same place
and mechanism as program grants, design.md section 46) and removes it when the lease ends:

```
## Applet lease L-3fa9c210 (written by the fleet program)
Until this section disappears you work only for applet-rollout-1.
Purpose: <purpose>
You may also write inside: <paths>
- Messages that start with "[Applet lease L-3fa9c210" are your instructions for this lease.
- Do exactly what each message asks, inside the purpose above. Anything outside it: reply with "blocked".
- You do not decide whether the work passed; the applet checks it.
- Never delete or move files. If that seems necessary, reply with "blocked" and say what and why.
```

### 4.2 Message framing

Every delivery starts with a line the program writes (a supervisor or another sender cannot forge the start of a
delivery, design.md section 46):

```
[Applet lease L-3fa9c210 | applet-rollout-1 | message 3 | term ends 14:32]
[Request applet-rollout-1.43]
<instruction>
Payload (JSON): ...
Reply with one JSON object: required keys status, summary; allowed keys status, summary, commit, blocked.
```

### 4.3 How it replies

Exactly as any service worker (the `fleet_result` tool, or the reply JSON on CLIs without it; *existing*). To say it
is stuck it returns `{"blocked": "<one sentence>"}` as usual; under a lease that comes back to the applet
(`status: blocked`), not to the supervisor. A split proposal (`escalate` / `proposal`, design.md section 44) from a leased
worker is not started; it is returned to the applet as `blocked` with the proposal attached.

### 4.4 How it knows the lease ended

| Situation at the end | What the worker experiences |
|---|---|
| Idle | Nothing is sent (no wasted turn). The lease section and the path grants are removed; the slot's next work starts in a clean session (design.md section 37), so nothing from the lease carries over. |
| A turn is running (expiry, revoke, forced release) | One Escape (the fleet's only accepted stop, design.md section 17). The unanswered message is published `lease_ended`. Then as the idle row. If the stop cannot be confirmed, the slot follows the existing hold rules. |
| The slot rotated during the lease | The handoff note carries a "Lease" block (holder, id, purpose, paths, term end, the message in flight); the new generation's start prompt repeats it; the unanswered message is delivered again under the existing replay rules. |

## 5. Commands

All take `--config <host config.json>` (the applet's `FLEETMUX_APPLET_CONFIG`); without it they act on the
current run of the fleet home, which is the host when the host is a root fleet. Output is one JSON line; exit code
`0` ok, `2` rejected, `3` pending (the same convention as `fleetmux operator`).

| Command | Purpose |
|---|---|
| `fleetmux applet list` | Applets of this host: name, state, pid, heartbeat age, restarts, open leases, `phase`. |
| `fleetmux applet status <name>` | The hash `fx:applet:<name>` + its leases + open escalations. |
| `fleetmux applet start <name>` / `stop <name>` / `restart <name>` | Operator control of the process of a declared row. `start` clears `crashloop` / `config_error`. Attach and detach are roster edits (section 1), not commands. |
| `fleetmux applet pause <name>` / `resume <name>` | Sets `paused`; emits `control`. |
| `fleetmux applet leases [--all]` | Open (or all kept) lease records. |
| `fleetmux applet revoke <lease id> --note <text>` | Operator revoke. |
| `fleetmux applet send <name> <file or ->` | Append one JSON item to the applet's inbox. |
| `fleetmux applet param <name> <key> <json>` | Change one hot parameter of the applet's own block. |
| `fleetmux applet set <name> hot_add_max <n>` | Change the worker ceiling while the fleet runs; written back to the roster row. |
| `fleetmux applet reviews [--all]` | Running (or all kept) fix reviews: id, job, lease, fixer, reviewer, round, verdict. |
| `fleetmux applet answer <op id> approve\|reject [--choice <n>] [--note <text>]` | The human owner's answer to a `delete` escalation. Agents' rule files forbid running it. |
| `fleetmux status` (*existing*) | The tree gains, per fleet that hosts applets, `applets: [{name, state, phase, leases}]`; each slot gains `applet_lease: {id, holder, until}`, `applet_review` and `applet_added_for` when set. |
| `fleetmux logs` (*existing* reader) | Shows the audit events of section 6 with the rest of the host's audit. |

## 6. Audit events

| Event | Fields |
|---|---|
| `applet.declared` / `applet.up` / `applet.exited` / `applet.stopped` / `applet.detached` / `applet.adopted` / `applet.attach_wait` | `applet`, `pid`, `code`, `reason` |
| `applet.stalled` / `applet.restarted` / `applet.crashloop` / `applet.config_error` | `applet`, `reason`, `crashes`, `window_s` |
| `applet.lease.requested` / `.denied` / `.granted` / `.active` | `applet`, `lease`, `job`, `slot`, `added`, `ttl_s`, `by`, `reason` |
| `applet.lease.renew_requested` / `.renewed` / `.renew_denied` | `applet`, `lease`, `need_s`, `ttl_s`, `renewals`, `by` |
| `applet.lease.released` / `.expired` / `.revoked` | `applet`, `lease`, `slot`, `end_reason`, `held_s`, `messages`, `last_review` |
| `applet.worker_added` | `applet`, `slot`, `like`, `for` (lease or review id), `count`, `hot_add_max` |
| `applet.review.started` / `.passed` / `.failed` / `.unavailable` | `applet`, `review`, `job`, `lease`, `message`, `fixer`, `reviewer`, `round`, `findings` (count), `capped`, `reason` |
| `applet.message.sent` / `applet.message.result` | `applet`, `lease`, `id`, `status` |
| `applet.escalated` / `applet.ruled` | `applet`, `id`, `class`, `decision`, `by` |
| `applet.notified` / `applet.param_changed` / `applet.paused` / `applet.resumed` | `applet`, ... |
| `applet.event` | `applet`, `event`, `detail` (the applet's own events, e.g. `batch.bounced`, `batch.merged`) |

## 7. Python SDK surface (`fleetmux.applet`)

Signatures and docstrings only. The SDK is a thin layer over section 3; an applet in another language implements
section 3 directly.

```python
class Applet:
    """One applet process. Reads the FLEETMUX_APPLET_* environment, connects to the host Redis,
    starts the heartbeat thread and handles SIGTERM (sets `stopping`)."""

    name: str
    state_dir: "pathlib.Path"      # FLEETMUX_APPLET_STATE (durable)
    run_dir: "pathlib.Path"        # FLEETMUX_APPLET_RUN (per run)
    stopping: "threading.Event"    # set on SIGTERM or host.draining

    @classmethod
    def from_env(cls) -> "Applet":
        """Build from the environment. Exits 78 when a variable is missing or the interface version differs."""

    def params(self) -> "Params":
        """The applet's parameter block (section 2.5)."""

    def set_phase(self, text: str) -> None:
        """Short free text shown in `fleetmux status` (what the applet is doing now)."""

    def request_lease(self, *, job: str, purpose: str, ttl_s: "float | None" = None,
                      paths: "list[str]" = (), want: "dict | None" = None,
                      context: "dict | None" = None, wait_s: "float | None" = None) -> "Lease":
        """Ask the supervisor for a worker. Blocks until granted; if no worker is free the host wakes or
        adds one. Raises LeaseDenied (with .by, .reason, .note, .retry_after_s) when refused or not
        decided in time."""

    def leases(self) -> "list[Lease]":
        """Open leases of this applet as the host records them (use after a restart to re-attach)."""

    def escalate(self, *, cls: str, what: str, why: str, job: "str | None" = None,
                 options: "list[str]" = (), paths: "list[str]" = (),
                 wait_s: "float | None" = None) -> "Ruling":
        """Hand a decision to the supervisor (or, for cls='delete', the human owner). Blocks until ruled."""

    def notify(self, text: str, *, job: "str | None" = None) -> None:
        """One information line to the supervisor. Rate limited; costs a model turn."""

    def audit(self, event: str, **detail) -> None:
        """One audit record (`applet.event`). Free; use for every state change of the applet's own work."""

    def events(self, *, block_s: float = 5.0) -> "Iterator[Event]":
        """Host events in order (section 3.6). Remembers its position in state_dir."""

    def inbox(self, *, block_s: float = 5.0) -> "Iterator[dict]":
        """Items sent with `fleetmux applet send`. Persist an item before acting on it."""

    def run(self, step: "Callable[[Applet], float | None]") -> "NoReturn":
        """Main loop helper: call `step` until `stopping`; `step` returns seconds to sleep.
        Exits 0 on stop; an uncaught exception exits 1 (a crash)."""


class Lease:
    """An applet lease on one worker slot."""

    id: str
    slot: str
    job: str
    expires_at: "float | None"

    def send(self, payload: dict, *, instruction: str, required: "list[str]",
             allowed: "list[str] | None" = None, timeout_s: "float | None" = None,
             replay: bool = True, fix: "dict | None" = None) -> "Reply":
        """Send one message to the leased worker and wait for its result.
        fix={"dir", "base", "head", "files"} marks the message that hands back a fix: the host then has
        another slot review it and the reply carries .review. Raises LeaseEnded when the lease is over."""

    def renew(self, *, need_s: float, progress: str, why: "str | None" = None) -> bool:
        """Ask for more time. True when granted (expires_at updated); False when denied or unanswered."""

    def release(self, outcome: "dict | None" = None, *, force: bool = False) -> None:
        """Give the worker back. Call as soon as the job is done; a later job requests a new lease."""

    def remaining_s(self) -> float:
        """Seconds until expiry by the local clock."""

    def __enter__(self) -> "Lease": ...
    def __exit__(self, *exc) -> None:
        """Releases the lease (force=True when leaving on an exception)."""


class Reply:
    status: str            # ok / blocked / invalid / timeout / stalled / lease_ended
    result: "dict | None"  # the worker's JSON for ok / blocked
    worker: str            # label of the generation that answered, e.g. fix-1.2
    elapsed_s: float
    review: "Review | None"  # set when the message carried fix= and the worker replied ok


class Review:
    verdict: str           # pass / fail / none
    round: int
    findings: "list[dict]" # level, file, line, what, repro
    capped: bool           # True: the job used up review.max_rounds
    reason: "str | None"   # for none: reviewer_timeout / reviewer_invalid / no_reviewer
    report: "str | None"


class Ruling:
    decision: str          # approve / revise / reject
    choice: "int | None"   # index into the options the applet gave; branch on this, not on note
    by: str                # supervisor / root / human
    note: str


class Params:
    def get(self, key: str, default=None): """Current value (hot keys read Redis on every call)."""
    def set(self, key: str, value) -> None: """Change a hot key; written back to the YAML on the timer."""
    def flush(self) -> None: """Write changed hot keys back to the YAML now."""


class LeaseDenied(Exception): ...     # .by .reason .note .retry_after_s
class LeaseEnded(Exception): ...      # .state .end_reason
class Rejected(Exception): ...        # any other refused op: .by .reason .note
```

## 8. Minimal applet skeleton

```python
"""applet-example-1: when a job arrives, have a leased worker do it and hand the fix back; the fleet reviews it."""
import json
from fleetmux.applet import Applet, LeaseDenied, LeaseEnded


def check(job) -> bool:
    """The applet's own deterministic verdict (run tests, compare hashes, ...)."""
    ...


def handle(app: Applet, job: dict) -> bool:
    """True when the job is finished (done or bounced); False to try again on a later step."""
    p = app.params()
    try:
        with app.request_lease(job=job["id"], purpose=job["purpose"],
                               ttl_s=p.get("fix_ttl_s", 1800), paths=[job["dir"]]) as lease:
            note = "Do the job described in the payload."
            for attempt in range(p.get("max_fix_attempts", 3)):
                reply = lease.send({"job": job}, instruction=note,
                                   required=["status", "summary"], allowed=["status", "summary", "blocked"],
                                   timeout_s=p.get("fix_timeout_s", 1200))
                if reply.status == "blocked":
                    ruling = app.escalate(cls="scope", what=reply.result["blocked"], why="worker is blocked",
                                          job=job["id"])
                    if ruling.decision != "approve":
                        break
                    continue
                if lease.remaining_s() < p.get("renew_below_s", 300):
                    lease.renew(need_s=900, progress=f"attempt {attempt + 1}")
                if reply.status != "ok" or not check(job):        # the applet's own verdict comes first
                    note = "The check still fails. Payload has the details."
                    continue
                back = lease.send({"job": job}, instruction="State what you changed, file by file.",
                                  required=["summary"], fix={"dir": job["dir"], "base": job["base"], "head": None})
                review = back.review
                if review and review.verdict == "pass":
                    app.audit("job.done", job=job["id"], review_round=review.round)
                    return True
                if review is None or review.capped:
                    break
                if review.verdict == "fail":                      # next round: findings go to the same fixer
                    job = dict(job, findings=review.findings)
                    note = "A reviewer found the problems listed in payload.findings. Fix them."
            app.audit("job.bounced", job=job["id"])
            return True
    except LeaseDenied as denied:
        app.audit("job.deferred", job=job["id"], reason=denied.reason)   # e.g. worker_limit: retry later
    except LeaseEnded as ended:
        app.audit("job.interrupted", job=job["id"], reason=ended.end_reason)
    return False


def pending(app: Applet) -> list:
    """Jobs accepted but not finished: queue.jsonl minus done.jsonl (both under the durable state directory)."""
    def ids(name):
        path = app.state_dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    done = {row["id"] for row in ids("done.jsonl")}
    return [job for job in ids("queue.jsonl") if job["id"] not in done]


def finish(app: Applet, job: dict) -> None:
    with (app.state_dir / "done.jsonl").open("a") as f:
        f.write(json.dumps({"id": job["id"]}) + "\n")


def step(app: Applet):
    for item in app.inbox(block_s=5):                  # new work: persist before acting
        with (app.state_dir / "queue.jsonl").open("a") as f:
            f.write(json.dumps(item) + "\n")
    for job in pending(app):                           # also everything a crash or a restart left unfinished
        if app.stopping.is_set():
            break
        if handle(app, job):                           # must be safe to run again for the same job
            finish(app, job)
    return 0


if __name__ == "__main__":
    Applet.from_env().run(step)
```

## 9. Stable rejection reasons

`bad_token`, `bad_version`, `applet_not_up`, `applet_paused`, `applet_detaching`, `host_paused`, `host_draining`,
`max_active`, `max_renewals`, `renew_pending`, `worker_limit` (the worker ceiling or a fleet limit is reached; never
sent only because workers are busy), `review_cap`, `invalid`, `lease_not_active`, `wrong_slot`, `slot_leased`,
`message_in_flight`, `rate_limited`, `duplicate`, `no_decision` (supervisor did not answer in time),
`supervisor` (the supervisor's own refusal; read `note`).

## 10. Compatibility promises

| Promise | Detail |
|---|---|
| Version | `FLEETMUX_APPLET_INTERFACE` and `v` are integers. Version 1 = this page. |
| Additive within a version | New optional op fields, new event types, new record fields, new audit events, new result statuses and new rejection reasons may appear. An applet must ignore unknown event types and fields and treat an unknown status or reason as a refusal. |
| Never within a version | Renaming or removing a key, field, op, exit code meaning or environment variable; changing a default in section 1; making an optional field required. |
| A new version | The host accepts both the old and the new version for at least one release line; an entry's `interface:` says which one the applet speaks. |
| Not part of the contract | The internal bus stream and Lua scripts, the layout of `config.json`, the wording of supervisor, worker and reviewer prompts (only the first-line markers `[Applet lease ...`, `[Fix review ...` and `## Applet lease ...` are stable), terminal tab placement, which slot reviews. |
| Changes to this page | Follow contracts.md: add, do not change; a change of meaning needs a transition period and the commit's first line starts with `contract`. |
