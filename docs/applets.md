# fleetmux applets (design)

Status: **proposed design, not implemented.** Key names, field tables, commands and signatures are in
[applet-interface.md](applet-interface.md) and are not repeated here; this page explains the model, the state
machines, who decides what, and what happens when things fail. How to build and deploy a whole team is in
[applet-team-guide.md](applet-team-guide.md). Section numbers such as "section 29" refer to [design.md](design.md).

## 1. What an applet is

A fleet is one supervisor, N workers, one daemon and one private Redis (section 1). That is enough for work a supervisor
can plan in its head. Some teams also have a **fixed, mechanical core**: a release train merges branches, runs a
test gate, installs, checks health and rolls back; a nightly scanner walks a list; an ingest loop polls a queue.
That core must be deterministic code, not a model following a prompt, but it still needs a model now and then
(resolve this merge conflict, fix this small break).

An **applet** is that deterministic core, run as a process of a fleet next to its daemon:

- It carries the team's custom function. The fleet around it (supervisor + workers) is the team.
- It is **hot-loadable**: it is attached to a running fleet, detached from it, or moved to another fleet without
  restarting any fleet and without touching workers that hold none of its leases.
- Its **host** is chosen by the owner: a root fleet or any named sub-fleet (section 29).
- It **never calls a model itself**: no model API, no agent CLI, no one-shot prompt runner.
- When it needs model work it asks the host's supervisor for an **applet lease**: one worker slot, for a
  limited time and a stated scope, exclusively its own. During the term it talks to that worker directly.
- It gives the worker back as soon as the job is done and applies again next time. If the job is not finished
  when the term runs out, it asks for a renewal; the supervisor decides.
- Its verdicts are its own: whether a fix worked is decided by the applet's checks, never by the worker's claim.
- **Independent review is not its job.** When a leased worker hands back a fix, the fleet has a worker in another
  slot review it and gives the applet one final result (section 6).

Name format: `applet-<name>-<n>`, for example `applet-rollout-1`. (Not "kit": a worker kit, section 30, is a read-only
data package. Not a `tabs:` program, section 31: a tab is opened once and never watched.)

### 1.1 Roles

| Role | Does | Does not |
|---|---|---|
| Host supervisor | Acts at two moments: grants a lease (which worker, what scope, how long) and takes it back (deny a renewal, revoke). Rules on what the applet escalates. | Take part in the back-and-forth inside a lease. Dispatch other work to a leased or reviewing slot. Pick or instruct the reviewer of a fix. |
| Applet | Runs the deterministic loop; keeps every threshold as a parameter; judges results by program; requests, renews and releases leases; marks the message that hands back a fix; escalates anything outside its scope. | Call a model. Widen its own scope. Approve its own escalations. Delete or move files on its own decision. Arrange reviews. |
| Leased worker | Works inside the lease's stated purpose and paths for the applet that holds the lease. | Decide whether the work passed. Take work from anyone else during the lease. Do out-of-scope work (it replies `blocked`). |
| Reviewer | A worker in another slot, picked by the host daemon. Reviews one handed-back fix, read-only, by the fleet's review rules (`REVIEWER.md`). | Edit files. Fix anything. Review a job it held a lease for. |
| Root supervisor | Rules on `access` escalations (section 46). For a sub-fleet host they are passed up to it; on a root host it is the host supervisor. | Approve deletions. |
| Human owner | Chooses the host. The only one who approves deletions and moves of files. | |
| Host daemon | Starts, watches and restarts the host's applets; enforces every rule below; adds a worker when a lease needs one; runs fix reviews; delivers messages; records audit. | Trust a model's or an applet's claim without checking state. |

### 1.2 What is reused and what is new

| Need | Existing mechanism reused | New |
|---|---|---|
| A place for the team | A root service fleet, or a named service sub-fleet (section 29) | Roster block `applets:` |
| Attach / detach while running | Roster hot reload: detection, whole-file validation, add and remove (section 16) | Applied to `applets:` rows; the detach sequence |
| Start / watch / restart a process | The daemon's handling of its channel actor: spawn, heartbeat, restart (section 53) | Restart rule of section 2; the applet survives a daemon outage and is adopted on resume |
| Applet -> fleet requests | Service request entry and the external-op pattern of `subfleet.up` (program gate first, then a supervisor decision field, op queue re-asked on resume; section 29) | Ops `applet.*`; decision key `applet` |
| Applet -> worker messages | Service request with `to`: dispatched straight to that worker, reply by `fleet_result`, result at `fx:result:<id>` (section 53 "Direct to a worker") | The `lease` and `fix` fields and their checks |
| "This slot is taken" | Slot hash `fx:agent:<slot>`; the scheduler's existing guards for `delegated` / `held` slots | Fields `applet_lease*` and `applet_review` on the slot; the guards read them |
| A worker when none is free | Wake of a reclaimed worker; roster hot add of a worker row (section 16); model from the default worker chain (section 52) | The daemon writes the row for a granted lease or a review; `hot_add_max` |
| Giving such a worker up again | Idle reclaim of service workers (`worker_idle_reclaim_min`) | Also applied to lease-added slots of a sub-fleet host (today the rule runs in root fleets only) |
| Independent review of a fix | `REVIEWER.md` in every worker directory and its pointer in the rule file; verdict rule PASS = zero P0 and zero P1; a capped number of rounds (the `review_step` of the `dev_task` lifecycle mode) | The daemon dispatches the reviewer in a service fleet and attaches the verdict to the message result |
| Clean context at lease start and end | `fresh` rotation (section 37) | Triggered by lease start / end and by a review |
| Extra write paths for the lease | Program grants written into the worker's rule file (section 46) | Issued and cleared by the lease |
| Surviving rotation | Handoff note + next generation in the same slot (section 4, section 34), frozen resume (section 43), model chain on a wall (section 23) | A "Lease" block in the note and the start prompt |
| Out-of-scope requests | `report_up` to the root supervisor and its ruling (section 46) | Op `applet.escalate` in front of it |
| Deletions | Human-only rule; channel push and two-step approval (section 53) | A pending item for an applet's `delete` escalation |
| Audit, status | Audit stream, `logs`, `status` tree | Event names `applet.*`; applet rows in the tree |

