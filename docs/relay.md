# Relay development: one job across several machines

Design: [design.md](design.md) section 59. Formats: [contracts.md](contracts.md) section 13.

## Overview

Relay development lets several of your machines work on one development job in turns. The job is split into
tasks. Each machine claims tasks it is fit for and runs each one in a dev_task sub-fleet of its own root fleet.
When a machine sleeps, shuts down or loses its network, another machine takes over its tasks from the last
checkpoint and goes on. When it comes back, it does not take them back.

All shared state lives in git on one hub (the state remote, by default the project's GitHub repository).
Machines never talk to each other directly.

The relay program is part of fleetmux. The root fleet daemon runs it as a built-in applet
(`applet-relay-1`). It never calls a model and never starts an agent CLI. Judgment stays with the supervisors.
It is off by default.

## When to use it

1. Use it when one job is split into tasks that can run in parallel or in sequence on different machines,
   for example a `laptop` that sleeps, a `desktop` and an always-on `server`.
2. Use it when a task must survive its machine going away without notice.
3. Do not use it for a job that runs on one machine. A plain dev_task job (design.md section 58) is simpler.

## Steps

1. **Turn it on.** On every machine, set `relay.enabled: true` in the private `fleet.yaml` (the shipped file
   keeps `false`). The daemon reads it when it starts or resumes and at every roster reload, so a change takes
   effect at the next daemon restart or roster reload. Turn it on for one root service fleet per machine: two
   relays on one machine would write the same node record.

   ```yaml
   relay:
     enabled: true
   ```

2. **Refresh the inventory.** On every machine, run:

   ```
   fleetmux superpowers refresh
   ```

   This records `hardware` (`cores`, `perf_cores`, `mem_gb`, `disk_free_gb`; sizes in GiB), `always_on` and the
   found `toolchains` (`rust`, `node`, `python`, `go`) in this machine's inventory entry. The relay program uses
   them to place tasks. The first refresh needs `--name <name>`: that name is this machine's node name in the
   relay. `always_on` defaults to `false` on a machine with a battery and `true` otherwise; a value you set by
   hand is kept.

3. **Write the job into the repository.** Create `.relay/<job>/` in the project repository:

   ```
   .relay/<job>/job.yaml                  # tasks, dependencies, weight, toolchains, merge task
   .relay/<job>/<task>/job.spec.yaml      # start spec of the task's sub-fleet
   .relay/<job>/<task>/brief.md           # the task brief (the only plan)
   .relay/<job>/<task>/roster.yaml        # sub-fleet roster; slot names end in -<number>
   .relay/<job>/<task>/roles/             # role files
   .relay/<job>/<task>/gate.sh            # delivery gate; runs from the repo root
   ```

   Use repository-relative paths only. Commit and push.

4. **Join from each machine.** On every machine that takes part, run once:

   ```
   fleetmux relay join <job> --repo <path to this machine's clone>
   ```

   `join` records the membership and pushes this machine's first node record. The running relay then beats for
   the job. A machine stays `rejoining` for `settle_s` before it becomes `online` and can claim tasks. On join
   and on every heartbeat round, each machine copies the other machines' `hardware`, `always_on` and
   `toolchains` from their node records into its own inventory (`machines.<peer>`). It never writes its own
   entry.

5. **Create the task table.** After every machine has joined, run on one machine:

   ```
   fleetmux relay init <job> --from .relay/<job>/job.yaml
   ```

   Tasks without dependencies start `pending`. Tasks with dependencies, and the `merge` task, start `blocked`.
   Running `init` twice for the same job is refused. Join every machine first: a machine that has not joined
   when `init` runs cannot be the `prefer` machine of a task that someone else claims first.

6. **Watch.** On any machine, run:

   ```
   fleetmux relay status [<job>]
   ```

7. **Work inside a task.** The task's sub-fleet works as any dev_task sub-fleet. In addition:
   1. Workers commit and push to the task branch after every small step.
   2. Each slot keeps `.relay/<job>/<task>/notes/<slot>/PROGRESS.md` with the five fixed headings and commits
      it. The rendered goal tells every slot so.
   3. The sub-fleet supervisor marks a phase: `fleetmux relay checkpoint <task> --phase <name>`.
   4. Delivery is automatic. When the dev_task delivery gate passes and queues the commit, the relay pushes that
      commit to the task's deploy branch and records the task as delivered. If the push fails, it retries next
      round and the dependents stay `blocked`. By hand: `fleetmux relay deliver <task> [--commit <sha>]`.
   5. To stop a task until a person decides: `fleetmux relay park <task> --reason <text>`.
   6. Every task command also takes `--job <job>` when this machine has joined more than one job.

8. **Leave in good order.** Before closing the lid or shutting down, run:

   ```
   fleetmux relay leave [--reason shutdown|sleep|operator]
   ```

   It snapshots each owned task, marks it `handed_over` and pushes a goodbye. Other machines can take the tasks
   at once. The default reason is `operator`. A one-shot `leave` also makes the running relay leave. Stopping
   the root fleet stops the relay with a signal, and it leaves with reason `shutdown`. A closed lid without a
   leave is handled as a lost machine.

9. **Take a task by hand.** `fleetmux relay take <task>` claims a task with the same checks and the same
   compare-and-swap as an automatic claim. It is refused with the reason when this machine is not eligible.

10. **Finish.** When every task is delivered, the `merge` task is released. The machine that claims it merges
    the delivered branches into `relay/<job>/merged` and runs the full gate. Delivering `merge` ends the job.

## Rules

1. Only state pushed to the state remote counts. Every write of a task record is a compare-and-swap. A
   rejected push means another machine was first. The program does not retry the same claim.
2. Each task has one owner at a time. Each owner change adds 1 to the task's epoch. Code branches are named
   `relay/<job>/<task>/e<epoch>-<machine>`, so two owners never write the same branch.
3. Machines never compare clocks. A machine judges another machine lost only from its own monotonic clock:
   "no change to its refs for `lost_after_s`". While a machine cannot read the hub, it judges nobody.
4. A task is placed only on a machine that is `online`, has every toolchain in `needs`, is above the disk
   floors and is under the task caps. `prefer` wins when that machine is eligible. Otherwise a heavy task goes to
   the highest capability score and a light task to the machine with the fewest tasks.
5. A task no machine can take stays `pending`, and the coordinator's supervisor gets `relay.unplaceable` with
   what is missing. The program never installs a toolchain.
6. A machine that comes back reads the task table before it pushes anything about tasks. For each task taken
   over in the meantime, it stops that task's sub-fleet, pushes everything it has to a salvage ref and never
   writes that task again. It keeps its local work tree.
7. A machine that comes back takes no new task until it has been online for `settle_s` without another sleep.
8. The task names `merge`, `merged` and `coordinator` are reserved.
9. The coordinator role is optional. By default one machine, an always-on one first, holds it, and its
   supervisor receives `relay.peer` and `relay.unplaceable`. To turn it off, set
   `relay.coordinator.enabled: false`. Then no machine sends `relay.peer`, and each machine's own supervisor
   gets `relay.unplaceable`.
10. Files under `.relay/` hold no absolute machine path (a home or volume directory, `~/`, `$HOME`, a drive
    letter). `init` refuses them and names the file and line. Keep machine names and accounts out of them too.
11. Snapshots include every file in the work tree that git does not ignore. Keep secrets and credentials in
    `.gitignore`. Files matching `relay.secret_globs` are left out of snapshots with a warning.
12. For a public project, point `relay.state_remote` at a private repository: the state holds machine names
    and progress.

## Example

Three machines, `laptop`, `desktop` and `server`, and a job `demo` with three tasks and a merge task.

`.relay/demo/job.yaml`:

```yaml
job: demo
nodes: [laptop, desktop, server]
tasks:
  ui:    {title: "client side", weight: light, needs: [], prefer: server}
  core:  {title: "protocol core", weight: heavy, needs: [rust], prefer: laptop}
  api:   {title: "server side", weight: heavy, needs: [rust], depends_on: [core], prefer: desktop}
merge:
  depends_on: [ui, api, core]          # default: every task
```

The `merge` task has its own directory `.relay/demo/merge/` with a `job.spec.yaml` whose gate runs the full test
suite. `.relay/demo/core/job.spec.yaml`:

```yaml
goal: "Read .relay/demo/core/brief.md and do it."
hours: 8
artifacts:
  - {path: notes/integ-1/integration.md, min_chars: 300}
  - {path: notes/reviewer-1/verdict.json, contains: ['"verdict"']}
deliver:
  test_cmd: ["/bin/bash", ".relay/demo/core/gate.sh"]
  test_timeout_s: 1800
  review_file: notes/reviewer-1/verdict.json
```

What happens:

1. `ui` goes to `server` and `core` to `laptop`. `api` stays `blocked` on `core`.
2. `laptop` sleeps without leaving. After `lost_after_s`, `desktop` takes `core` (epoch 2) from the last
   checkpoint, with a byte-identical work tree and a `TAKEOVER.md` for its sub-fleet supervisor.
3. `desktop` delivers `core`. `api` turns `pending` with `core`'s delivered commit as its base, and `desktop`
   (its `prefer`) claims it.
4. `laptop` wakes. It finds `core` taken, stops its old sub-fleet, pushes its extra work to
   `salvage/core/laptop-e1` and becomes online and idle after `settle_s`.
5. When `ui`, `core` and `api` are delivered, `merge` is claimed, merged and gated.

`fleetmux relay status demo` during step 2:

```
job demo   hub github.com/<owner>/<repo>   3 nodes
NODE      STATE     SEEN     TASKS
desktop   online    12s      core (working, e2, phase impl)
laptop    lost      7m ago   -
server    online    40s      ui (working, e1, phase tests)

TASK         STATE        OWNER     EPOCH  CHECKPOINT             DEPENDS
api          blocked      -         0      -                      core
core         working      desktop   2      phase design           -   taken over from laptop (lost)
ui           working      server    1      auto #4                -
coordinator  working      server    1      -                      -
merge        blocked      -         0      -                      api core ui
```

SEEN is how long ago this machine last saw that machine's refs change, on its own clock. CHECKPOINT is
`phase <name>` or `auto #<seq>`. The `status` and other one-shot commands read the running relay's view of the
other machines while that view is fresh (younger than `lost_after_s`). Otherwise they judge from the hub, and
SEEN shows `-` for machines the view never saw.

## Checklist

Before `relay init`:

1. `.relay/<job>/` is committed and pushed.
2. No file under `.relay/<job>/` holds an absolute machine path, a machine name or an account.
3. Every task, and `merge`, has a directory with `job.spec.yaml`. Its keys are `goal`, `hours`, `budget`,
   `artifacts`, `workers`, `supervisor_profile`, `worker_profiles` and `deliver` (`test_cmd`, `test_timeout_s`,
   `review_file`). It does not set the keys the program fills.
4. Every `needs` entry uses an inventory toolchain name (`rust`, `node`, `python`, `go`), optionally with a
   bound such as `node>=20`.
5. Every `gate.sh` runs from the repository root and takes its interpreter from `RELAY_PYTHON`.
6. No task is named `merge`, `merged` or `coordinator`.
7. Secrets, build output and virtual environments are in `.gitignore`.
8. Every machine has `relay.enabled: true`, a fresh inventory entry, a running root fleet, and has joined.

## Requirements

1. One state remote that every machine can read and push to. The code remote is the clone's `origin`.
2. git and gh found by the shared PATH lookup (`discover.search_path()`).
3. A running root service fleet on each machine (transport v2) with at least one worker row in its roster. The
   built-in applet starts only there, never in a task fleet or a sub-fleet.
4. An inventory entry for each machine with its name (`fleetmux superpowers refresh --name <name>` once). Without
   it the daemon logs `applet.builtin_error` once and does not start the relay.

## Configuration

The `relay` block of `fleetmux/config/fleet.yaml`. A private `fleet.yaml` overrides it key by key. The `hot`
keys are loaded into the root fleet's Redis hash `fx:relay:params` at start, can be changed while it runs and are
written back every `write_back_seconds` to the private file only, never to the shipped one. A hot value that
breaks a rule is not used; the last good value stays.

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Switch. Turn it on only on purpose. |
| `state_remote` | `""` | Where task, node, snapshot and salvage refs go. Empty = the code remote. |
| `snapshot_timeout_s` | `45` | Limit of one checkpoint push (record and snapshot). |
| `takeover_md_max_lines` | `60` | Maximum length of `TAKEOVER.md`. |
| `text_max` | `200` | Clip length of titles and progress items in records. |
| `secret_globs` | `[".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "*.p12", ".netrc", "credentials*"]` | File names never put into a snapshot. |
| `probe_timeout_s` | `5` | Limit of one local machine probe. |
| `notify_max` | `1000` | Characters a relay notice may carry. |
| `reason_max` | `200` | Characters of a refusal reason in an audit line. |
| `coordinator.enabled` | `true` | Keep a `coordinator` record. The role is optional: `false` turns it off. |
| `coordinator.prefer` | `""` | Machine that should hold the coordinator role; `""` = always-on machines first. |
| `coordinator.takeover` | `true` | Other machines may take the role over from a lost holder. |
| `hot.beat_s` | `60` | Heartbeat period. |
| `hot.lost_after_s` | `300` | No change for this long = lost. |
| `hot.snapshot_s` | `120` | Auto snapshot period (only when the work tree changed). |
| `hot.poll_s` | `30` | Task table read period. |
| `hot.git_timeout_s` | `20` | Limit of one git network operation. |
| `hot.max_tasks_per_node` | `2` | Tasks one machine works on at once. |
| `hot.max_heavy_per_node` | `1` | Heavy tasks one machine works on at once. |
| `hot.disk_floor_heavy_gb` | `40` | Below this free disk (GiB), no heavy task. |
| `hot.disk_floor_any_gb` | `15` | Below this free disk (GiB), no new task. |
| `hot.score` | `{cores: 1.0, mem_gb: 0.25, disk_free_gb: 0.02}` | Capability score weights. |
| `hot.offline_grace_s` | `0` | Wait after a goodbye before others take over. |
| `hot.claim_stagger_s` | `20` | The machine ranked k waits (k-1) times this before it claims. |
| `hot.wake_gap_s` | `180` | A gap between two rounds above this = the machine just woke. |
| `hot.settle_s` | `600` | Online time needed after a return before claiming new work. |
| `hot.budget_pause_gap_s` | `60` | Reserved for the time-budget pause (not used yet: the pause is deferred). |
| `hot.write_back_seconds` | `300` | Redis to file write-back period. |


## The relay process

The root daemon runs `python -m fleetmux.relay run --node <name>` as the built-in applet `applet-relay-1`. Do not
start it by hand on a machine whose root fleet runs it.

**Exit codes of `relay run`.** The applet host reads 0 as done, 75 as a restart (at once, not counted as a
crash), 78 as held (no restart) and anything else as a crash. Three crashes in an hour stop the applet.

| Exit | When | Owned tasks |
|---|---|---|
| keeps running | No job joined yet. It beats every pass and looks for a join every `poll_s`. | - |
| 0 | Stopped by SIGTERM or SIGINT. | Handed over; node `leaving` with reason `shutdown`. |
| 75 | The hub of every joined job was unreachable for `lost_after_s` on the monotonic clock (a sleep is not an outage). One reachable job keeps it up. | Kept; the next process rejoins and settles. |
| 78 | Under the applet: the relay configuration is invalid, or the inventory has no machine of its own. One notice says what to fix. | Kept |
| 69 | The same as 78, when run by hand. | Kept |
| 1 | A refused operation or an unexpected error. | Kept |

The one-shot commands exit 0 when done, 1 when refused (lost CAS, not eligible, not the owner), 2 on a usage error,
69 when the relay is unavailable (bad configuration, no inventory name).

**Heartbeat.** Under the applet, every pass of the main loop writes the applet heartbeat (at most once per
`heartbeat_s`), and passes are never more than `heartbeat_s` apart. A pass that hangs, for example on a git call,
stops beating, and the host marks the applet stalled. A heartbeat that fails is logged, not a crash. By hand,
`run` does not beat.

**Hot parameters.** Under the applet, the relay seeds every `relay.hot` key into the Redis hash
`fx:relay:params` at start. It reads hot values from there on every use. Every `write_back_seconds`, and on a
clean stop, it writes changed values into the user's `fleet.yaml`. By hand, it reads the files.

**Hub backoff.** While a job's hub cannot be read, the next read waits `poll_s`, then twice as long each time,
up to `max(poll_s, beat_s)`. A good read resets the wait.

## Known limitations

1. **Join before init.** A machine that joins after `init` may find that its `prefer` tasks were already
   claimed by machines that joined first. Join every machine, then run `init`.
2. **One relay per machine.** Every root service fleet with `relay.enabled: true` starts its own relay, and two
   relays on one machine write the same node record. Turn it on for one root service fleet per machine.
3. **One-shot view freshness.** `status` and the one-shot commands use the running relay's view only while it is
   younger than `lost_after_s`. Otherwise they judge from the hub, and SEEN shows `-` for machines the view never
   saw.
4. **Secret-looking files.** Files matching `relay.secret_globs` are never in snapshots, so a takeover does not
   get them. The owner's supervisor gets one `relay.snapshot` notice naming them.
5. **Time budget across a sleep.** A dev_task time budget still counts the time a machine sleeps.
   `relay.hot.budget_pause_gap_s` is reserved for that fix and is not read yet.
6. **Salvage note during delivery.** If a worker commits `notes/SALVAGE.md` and a later salvage appends to it,
   a delivery in that window fails with "uncommitted changes" until the worker commits again. No work is lost.