Nothing else is added: no new port, no second Redis, no system service.

### 1.3 Where an applet may be hosted

| Host | Allowed | Notes |
|---|---|---|
| Root fleet started with `--lifecycle service` | yes | The simplest team: one fleet, one roster. |
| Named sub-fleet with `mode: service` | yes | Several teams under one root, or a team next to other work of the root. |
| Root fleet in a task lifecycle (`dev_task`, `scheduled`, ...) or a named sub-fleet with `mode: task` | no | The one remaining limit. Ops and lease messages are service requests (section 7 of design.md, contracts.md section 4); a task fleet has no request entry and ends with its task. A roster with `applets:` is refused there. |
| Split sub-fleet `M<n>` (section 44) | no | It has no roster of its own and ends with its task. |

The transport must be v2, as for every service feature.

## 2. Applet lifecycle

States of `fx:applet:<name>`:

```
declared --start--> starting --first heartbeat--> up --SIGTERM--> stopping --exit--> exited
                        |                          | |
                        | exit 78                  | +- heartbeat stale -> stalled -> (kill, restart)
                        v                          |
                  config_error                     +- crash -> starting (at once) ... third in an hour -> crashloop
```

| Event | Rule | Decided by |
|---|---|---|
| Attach | A row added to the host roster's `applets:` (hot, section 16), or present when the host starts. The host must be allowed by 1.3. | Whoever edits the roster (the roster is the authorization, as for `tabs:` section 31) |
| Start | After the host supervisor is READY. In its own tab after the daemon's tab. Refused while another host under the same fleet home still runs an applet of that name (2.2). | Program |
| Stop | SIGTERM, grace, SIGKILL. Triggered by a detach, a host stop or drain, `fleetmux applet stop`. | Program / operator |
| Detach | The row is removed from the roster. Sequence in 2.1. | Whoever edits the roster |
| Resume | The applet restarts with the host and finds its durable state directory. It must be written so that a restart at any point continues or safely redoes the current step. | Applet author |
| Crash | Any exit other than 0 / 75 / 78. Open leases are ended (`applet_down`), so a dead applet never keeps a worker. Restarted at once. | Program |
| Stall | Process alive, heartbeat older than `stale_after_s`. Treated as a crash after SIGTERM / SIGKILL. | Program |
| Crashloop | Third crash or stall within an hour: no more restarts; one line to the host supervisor. Cleared by `fleetmux applet start`. The host itself stays up. | Program; operator clears |
| Host daemon down | 2.3. The applet keeps running, unwatched, and is adopted when the daemon is back. | Program |
| Rotation of a leased worker | Section 5. The lease stays with the slot. | Program |
| Host goes down | A root host: `fleetmux stop`. A sub-fleet host: planned down, removed sub-fleet row, parent stop, or the periodic rotation of the host's own supervisor (section 29 "rotation gate": drain -> reclaim -> delegate again). The applet gets `host.draining`; the host waits for its leases to be released (a sub-fleet at most its `drain_s`, a root host at most the applet's `detach_wait_s`); then the applet is stopped and remaining leases end with `host_down`. | Program |
| Host comes up again | A sub-fleet host starts with an empty Redis (the previous run is archived); a resumed root host keeps its Redis. In both cases the applet is started again and relies only on its durable state directory. | Program |

### 2.1 Hot load: attach, detach, move

Attach, detach and move are roster edits. No command is added for them: the roster is already the one place that
says what a fleet runs, it is already hot-reloaded and validated as a whole, and a second entry point would have
to be kept in step with it. `fleetmux applet start / stop` still control the process of a row that stays declared.

| Action | How | What the host daemon does | What is not touched |
|---|---|---|---|
| Attach | Add a row to `applets:` in the running host's roster. | Next tick: validate the whole file; declare the applet; start it if `autostart`. A name that was attached earlier in this run may be attached again. | The fleet is not restarted. No worker is started, stopped or rotated. |
| Detach | Remove the row. | 1. Refuse new lease requests of this applet (`applet_detaching`). 2. Send `host.draining` (`reason: detach`) and wait up to `detach_wait_s` for the applet to release its leases. 3. Revoke leases still open (`applet_detached`); a turn running under one is stopped with one Escape. 4. SIGTERM, `stop_grace_s`, SIGKILL. 5. Close the tab, clear the applet's Redis keys except ended lease records. Putting the row back before step 3 cancels the detach. | Workers without a lease of this applet. The durable state directory (never deleted). |
| Move | Detach from the old host, then attach to the new host. | Each host does its own half. The new host starts the applet only after the old process is gone (2.2). | Leases do not move: they end with the detach and the applet applies again on the new host. Workers of both fleets. |

After a detach the worker that held a lease gets a clean session (section 4, as after any lease).

### 2.2 One applet, one host

Two rosters are reloaded by two daemons, so a move could start the new copy before the old one has stopped. The
guard is an owner record in the durable state directory (`host.json`: host name, run, pid), written by the host
daemon before it starts the process and removed after the process has exited.

| Situation at start | Result |
|---|---|
| No owner record, or its pid is gone | Start; write the record. |
| A live owner record of another host | The row stays `declared`; audit `applet.attach_wait` once; checked again on every tick. |
| A live owner record of this host and run | The applet is already running: adopt it (2.3). |

The durable state directory is `<fleet home>/applets/<name>/`. A root fleet and its sub-fleets share one fleet
home, so a move inside one fleet tree keeps the applet's state. A host with a different fleet home has a
different, empty state directory; carrying state there is the applet project's own step (copy it before the
attach). fleetmux neither copies nor deletes a state directory.

### 2.3 Who watches the applet

The **host's own daemon**, for a root host and for a sub-fleet host alike. An applet runs in its own terminal
tab; it is not a child process of the daemon, so a daemon exit does not take it down.

| Duty | Done by | How |
|---|---|---|
| Start, signal, restart the process; count crashes; decide crashloop | Host daemon | On its tick, per roster row: pid alive, heartbeat age, exit code. |
| End the leases of an applet that is not `up` | Host daemon | On its tick (`applet_down`). |
| Gate ops, ask the supervisor, deliver messages, add workers, run reviews, emit events, expire leases | Host daemon | Sections 3 to 6. |
| Bring the host daemon back | Sub-fleet host: the parent daemon (section 29). Root host: its supervisor or an operator, `start --resume` (section 36). | Unchanged. |
| Carry an `access` escalation up and its ruling down (sub-fleet host) | Host supervisor + both daemons | `report_up` notice files and ruling files (section 46), unchanged. |
| Carry a `delete` escalation to the human | Host daemon; from a sub-fleet host up to the top-level daemon | A root host records the pending item itself, shows it in `status` and pushes it on its channels. A sub-fleet host writes a notice of kind `applet_delete` into the parent's notice directory (the section 20 / section 46 channel; middle levels pass it up unchanged); the top-level daemon does the same recording and push. The human's answer comes back as a ruling file to the originating host, which emits `escalation.ruled` with `by: human`. No supervisor at any level is asked. |

While the host daemon is down:

| Thing | During the outage | After `daemon --resume` |
|---|---|---|
| Applet process | Keeps running and may continue deterministic work. Nobody restarts it if it dies. | Pid alive, heartbeat fresh, owner record of this host: adopted as it is, no restart (`applet.adopted`). Otherwise its leases are ended (`applet_down`) and it is started again; this counts as one crash. |
| Ops submitted | Stay `pending`; no answer. | Processed in order. The applet may resend with the same `id`. Requests that were waiting for the supervisor are asked again (as for `subfleet.up`, section 29). |
| Lease messages | Not delivered. | Resume rules of section 17: a message that was in flight is stopped and delivered again when its `replay` allows, otherwise it is answered `timeout`; a turn the worker finished during the outage is done again. A worker that cannot be reattached starts its next generation (`worker.rotated`); the lease stays with the slot. |
| Lease clocks | Keep running (wall clock). | Leases past their end expire at once. |
| Reviews | Nothing new starts. | A review that was in flight is redone by the same rules; waiting ones start. |
| Host Redis gone as well (the host was stopped or reclaimed without a clean applet stop) | The applet cannot reach Redis; after `stale_after_s` the SDK exits `0`. | The next host start starts the applet again. |

Consequence for authors: **Redis is not the applet's memory.** Job queues, attempt counters and "where was I" go
into `FLEETMUX_APPLET_STATE`. Leases do not carry across a host down/up or a move; the applet applies again.

## 3. The applet lease

### 3.1 State machine

```
            request                 grant                  worker READY
  (none) -----------> requested -----------> granted -----------------> active <--------------+
                          |                      |                       |  |                  | renewed
                          | deny / gate /        | prepare_timeout       |  | renew            |
                          v no decision          v                       |  v                  |
                        denied                revoked                    | renew_requested ----+
                                                                         |      | denied / unanswered
                                             release <-------------------+      v (runs to expires_at)
                                             expired <-- term over ------+
                                             revoked <-- supervisor / operator / host_down / applet_down /
                                                         applet_detached / slot_held / slot_retired
```

| Edge | Who decides | Rule / timeout |
|---|---|---|
| -> `requested` | Applet | Op `applet.lease.request`. |
| `requested` -> `denied` (program) | Program gate | Bad token, applet not up, paused or detaching, host paused or draining, `max_active` reached, invalid shape, or a real worker limit (3.3). No model turn is spent. A busy fleet alone is not a reason. |
| `requested` -> `granted` / `denied` | **Host supervisor** | Picks the slot (or accepts the new worker the program offers, 3.3), the term (<= `ttl_max_s`) and may narrow the paths; or denies with a note. The program re-checks the answer. |
| `requested` -> `denied` (`no_decision`) | Program | No supervisor answer within `decision_wait_s` (default 600 s). |
| `granted` -> `active` | Program | Worker woken or added if needed, clean session started, lease section and grants written, worker READY. The term starts here, so preparation time is not charged to the applet. |
| `granted` -> `revoked` (`prepare_failed`) | Program | Not ready within `prepare_timeout_s` (default 300 s). |
| `active` -> `renew_requested` | Applet | Op `applet.lease.renew` with progress and the time still needed. The host reminds with `lease.expiring` `renew_margin_s` (default 300 s) before the end. |
| `renew_requested` -> `active` (renewed) | **Host supervisor** | Grants the asked time or less. The program denies by itself past `max_renewals`. |
| `renew_requested` -> `active` (denied / unanswered) | Host supervisor / program | The lease keeps its current end. `renew_grace_s` (default 0) is the only extension an unanswered renewal can get. |
| `active` -> `released` | Applet | Op `applet.lease.release`. No supervisor turn. Expected as soon as the job is done. |
| `active` -> `expired` | Program (clock) | Wall-clock `expires_at` passed. Checked on the daemon tick; a daemon outage past the end means expiry on resume. Not enforced while a review of this lease's fix runs (section 6). |
| any open -> `revoked` | Host supervisor (`revoke`), operator (`fleetmux applet revoke`), or program (`host_down`, `applet_down`, `applet_detached`, `slot_held`, `slot_retired`) | Immediate. |

The lease clock is wall-clock and does not stop for a paused fleet, a rotation or a wall. This keeps the rule
simple and bounded; an applet that lost time asks for a renewal and says why.

### 3.2 One job, one lease

A lease covers one job (the `job` field). When the job is done the applet releases; the next job requests a new
lease. A roster never assigns a worker to an applet permanently. Several leases at once are allowed up to
`max_active` (jobs in parallel).

### 3.3 No free worker: wake one or add one

A lease request is not refused because every worker is busy. The program looks for a slot in this order and
tells the supervisor what it found:

| Step | Condition | What happens on a grant |
|---|---|---|
| 1 | An eligible slot is idle, READY, has nothing queued and is neither leased nor reviewing. | The supervisor names it. |
| 2 | An eligible slot is asleep (state `reclaimed` by the idle reclaim). | The supervisor names it; the program wakes it (the existing respawn) and the lease starts when it is READY. |
| 3 | Neither, and the applet has fewer than `hot_add_max` lease-added rows in the roster, and the fleet's own limits allow one more worker. | The supervisor grants without naming a slot. The host daemon writes a new row into the host roster (and the night roster, if any) and section 16 starts it: name = the base of the template worker + the next unused number (`fix-1` -> `fix-2`; names are never reused in a run), `role` = the template's role file, nothing else. CLI and model are the host's defaults for a worker: the first usable model of the default worker chain (`fleet.yaml#discovery.worker`, section 52), moving down the chain on a wall (section 23). |
| 4 | None of the above. | Not asked. `rejected`, reason `worker_limit`, with `retry_after_s`. See 13.1 "Worker limit". |

The template worker is the entry's `hot_add_like` (default: the first eligible worker). A lease-added slot is
eligible for the applet it was added for, in addition to the entry's `eligible` list.

After the lease ends the added slot is an ordinary idle worker. The existing idle reclaim closes its session
after `worker_idle_reclaim_min` idle minutes (state `reclaimed`: no session, no pane, no cost); a later lease
wakes it (step 2) before any new row is added. The row leaves the roster when someone deletes the line
(section 16). In a sub-fleet host the idle reclaim does not run today; applying the same rule there to
lease-added slots is part of this design.

`hot_add_max` counts lease-added rows present in the host roster, awake or asleep, including those added for a
review (section 6). It is a parameter of the applet's roster row and a hot value (applet-interface.md 1.2). The
fleet's own limits still apply and are real limits: `child_workers_max` and `max_agents_total` (section 19) for a
sub-fleet host, `max_agents_total` for a root host, and `max_busy` for delivery.

## 4. Exclusivity and the scheduler

A leased slot is marked by fields on its slot hash (`applet_lease`, `applet_lease_holder`, ...), **not** by a new
value of the slot's `state`. The reason: a leased worker still moves through `idle` -> `busy` -> `idle` for every
message, can be `held`, and goes through `handoff` and `starting` on rotation; every existing branch on `state`
stays correct. "Leased" is an owner mark on top of those states. In displays it reads `leased by applet-rollout-1`.
A slot that reviews a fix carries `applet_review` and is treated the same way for the length of the review; it
reads `reviewing for applet-rollout-1`.

How every existing mechanism treats a slot whose `applet_lease` (or `applet_review`) is set:

| Mechanism | Behavior for a leased slot |
|---|---|
| Supervisor routing board (section 32) | Listed as `leased by <applet> until HH:MM`, not as idle. |
| Supervisor `dispatch` / `fresh` to it | Refused through the usual correction path. |
| Service request with `to` = the slot, without the matching `lease` | Refused `slot_leased` (channel direct messages, section 53, included). |
| `max_busy` and idle counts | Counted as busy for the whole lease, also between messages. |
| Idle reclaim of workers (service lifecycle) | Skipped. |
| Clean session after every result (`fresh_after_result`, section 51) | Suspended; one clean session at lease start and one after its end instead. |
| Long-task split nudge (section 44) and split proposals | No nudge; a proposal is returned to the applet as `blocked`. |
| Worker `blocked` reply | Returned to the applet, not queued for the supervisor. |
| Roster hot reload removing the slot | The slot retires gracefully (section 16); the lease is revoked `slot_retired` first. |
| Night switch / profile change (section 25) | Takes effect at the slot's next generation, which for a leased slot is after the lease ends. |
| Named sub-fleet delegation of that slot | Refused while leased. |
| Peer notices (section 21) | Not delivered during the lease; queued for after. |
| Operator pause of the fleet | Delivery stops for everyone, leased slots included; the lease clock keeps running. |

Only two parties can send a leased worker anything: the applet holding the lease (messages) and the daemon itself
(READY, handoff and mid-turn rotation prompts, which are fleet housekeeping). A reviewing slot receives only the
daemon's review message.

## 5. Rotation, walls and other things that happen to a leased worker

The lease belongs to the **slot**, not to the session. Labels change (`fix-1` -> `fix-1.1`), the slot does not.

| Event | What the fleet does | What the applet sees |
|---|---|---|
| Context threshold between messages (section 4, section 22) | Normal handoff rotation. The handoff note gets a "Lease" block (holder, lease id, purpose, paths, end of term, last message and its result); the new generation's start prompt repeats it and the rule file still has the lease section. | `worker.rotated` (`why: handoff`). Next message goes to the new generation. |
| Mid-turn rotation (section 34) | As today: the turn is split, the continuation goes first to the new generation. | `worker.rotated` (`why: midturn`); the message's result arrives later as usual. The request's timeout clock pauses during the split, as today. |
| Frozen session (section 24, section 43) | Resume the session if possible, else new generation with a skeleton note; the message in flight is redelivered when `replay` allows. | `worker.rotated` (`why: frozen`), or result `stalled`. |
| Link loss (section 35) | Restart path as today. | `worker.rotated` (`why: link`). |
| Usage wall (section 23) | The slot moves to the next model on the worker chain, possibly another CLI; the instruction is redelivered once. | `worker.model_changed`. The applet may release if the new model is not acceptable for the job (a parameter of the applet, not of fleetmux). |
| End of the model chain, budget brake | The fleet pauses as today. | `host.paused`; the lease clock runs; ask for a renewal after `host.resumed` or let it expire. |
| Message `timeout_s` passes (section 43) | The slot is held (only this slot). A held slot has no reliable exit on every CLI, so the lease is revoked `slot_held` rather than left pointing at a dead end. | Result `timeout`, then `lease.ended`. |
| Confirmation dialog in the worker's pane | Unchanged: non-delete dialogs go to the supervisor, delete dialogs to the human (section 24, section 53). | Nothing, or a late result. |

A lease does not survive the host going down (section 2). For a sub-fleet host that includes the periodic
rotation of the host's supervisor, which today is implemented as a down/up of the sub-fleet; `drain_s` gives
running jobs time to finish; see open question 2. A root host has no such rotation: its supervisor rotates in
place and leases continue.

## 6. Independent review of a fix (done by the fleet)

Every fix a leased worker hands back is reviewed by a worker in a different slot. Arranging that is the fleet's
duty. The applet does not request a reviewer, does not talk to one and does not hold a lease on one; it marks
which message hands back a fix and receives one final result.

### 6.1 What the applet does

- It sets `fix` on the lease message whose reply hands the change back (`fix` names the directory, the base
  revision, the head revision or the working tree, and optionally the files; applet-interface.md 3.8). A message
  without `fix` is plain work or information and is never reviewed.
- Two ways to use it. Small fix: the one message that asks for the change carries `fix`. Longer fix: plain
  messages and the applet's own check first, then a last message with `fix` that asks the worker to state what it
  changed. The second way spends a review only on a change that already passes the applet's check.
- The result of that message arrives once, after the review, with a `review` object: `pass`, or `fail` with the
  reviewer's findings, or `none` with a reason when no verdict could be had.
- Rule for authors: **a worker's change is used (merged, installed, shipped) only after the applet's own check
  passed and a `fix` message for it came back `pass`.** A change made after a passed review needs a new hand-back.
- On `fail` the applet sends the findings to the fixer under the same lease (same session), re-runs its own
  check, and hands back again. That is the next round.

### 6.2 What the fleet does

| Step | Rule |
|---|---|
| Trigger | The leased worker's reply to a `fix` message has status `ok`. Other statuses are published at once, without a review. The result is withheld; event `review.started`. |
| Reviewer | Picked by the host daemon, with no supervisor turn (the purpose, the scope and the read-only rule are fixed, so there is nothing to decide). Any worker of the host qualifies except: the slot that made the fix and every slot that held a lease of this applet with the same `job`. Only a different **slot** is required, not a different model. |
| No free reviewer | The same order as 3.3: an idle slot, else an asleep slot, else a new row under `hot_add_max` and the fleet limits. At the limit the review waits for a slot, at most `review.timeout_s`. |
| Session | Clean session (section 37) before the review, so the reviewer has none of the fixer's context, and after it. The slot carries `applet_review` meanwhile (section 4). |
| Instructions | One program-written message: the scope from `fix`, the previous round's findings from round 2 on, and a pointer to `REVIEWER.md` in the worker's directory (the fleet's review rules: read-only, review only the given scope, each P0 / P1 finding with file, line and a reproduction). The reviewer gets no write grant. |
| Verdict | `PASS` = zero P0 and zero P1, the existing rule. A reply that says PASS but lists a P0 or P1 counts as FAIL. |
| Result | The withheld message result is published with `review` attached (applet-interface.md 3.9). The applet never sees the reviewer's session. |
| Rounds | Counted per `job`. Round 2 and later go to the same reviewer slot when it is free and only re-run the reproductions of the open findings. After `review.max_rounds` (default 3) FAIL verdicts the result says `capped`; further `fix` messages of that job are refused `review_cap`. The applet then bounces the job or escalates (`plan`). |
| No verdict | Reviewer timeout, an unusable reply after the correction rounds, or no slot within `review.timeout_s`: `review.verdict` is `none` with a `reason`. It is not a round. The applet may hand back again. |
| Lease clock | While a review of its fix runs, the fix lease does not expire. When the review ends, the lease's end is at least `renew_margin_s` away, so the applet can send findings or release. Bounded by `review.timeout_s`. |
| Record | The lease record keeps the last review (`last_review`); `applet.lease.released` carries it, so a lease released without a passed review is visible in the audit. |

What stays the applet's rule: whether a failed review blocks the job, how many fix attempts a job gets, and what
happens to a bounced job.

## 7. Escalation

An applet acts only inside what its code and its leases cover. Everything else goes up; the applet waits or
moves on to other work, and executes only what an approval covers.

| Situation | `class` | Ruled by | Mechanism |
|---|---|---|---|
| Change the plan, the target, the order; drop or defer part of the job | `plan` | Host supervisor | Decision key `applet` |
| The worker needs a wider scope than the lease's purpose | `scope` | Host supervisor | Same; an approval is followed by a new lease or a renewal with the new purpose |
| Write outside the host workspace, network or install needs, anything touching credentials | `access` | Root supervisor | Sub-fleet host: the host supervisor forwards with `report_up` (section 46); it cannot approve this itself. Root host: the host supervisor is the root supervisor and rules on it directly, under the same section 46 rules. Approved paths come back as program grants. |
| Delete or move files or directories | `delete` | Human owner only | Pending item + channel push; answered by `fleetmux applet answer` or the channel's two-step approval. No supervisor can approve it, on a root host or a sub-fleet host. |
| A leased worker replies `blocked` | - | The applet forwards it as `scope` or `plan`, or releases | The applet never answers the worker's question by itself in a way that widens the scope |
| The applet offered `options` and needs a machine-readable answer | any | as above | An approval carries `choice`, the index of one option. The applet branches on `choice` only. |
| The supervisor wants the applet to stop or change course | - | Host supervisor | Decision `pause` / `resume` / `revoke`; an operator instruction to the supervisor (section 48) is the way in for anyone outside |

Outside sessions and operators do not instruct a leased worker or an applet's job directly; they tell the
supervisor, which rules and uses the decisions above. The applet's inbox (`fleetmux applet send`) is for **work
items** (enqueue this branch), not for plan changes.

## 8. Failure modes and recovery

| Failure | Detection | Automatic response | Left for a human |
|---|---|---|---|
| Applet crashes | Process exit | Leases ended, immediate restart, crashloop at the third in an hour | Clear a crashloop |
| Applet hangs | Stale heartbeat | Kill, then as a crash | |
| Applet has a bad config | Exit 78 | No restart, reason shown | Fix and `fleetmux applet start` |
| Supervisor never answers a lease request | `decision_wait_s` | `timeout` / `no_decision`; the applet retries later | A supervisor that is stuck is handled by the existing watchdog (section 24, section 36) |
| Supervisor answers wrongly (leased slot, busy slot, wider paths) | Program re-check | Correction path; counts toward the existing consecutive-bad-reply limit | |
| No free worker | Program gate | Wake an asleep worker or add one (3.3) | |
| Worker limit reached | Program gate | `worker_limit` + `retry_after_s` (13.1 "Worker limit") | Raise `hot_add_max` or the fleet limits |
| A hot-added worker does not start | `prepare_timeout_s` | Lease revoked `prepare_failed`; the row stays for the next request | A CLI that is not logged in |
| Leased worker stuck in a turn | Watchdog (section 24) | Rotation; lease continues | |
| Leased worker hits a wall | Wall table (section 23) | Model chain; lease continues | End of chain: resume the fleet |
| Message deadline passes | `timeout_s` | Slot held, lease revoked | Release the held slot per existing rules |
| Reviewer times out or replies unusably | `review.timeout_s`, reply check | Result with `review.verdict: none`; the reviewer slot is rotated or held by the existing rules | |
| Review fails `max_rounds` times | Round count | `capped`; further hand-backs of the job refused | The applet's bounce or escalation |
| Applet forgets to release | Lease term | Expiry returns the worker | |
| Applet dies holding a lease | Process exit | Lease ended at once | |
| Applet is detached while it works | Roster reload | 2.1: drain, revoke, stop | |
| Two hosts declare the same applet | Owner record (2.2) | The second waits (`applet.attach_wait`) | Remove one row |
| Host daemon dies | Sub-fleet: the parent's liveness check (section 29). Root: the supervisor's daemon watch (section 36). | Daemon resumed on the same Redis; applets adopted; in-flight messages redone under section 17 | Daemon crashloop; a root whose supervisor cannot run the resume command |
| Host Redis dies | As above | Restarted from its own directory with the daemon | |
| Host goes down (any reason) | Planned | Drain, stop applets, end leases | |
| Parent of a sub-fleet host is not running | - | Nothing watches the host daemon; on the parent's `start --resume` the host and its applets come back | Start the parent |
| Machine sleeps past a lease end | Clock on resume | Lease expired; the applet applies again | |
| Two copies of one applet on one host | Start clears a previous process with the same recorded pid; the token of the older copy stops matching | The older copy's ops are refused `bad_token` | |
| Result arrives after the lease ended | Existing late-result rule (section 43) | Audited as late, not used | |

Recovery rule for applet authors: every step is either idempotent or guarded by a record in the durable state
directory written **before** the step's side effect, so a restart knows whether to redo or to verify.

## 9. Security boundaries

The private Redis is a Unix socket with owner-only permissions; everything that can open it runs as the same OS
user. The token therefore guards against **mistakes** (a stale applet copy, a worker script poking the socket, a
caller using the wrong applet name), not against a hostile local user. The boundaries below are enforced by the
program where stated and by rule files otherwise, as elsewhere in fleetmux (section 46 "known boundaries").

| Party | May | May not | Enforced by |
|---|---|---|---|
| Applet | Read and write its state and run directories and whatever its own project needs. Submit `applet.*` ops and lease messages. Read its own `fx:applet:<name>:*` keys and `fx:result:<its ids>`. | Call a model or start an agent CLI. Write any other `fx:*` key. Message a worker without an active lease on it. Message a reviewer. Delete or move files without a human approval. Install launchd / cron / systemd entries. Bind a network port for fleet purposes. | Program: lease and token checks on every op and message. Review + the team guide's checklist for the rest (an applet is ordinary code running as the fleet user; the roster entry that names its command is the authorization). |
| Leased worker | Write its own directory and the lease's granted paths. Use its normal tools. | Work outside the purpose. Write elsewhere. Delete or move. Take instructions from anyone but the lease holder. Judge the result. | Rule file section + program grants (section 46). Hard sandboxing depends on the worker CLI and is not added by this design. |
| Reviewer | Read the fix's scope. Write its report in its own directory. | Edit the reviewed files. Take instructions from the applet or the fixer. | `REVIEWER.md`; no write grant is issued. |
| Host supervisor | Grant, deny, renew, revoke; rule on `plan` / `scope`; on a root host also `access`. | Dispatch to a leased or reviewing slot. Approve `delete`. Approve `access` on a sub-fleet host. Widen paths beyond the request. | Program checks on the decision. |
| Outside callers | `fleetmux applet send` (work items), `fleetmux request` to non-leased workers, operator text to the supervisor. | Message a leased worker. Submit ops for an applet (no token). | Program. |

Secrets: an applet's credentials (deploy keys, API tokens for its own deterministic calls) stay in the applet
project's own secret store or environment and are never put in a lease's `purpose`, `context`, `payload` or an
audit `detail`; those are shown to models and written to logs.

## 10. Observability

- **Audit**: every transition in sections 2, 3 and 6 writes an event (`applet.*`, `applet.lease.*`,
  `applet.review.*`; list in applet-interface.md section 6). The applet's own milestones are `applet.event`
  records, free of model turns.
- **Status tree**: `fleetmux status` shows, under each fleet that hosts applets, its applets (state, phase text,
  open leases) and, on each slot, the lease or review that holds it and whether the row was added for a lease.
- **Supervisor view**: lease requests, renewals and escalations arrive as tagged messages; `applet.notify` lines
  are rate limited so an applet cannot flood the supervisor's context. A hot-added worker appears in the next
  `[Roster change]` list (section 16).
- **Terminal**: each applet has its own tab next to the daemon's, showing its stdout / stderr.
- **History**: the leased worker's and the reviewer's generations, screens and handoff notes are recorded as for
  any worker; lease and review start and end appear in the slot's timeline.

## 11. Versioning

One integer interface version, carried in the roster entry (`interface:`), the environment and every op (`v`).
Within a version the interface only grows. Details and the exact promises: applet-interface.md section 10. The applet
interface is part of fleetmux's external contracts: a concrete applet lives in its own project and repository and
depends only on that page.

## 12. Worked example: a release train

This shows that the interface can express a real applet end to end. It is not the design of that applet.

| Step | Who | Interface used |
|---|---|---|
| A branch is submitted for release | Outside caller | `fleetmux applet send applet-rollout-1 item.json`; the applet persists it. On submission the applet runs the branch's tests on the branch's own base and records "passes alone". |
| Queued branches are merged into one batch | Applet (git) | none; `applet.audit batch.merged` |
| A merge conflict needs judgment | Applet -> supervisor -> worker | `applet.lease.request` (job = batch id, paths = the batch work tree) -> `Lease.send` with the conflict -> the applet re-runs the merge check itself |
| The whole batch is tested once | Applet (test command) | none |
| A failing test is attributed | Applet (deterministic) | A test that failed in the batch but passed on every branch alone = caused by the merge. A test that already fails on one branch alone = that branch's own problem. |
| Merge-caused failure | Applet -> leased worker | Plain messages until the applet's own test run passes or `max_fix_attempts` is used; `renew` if the term is short. If every worker is busy the fleet adds one for the lease. |
| The fix is handed back | Applet -> leased worker, then the fleet | A last message with `fix` (the batch work tree, base and head). The fleet has another slot review it; the applet gets `review.verdict`. On `fail` the findings go to the fixer and the step repeats. |
| A branch's own failure | Applet | The branch leaves the batch and goes back to its author (`applet.audit batch.bounced`, plus the applet's own notification); the rest of the batch continues. If dropping it changes the agreed plan: `applet.escalate` (`plan`). |
| Install | Applet | none (its own install command, local or over SSH). Removing an old release directory: `applet.escalate` (`delete`) -> human. |
| Health check, rollback | Applet | none; `applet.audit release.healthy` / `release.rolled_back`; `applet.notify` once on rollback. |
| Publish and watch CI | Applet | none |

Nothing in the train needs the applet to call a model, to arrange a review, or a worker to decide pass or fail.

## 13. Decisions and open questions

### 13.1 Decided

| Topic | Decision | Where |
|---|---|---|
| Host | An applet is hot-loadable and its host is the owner's choice: a root fleet or a named sub-fleet, in service mode. | 1.3, 2.1 |
| Watcher | The host's own daemon watches the applet process, for both kinds of host. | 2.3 |
| Review | Independent review of a fix is done by the fleet, not by the applet. The reviewer must be a different slot; a different model is not required. | 6 |
| No free worker | The fleet wakes or hot-adds a worker and grants the lease on it; it does not refuse and does not queue. | 3.3 |
| Lease shape | One slot per lease, one job per lease, on demand; renew when the term is short. | 3 |
| Worker limit | At `hot_add_max` (or a fleet limit) the lease request is refused with `worker_limit` and `retry_after_s`; the applet retries from its own loop. A review waits for a slot, up to `review.timeout_s`. One rate-limited line tells the supervisor. Raising the limit is a hot parameter change. | 3.3 |
| Reviewer choice | The program picks the reviewer; it costs no supervisor turn. | 6 |
| Sleeping hot-added slots | A hot-added slot that idle reclaim put to sleep stays in the roster and counts toward `hot_add_max`; removing it is a manual roster edit. | 3.3 |
| Lease during review | The fix lease does not expire while its review runs; after the review at least `renew_margin_s` remains. | 6 |
| Project YAML writers | The host daemon and the SDK both take an exclusive lock on `<file>.lock` before writing the project YAML. | applet-interface.md 2.5 |

### 13.2 Open

Not decided. Each has options and a recommendation; the rest of this design assumes the recommendation only
where marked "assumed".

| # | Question | Options | Recommendation |
|---|---|---|---|
| 1 | Should an unanswered renewal extend the lease briefly? | (a) Strict: `renew_grace_s` = 0. (b) Default grace of a few minutes. | (a) is the default, assumed; the field exists so a team can choose (b). Applets should ask at `lease.expiring`, five minutes ahead. |
| 2 | Should leases survive the periodic rotation of a sub-fleet host's supervisor? | (a) No: drain, end leases, applets re-apply after the up. (b) Rotate the host supervisor in place without taking the sub-fleet down. | (a) for version 1, assumed. (b) is a change to named sub-fleets themselves and is worth doing separately; applets written for (a) keep working under (b). A root host is not affected. |
| 3 | Roster and applet parameters in one project file. The roster parser accepts only roster keys, so one YAML for the whole applet project needs block addressing. | (a) `--roster file.yaml#block` and `subfleets[].roster: file.yaml#block`. (b) Separate roster files next to the project YAML. | (a): small, general, and lets a project keep a single YAML. The team guide shows (a) and marks it proposed, with (b) as the fallback that works on the roster parser as it is. |
| 4 | Where the human answers a `delete` escalation. | (a) Operator command only. (b) Also the channel two-step approval used for delete dialogs (section 53). | Both, (a) first: it needs no channel setup. |
| 5 | Applet-specific: when a new fleetmux release needs running fleets to stop, does the release applet ask the human each time or pick a time when no fleet is running? | (a) Ask each time. (b) Applet picks an idle window by a parameter. | Not an interface question; the interface supports both (`applet.escalate` for (a), parameters for (b)). Decide in the release applet's own design. |
| 6 | Applet-specific: must every change to a repository an applet releases go through the applet's queue? | (a) Yes, the applet is the only path to the released branch. (b) Direct merges allowed. | Not an interface question; decide in that applet's design. |
