# Distributed development with multiple fleets

How to develop with two or more fleetmux instances on different machines. Each fleet serves the others and
calls them. One always-on fleet owns the work, so the work goes on when the other machines go offline.

> **Status:** guide and design reference. The steps come first. The design behind them is in
> [Reference](#reference). Items marked **[pending]** need a change that is not in the current release.

## Overview

| Fleet | Runs on | Role |
|---|---|---|
| **Home fleet** | An always-on machine | Owns the goal. Plans, builds through a development sub-fleet ([applet-development.md](applet-development.md)), reviews, saves the result. |
| **Field fleet** | A machine you carry or a second machine | Runs the steps that only that machine can run. Its liaison worker reports to the home fleet and does what the home fleet asks. |

Key points:

- **Peers, not master and slave.** Each fleet receives through its own entry points. Each fleet calls a
  peer through a liaison worker that runs `fleetmux remote`.
- **The link is ssh.** No new port, broker or tunnel. Connection details are one entry per machine in the
  `machines` block of `fleet-superpowers`.
- **The home fleet owns the goal.** It gets the goal as its own request, not only through field reports.
  When the field machine goes offline, the home fleet keeps working; only the steps that need the field
  machine wait for its next report.
- **The field calls, the home answers.** By default only the field machine needs ssh to the home machine.
  The home answer carries its next ask.

## When to use it

Use it when:

- the work needs facts or tests from a machine other than the one that builds it; or
- the work must go on after you shut down or leave with your own machine.

A typical case: a problem exists on one machine because of where it is, and another machine of yours has the
models, the logins or the network that can solve it.

Do not use a link to spread one team over two machines. A fleet still runs on one machine
(`docs/applet-team-guide.md` section 3.1). Two linked fleets are two teams that talk.

## Steps

### 1. Prepare the machines

1. **Install the same release** on every machine. Follow the dependency rules in
   [applet-development.md](applet-development.md#dependencies). Installing does not move a running fleet:
   it keeps its release until you restart it with its usual start command. A loaded workflow survives a
   restart of the host fleet; check `superpowers_load` in `fleetmux status` afterwards (see
   [Restart of the host fleet](#restart-of-the-host-fleet)).
2. **Check the running releases** on every machine. Each fleet must run the installed release:

   ```sh
   fleetmux superpowers releases
   ```

   `fleetmux status` shows `release.restart_needed: true` for a fleet that still runs an older release, and
   `fleetmux superpowers show --self` lists every such fleet as a `#` line.
3. **Check ssh** from the field machine to the home machine, with no prompt:

   ```sh
   ssh -o BatchMode=yes <home ssh alias> true; echo $?
   ```

   Put keys, ports and jump hosts in `~/.ssh/config` under the alias. fleetmux stores no key.
4. **Describe each machine.** On every machine, give it the name that workflows and the other machine will use
   (here `home` and `field`). The first refresh refuses to run without `--name`; later refreshes use the
   stored name.

   ```sh
   fleetmux superpowers refresh --name field
   ```

5. **Register each way to reach a machine.** Run `add` once per way, with the route kind in `--via`. On each
   machine, after its first `refresh --name`, register its own inbound ways under its own name; on the field
   machine, register the ways it knows to the home machine:

   ```sh
   fleetmux superpowers add home --ssh <home ssh alias> --via overlay
   fleetmux superpowers add home --ssh <relay alias for home> --via tunnel
   fleetmux superpowers sync home
   ```

   After `sync`, both machines hold both `machines` sections. Each machine writes only its own section,
   except `reach`. `sync` merges `reach`: the copy keeps the entries this machine already has and gains the
   ones the owner declares, each exact entry once. To drop one, run `add <name> --remove-reach <dest>`.
   An entry the owner still declares comes back on the next `sync`.

   **One connection procedure.** Every machine tries the entries of another in one fixed order of route
   kinds, whatever the list order: `thunderbolt` (direct cable), `lan`, `overlay` (an overlay network such
   as Tailscale, direct), `tunnel` (ssh through a domestic relay host), `tunnel-overseas` (ssh through an
   overseas relay host). Entries of the same kind keep their list order. A kind outside the order is tried
   last. The order is `fleet.yaml#remote.reach_order`; see "Hosts come from the inventory".

### 2. Start the home fleet

1. **Start it on the home machine**, in service mode, with `--supervisor-split`, from the field machine or
   from the home machine itself:

   ```sh
   fleetmux remote --host home --home '~/.fleetmux/fleets/<name>' \
     start --lifecycle service --mode service --backend fleetmux --supervisor-split \
     --allow-development-v2 --name <name> --roster <roster> --roles <roles dir>
   ```

   Pass paths relative to the far home directory, or absolute paths. A quoted `~` does not expand there.
   An example roster is `fleetmux/agents/peer/home-dev.example.yaml`.
2. **Open its window on the home machine's screen:**

   ```sh
   ssh <home ssh alias> '~/fleetmux-current/bin/fleetmux gui --runtime ~/.fleetmux/fleets/<name>/state/fleetmux/fleetmux'
   ```

   On macOS, check with `lsappinfo visibleProcessList` on the home machine.
3. **Watch it from the field machine** if you want:

   ```sh
   ssh -t <home ssh alias> '~/fleetmux-current/bin/fleetmux attach --runtime ~/.fleetmux/fleets/<name>/state/fleetmux/fleetmux'
   ```

4. **Hand over the goal** as a request to the home fleet (see
   [applet-development.md](applet-development.md), step 6). This is what lets it continue without the field
   machine.

### 3. Start the field fleet

1. **Start it on the field machine** with a liaison worker. Use any model the field machine can reach:

   ```sh
   FLEETMUX_HOME=~/.fleetmux/fleets/field FLEETMUX_STATE=~/.fleetmux/fleets/field/state \
   fleetmux start --lifecycle service --mode service --backend fleetmux --allow-development-v2 \
     --name field --roster fleetmux/agents/peer/field.example.yaml
   ```

   **Name the CLI and model when the field machine cannot reach Claude.** Without these flags, `start` picks
   the default pair by login state (design.md section 52), not by reachability, and would start Claude there.
   Add:

   ```sh
   --supervisor-cli pi --supervisor-pi-model <provider/model> \
   --worker-cli pi --pi-model <provider/model> --allow-unsandboxed-pi
   ```

2. **Sync again** so each side lists the other's running fleets and their `config` paths:

   ```sh
   fleetmux superpowers sync home
   ```

3. **Tell the field supervisor to connect.** Send one request that names the home machine, the home run's
   config and this machine's name, with the problem and the goal. Type it in the field supervisor pane or
   send it through a channel. The supervisor routes it to the liaison.

### 4. Work

1. **The liaison reports.** It sends a `report` with id `peer.<field>.<topic>.<n>` to the home fleet and
   waits with `fleetmux remote --host home result <id> --wait 60`.
2. **The home fleet answers.** `ask` (do this on site), `done` (finished, names the workflow) or `blocked`.
3. **The liaison does the ask** and sends the next report with `to: <owner>`, so the same home worker keeps
   the conversation.
4. **The home fleet keeps building** between reports. Its development sub-fleet does the work that does not
   need the field machine. See [Work on the home side](#work-on-the-home-side).

The full message set and its rules are in [Message set](#message-set).

### 5. Leave and come back

1. **Leave.** Shut down the field machine at any time. Do not stop the home fleet. An ask that was sent but
   not done is answered by the next report.
2. **Come back.** Start the field fleet again (step 3) and tell its supervisor to continue. The liaison
   reads the last answer with `fleetmux remote --host home result <id>` and goes on from there.

### 6. Finish

1. **Save** the workflow on both machines: `fleetmux superpowers save <workflow.yaml> --also home`. `load` reads
   the workflow from the inventory, so save it first.
2. **Load** the workflow: `fleetmux superpowers load <workflow>`. It loads the applet of every machine the workflow
   names, in order, the far ones over `remote`. It fetches a missing commit from `source.from` over the same
   ssh. See [What `load` does on one machine](#what-load-does-on-one-machine). It refuses when the host fleets
   run different releases and lists machine, fleet and running release. Install one release everywhere and
   restart those fleets. `--allow-mixed-release` loads anyway; use it only in an emergency.
3. **Check** the result. For a workflow that routes Claude, start one Claude session in the field fleet. It must
   reach the API through the route.
4. **Stop:** `fleetmux superpowers unload <workflow>`, then
   `fleetmux remote --host home stop --config <home config>` and `fleetmux stop --config <field config>`.

### 7. Next venue

1. **Connect.** `fleetmux superpowers sync home` (or skip it if the reach list did not change).
2. **Load.** Start a field fleet and run `fleetmux superpowers load <workflow>`.
3. **Done.** No development and no link work.

## Rules

1. **Give the goal to the home fleet.** Field reports carry facts and test results. They do not carry the
   goal alone.
2. **Plan changes come from the user or the orchestrator.** A peer's ask is work, not a plan change. A
   request outside the brief is answered `blocked`.
3. **Never forward a peer request.** A liaison answers a `peer.` request itself.
4. **Never resend after a lost connection.** Wait for the result of the same id. Send again with the same
   id only if the peer says the id is unknown.
5. **Deletions and moves go to the human.** A peer can never approve a deletion or a move for the other side.
6. **Keep each machine's section its own.** A machine writes only its own entry in `machines`. Other
   sections are copies, replaced whole except `reach`, which is merged.
7. **Show the home fleet.** Its window is open on the home machine's screen while it works.
8. **Keep a fleet on one machine.** Two machines means two fleets and a link.
9. **Receive only through existing entries.** `request -`, `operator`, `status`, `result`.
10. **Call only from a worker.** Supervisors route; liaisons call.
11. **Use ssh only.** No port, broker, discovery service or tunnel in fleetmux. Reach details live in the
    user's ssh config.
12. **One source for machines.** The `machines` block. `fleet.yaml#remote` keeps only `default` and
    `reach_order`.
13. **No system services.** No launchd, systemd or cron on either machine.
14. **Applets write nothing outside their fleet.** They read the inventory; they never write it.
15. **No validation layers.** No trial phase, shadow run or extra checking service for the link.

## Known issues

| Symptom | Cause | Workaround |
|---|---|---|
| A program is missing on the far side although it is installed | A non-interactive ssh command does not read the login shell's `PATH`. | Use absolute paths in far commands, as above. **[pending]** one resolver for every check (see [applet-development.md](applet-development.md#dependencies)). |
| `start` on a machine with older fleets exits 2 | Old-name data is not migrated while those fleets run. | See [applet-development.md](applet-development.md#known-issues). |

## Checklist

- [ ] The field machine reaches the home machine by ssh with no prompt.
- [ ] Every host fleet on both machines runs the same release before `load` and before acceptance
      (`fleetmux superpowers releases` on each machine). `load` refuses otherwise; `--allow-mixed-release` is
      for emergencies only.
- [ ] Both machines hold both `machines` sections after `sync`.
- [ ] The home fleet runs in service mode with `--supervisor-split`, and its window is visible on the home
      machine's screen.
- [ ] The home fleet received the goal as its own request.
- [ ] The home fleet kept working while the field machine was off.
- [ ] The workflow is saved and loaded on every machine it names.
- [ ] After unload and stop, nothing is left outside the fleets on any machine.

## Reference

This part is the design behind the steps. It defines four things:

- the **link**: how two fleets talk ("Model" and "Transport");
- the **inventory** `fleet-superpowers`: one file per machine that holds every machine's resources;
- **session environment from an applet**: how a loaded applet routes the fleet's agent sessions;
- **workflows**: how a solved scenario is saved and loaded again.

[Field workflow](field-applet-workflow.md) is the scenario this design supports. In short:

1. The field machine connects to the home machine over ssh and starts a fleet there.
2. The field fleet runs any model it can reach. It tells the home fleet the problem and the goal.
3. The field fleet works as the on-site engineer of the home fleet. The home fleet asks; the field fleet
   looks, acts and reports.
4. The home fleet builds two applets, one for each machine. Each side loads its applet.
5. The workflow is saved in the inventory of both machines. Both fleets stop. Nothing else is left.
6. At the next venue, the workflow loads from the inventory with no development.

### Concepts

| Term | Meaning |
|---|---|
| machine | A computer with one fleetmux release and one OS user that runs fleets. It has a name in the inventory, for example `home` or `field`. |
| peer | Another fleet, on another machine, that this fleet talks to. Peers are equal. Neither one is the master. |
| link | The ability of one fleet to call a peer. It is one entry in the inventory plus `ssh`. It has no process, no port and no state of its own. |
| server side | The existing entry points a fleet already has for outside callers: `request -`, `operator`, `status`, `results`. |
| client side | A worker of this fleet whose role is to talk to a peer. It runs `fleetmux remote --host <peer> ...` commands. |
| liaison | The client-side worker. Its slot name ends in `-<n>` like every worker, for example `onsite-1`. |
| reach | The ssh destinations by which other machines can reach this one, each with a route kind that sets when it is tried. |
| workflow | A `workflows` entry: the applets that solve one scenario, where they load, and when they apply. |

#### Why the remote worker is a whole fleet, not a remote session

A worker slot cannot run its agent session on another machine. Every hook and bridge of a slot is pinned
to a local `config.json` and to the run's private Redis Unix socket (`claude_argv` and the `--settings`
hooks in `fleetmux/fleet.py`, `pi_adapter.environment`, `agent_bridge.py`). The team guide forbids
exposing that socket (`docs/applet-team-guide.md` section 3.3).

So the remote worker is the peer fleet itself. The field fleet's liaison takes the home fleet's asks,
does them on site, and answers. No new slot type is added.

### Model

#### Peers, not master and slave

- Each fleet **serves** through its existing entry points. It needs no extra code to receive.
- Each fleet **calls** a peer through a liaison worker that runs `fleetmux remote`.
- A supervisor never calls a peer itself. The supervisor prompts forbid the fleet CLI
  (`fleetmux/agents/service_supervisor_tui_v2.md`). The supervisor routes peer work to the liaison like any
  other work.

#### Who can call whom

A call needs ssh from the caller to the callee. A field machine on a public network usually accepts no
inbound ssh. The design therefore has one default and one option:

1. **Default: the field calls, the home answers.** The field liaison sends a report. The home fleet's answer
   carries its next ask. The field does the ask and sends the next report. This is turn-taking. It needs
   ssh in one direction only.
2. **Option: the home also calls.** Allowed only when the field's reach list has an entry the home can use,
   for example an overlay network address. The message set is the same in reverse.

fleetmux opens no reverse tunnel for this. If a person wants one, it is part of their own ssh setup.

#### Message set

The receiving fleet must run in service mode to answer. `Fleet.request` refuses requests outside service
mode.

| Message | Sent by | Carried by (entry on the receiver) | Answer |
|---|---|---|---|
| `report` | the caller | `fleetmux request -` with `payload.kind: report` | A result whose reply is `{status, text, owner}` |
| `ask` | the callee | the result of a report, `status: ask` | The caller does it and sends the next report |
| `done` | the callee | the result of a report, `status: done` | The caller stops. `text` names the workflow. |
| `blocked` | the callee | the result of a report, `status: blocked` | The caller's supervisor rules, or asks the human |
| `note` | the caller | `fleetmux operator` (no answer needed) | `accepted` / `rejected` (exit codes 0 / 2 / 3) |
| `status` | the caller | `fleetmux status` | The peer's state tree |
| `result` | the caller | `fleetmux result <id> --wait <s>` | One result JSON line, or pending |

Rules for the payload and ids:

1. **Name the sender in the id.** Ids start with `peer.<sender machine>.`, for example
   `peer.field.claude-route.3`. No new request field is added. Ids already allow dots (`REQ_ID_RE`).
2. **Use one reply schema.** `reply.required` is `["status", "text", "owner"]`. `status` is one of `ask`,
   `done`, `blocked`. `owner` is the slot name of the worker that answered.
3. **Keep the conversation on one worker.** The first report has no `to`, so the home supervisor routes it
   and the advisor picks the model. Each next report sets `to: <owner>` from the previous answer. A service
   request with `to` goes straight to that worker (design.md section 53 "Direct to a worker"). The
   worker keeps its session, so it keeps the context. The supervisor comes back in when the owner answers
   `blocked` or asks to split the work (the `blocked` and `escalate` replies).
4. **Never forward a peer's request to a peer.** A liaison that receives a request whose id starts with
   `peer.` answers it on site. It never sends it on. This is a role rule. See open question 2.

Example report from the field liaison (also shipped as `fleetmux/agents/peer/report.example.json`):

```json
{
  "id": "peer.field.claude-route.1",
  "payload": {
    "kind": "report",
    "from": "field",
    "problem": "Claude sessions in the field fleet fail with a connection error to the Claude API.",
    "goal": "Claude sessions started by the field fleet work on this network.",
    "facts": "curl to the Claude API times out. DNS resolves. Other HTTPS sites work."
  },
  "reply": {"required": ["status", "text", "owner"]},
  "instruction": "Peer report from machine field. Answer with status ask, done or blocked."
}
```

Example answer from the home fleet:

```json
{"status": "ask", "text": "Run: nc -vz home.example 22 and send the output.", "owner": "dev-1"}
```

#### Mapping to entry points

| Peer action | Command on the caller | What runs on the callee |
|---|---|---|
| Send a report | `fleetmux remote --host home request - --config <home run>/config.json < report.json` | `fleetmux request -` |
| Wait for the answer | `fleetmux remote --host home result <id> --wait 60 --config ...` | `fleetmux result <id> --wait 60` (reads the key `fx:result:<id>`) |
| Send a note | `fleetmux remote --host home operator --config ... "<text>"` | `fleetmux operator` |
| Read state | `fleetmux remote --host home status --config ...` | `fleetmux status` |
| Watch the page | `fleetmux remote --host home` | `fleetmux view` |

### Transport

The link is ssh and nothing else.

- **One inventory entry.** `machines.<peer>.reach` lists ssh destinations, each with a route kind.
  `fleetmux remote` tries them in the fixed order of kinds and uses the first one that connects.
- **No new daemon port.** No broker. No discovery protocol. No reverse tunnel opened by fleetmux.
- **Keys, jump hosts and ports stay in the user's ssh config.** A reach entry is a `Host` alias or
  `user@host`. Put `ProxyJump`, `Port`, `IdentityFile` and `ControlMaster` under that alias in
  `~/.ssh/config`. The fleet opens no port, tunnel or VPN and stores no key.

What runs on the far side over the ssh link, and nothing else:

| Far-side command | Purpose |
|---|---|
| `<bin> request - --config <cfg>` | Receive a report |
| `<bin> result <id> --wait <s> --config <cfg>` | Return an answer |
| `<bin> operator --config <cfg> "<text>"` | Receive a note |
| `<bin> status --config <cfg>` | Return state |
| `<bin> view` | Show the live page |
| `<bin> start ...` / `<bin> stop --config <cfg>` | Start and stop a fleet there |
| `<bin> superpowers show --self --json` / `<bin> superpowers import -` | Exchange inventory sections |
| `<bin> superpowers load <workflow> --machine <name>` / `unload` | Load and unload a workflow's applet there |
| `<bin> superpowers save -` | Store a workflow sent on stdin (`save --also`) |
| `git -C <repo> fetch <reach entry>:<source.repo>` | Fetch a workflow's missing commit from `source.from` (run by `load`; git's own ssh, same `~/.ssh/config`) |

`<bin>` is `machines.<peer>.fleetmux.bin`. Its default is `~/fleetmux-current/bin/fleetmux`.

#### Hosts come from the inventory

The `machines` block is the one source of hosts. `fleet.yaml#remote` keeps only `default: <machine name>` and
`reach_order`.

1. **Resolve hosts in the inventory.** `remote.host(name)` reads `machines.<name>.reach` and
   `machines.<name>.fleetmux.bin`. It sorts the entries by the route kind in `via`, in the order of
   `remote.reach_order`; entries of one kind keep their list order, and a kind not in the order goes last. It
   then picks an entry: `ssh -o ConnectTimeout=5 <dest> true` on each entry in that order, and the first that
   exits 0 wins. Every caller picks this way: `remote`, `superpowers sync`, `save --also`, `load`, `unload`,
   and the git fetch of `load`. Then it runs the command once, on that entry only. It never
   retries the command on another entry: exit code 255 can also mean the connection dropped mid-command, and a
   second run could repeat an `operator` or a `start` whose stdin is already consumed.
2. **Keep the block small.** `fleet.yaml#remote` has no `hosts` key; a leftover `hosts` key is exit 2 naming
   the inventory. `reach_order` is a list of distinct route kinds. The shipped value is
   `[thunderbolt, lan, overlay, tunnel, tunnel-overseas]`. A private `remote` block replaces the shipped one
   whole, so without the key the built-in order applies (`remote.REACH_ORDER`). `remote` stays in
   `BLOCKS` and `USER_BLOCKS` in `fleetmux/fleet_config.py`, and `tests/test_fleet_config.py` keeps its
   shipped-block check.
3. **design.md section 55 says the same.** Hosts come from the `machines` block of `fleet-superpowers`. The
   sentence "the fleet opens no port, tunnel or VPN and stores no key" stays, with one clause: an applet loaded
   from a workflow may open its own tunnel or loopback listener.
4. **Use `--home <dir>` for a second fleet home.** `fleetmux remote --home <dir> <command>` runs the far command
   with `FLEETMUX_HOME=<dir>`. Commands that take `--config` do not need it. A leading `~/` in `<dir>` is sent
   as `"$HOME"/`, as `bin` already is. Every other argument is quoted, so a `~` in it does not expand on the far
   side: pass paths relative to the far home directory, or absolute.

### Resource inventory: `fleet-superpowers`

**File:** `~/.fleetmux/fleet-superpowers/fleet-superpowers.yaml`, next to `fleets/`, `board/` and `config/`
(design.md section 50.1). **Code:** `fleetmux/fleet_superpowers.py`. **Command:** `fleetmux superpowers`.

**Blocks:** `models`, `machines`, `quota`, `workflows`. This page defines `machines` and `workflows`.
`models` holds the `fleetmux setup` cache (design.md section 52, formerly `<fleet home>/discovered.yaml`).
`quota` holds the quota readings (formerly `~/.fleetmux/quota.json`). Their content is defined by those designs.
The top-level `hot` mapping holds two settings: `write_back_seconds` (see "Hot or cold") and `refresh_seconds`
(see "Ownership").

**Override:** `$FLEETMUX_SUPERPOWERS` names another file. The test suite points it at a temporary file.

#### Ownership

1. **Write only your own section.** `machines.<self>` is written by this machine only: its `reach` by
   `fleetmux superpowers add <self>` (or by hand), everything else by `fleetmux superpowers refresh` and by the
   root fleet daemons of this machine:
   1. **When.** Each root fleet daemon refreshes the entry once 30 seconds after it starts (so the applets it
      starts are up), then whenever its `updated_at` is older than `hot.refresh_seconds` (default 600; `0`
      turns it off). Sub-fleet daemons never do.
   2. **One at a time.** The daemon takes a non-blocking lock on `<file>.lock.refresh`. When another daemon
      holds it, this one skips the turn; a start refresh tries again after 30 seconds. A fresh `updated_at`
      tells the other root fleets to wait.
   3. **No extra probes.** The refresh runs in a background thread, never in the daemon tick. A probe answer
      younger than `hot.refresh_seconds` is reused, so the start-time refresh records a new fleet without a
      new network request. An answer of `none` is never reused. The file is written under the usual file
      lock.
   4. **No self entry.** Before the first `refresh --name` the daemons skip quietly.
   5. **Broken file.** An inventory file that cannot be read is logged once as `tick.error`; the daemon looks
      again every 60 seconds.
2. **Treat other sections as copies.** `machines.<other>` is a read-only copy. It changes only by
   `superpowers sync` or `superpowers import`, which replace it whole except `reach`, and by
   `superpowers add`, which appends one entry to its `reach` or removes one and changes nothing else.
   `reach` is merged: the entries already there first, then the owner's new ones, each exact entry once.
   `superpowers add` also works on this machine's own name, so the owner registers its own ways with the
   same command (refresh keeps them: it rereads `reach` under the file lock before it writes).
3. **Mark the source.** Every machine entry has `source: self` or `source: copy`. Exactly one entry is `self`
   once the machine has refreshed. Before the first refresh the file may hold copies only (`add`).
4. **Write atomically.** Every write takes a file lock (`<file>.lock`), writes a temporary file in the same
   directory (mode 0600), and renames it.
5. **Change one entry at a time.** A write replaces one entry (`machines.<name>`, `workflows.<name>`) or one
   hot block. Every other byte of the file stays, comments included. A block written in flow style
   (`machines: {a: ...}`) is refused, not rewritten; write it in block style.

#### Hot or cold

| Block | Hot | How it is read and written |
|---|---|---|
| `machines` | No | Read fresh by each command. Changes on refresh, add, import and sync. |
| `workflows` | No | Read fresh by each command. Changes on save. |
| `models` | Yes | Loaded into the fleet's Redis at start. Written back at the interval. |
| `quota` | Yes | Loaded into the fleet's Redis at start. Fed from `quota.json` when that file changes (rule 5). Written back at the interval. The brake reads only this block. |

`fleetmux remote` reads the file on a machine that may run no fleet, so `machines` and `workflows` never enter
Redis. Link state that changes often (last answer, last error) is run state and stays in the fleet's own Redis
and audit, not in this file.

The hot blocks follow these rules (`fleetmux/fleet_superpowers_hot.py`):

1. **Load at start.** Every daemon start and resume copies `models` and `quota` into the fleet's Redis. A
   block changed in Redis and not yet written back is kept on resume.
2. **Read and write Redis while running.** Callers use `get` and `put`. `put` marks the block dirty.
3. **Write back at the interval.** Every daemon tick writes the dirty blocks back to the file when
   `hot.write_back_seconds` has passed since the last write-back. The daemon writes them back once more when
   it exits.
4. **Migrate once.** When a hot block is empty in this file, it is read once from its old file
   (`discovered.yaml`, `quota.json`) and written into this file. The old file is left in place.
5. **Ingest quota.json.** The quota writer is outside this repository and keeps writing `quota.json`. Once after
   the start-time load, and on every daemon tick, the daemon checks the file's size and mtime. When they
   changed, or the mtime is younger than 2 seconds (a same-size rewrite inside one coarse mtime step), it reads
   the file. When the content differs from the block, it replaces the hot `quota` block (marked dirty, so it is
   written back). A missing or unreadable file keeps the last block. Every brake reader reads the hot block
   through one function (`Fleet.quota_reading`); none opens `quota.json`. A pool reading older than
   `brake.QUOTA_STALE` (3600 s) still counts as unknown.

```yaml
hot:
  write_back_seconds: 300   # default when missing
  refresh_seconds: 600      # default when missing; 0 = the daemons never refresh machines.<self>
```

#### `machines` entry schema

| Key | Type | Filled by | Meaning |
|---|---|---|---|
| `source` | `self` or `copy` | refresh / sync | Who wrote this entry. |
| `updated_at` | ISO 8601 with offset | refresh | When the owner last refreshed it. |
| `copied_at` | ISO 8601 with offset | sync / import | When this copy was taken. `copy` only. |
| `hostname` | string | refresh | The machine's own host name. |
| `addresses` | list of `{kind, address}` | refresh | `kind` is `lan`, `overlay` or `public`. Informational. |
| `reach` | list of `{via, ssh}` | the owner, with `add` or by hand; on a copy also `add`, and `sync` merges | How other machines reach this one. `ssh` is a `Host` alias or `user@host`. `via` is the route kind: `thunderbolt`, `lan`, `overlay`, `tunnel` (domestic relay) or `tunnel-overseas` (overseas relay), the kinds of `fleet.yaml#remote.reach_order`. Entries are tried in that order of kinds, list order within a kind; a stored label outside it (such as an older `jump`) is kept and tried last. Empty list = not reachable from outside. |
| `fleetmux.bin` | path | refresh | The `fleetmux` command on this machine. |
| `fleetmux.release` | commit sha | refresh | The installed release: the one `~/fleetmux-current` points at. Without that link, the release of the code that ran `refresh`. A running fleet may still run an older one (`fleets[].release`). |
| `clis` | map cli -> `{version, logged_in}` | refresh, from the `fleetmux setup` cache (design.md section 52) | Installed agent CLIs and their login state. |
| `models` | list of `{id, cli, source, reachable, checked_at}`; probed rows add `probe_host`, `via` and `answered` | refresh | Models this machine can use right now. `reachable` comes from the section 52 probe; for `claude` and `codex` it is one HTTPS request to the CLI's API host at refresh time, sent the way sessions go (see below). |
| `models[].source` | `card` or `listed` | refresh | Where the model id came from. `card`: claude models are copied from the model cards with `cli: claude` (design.md section 52); nothing asks the account, so a `card` model may still be refused by the account (for example a model the plan does not include). `listed`: the CLI listed it (`pi --list-models`, `opencode models`, `ollama list`). No model is probed on its own, so `refresh` spends no quota. |
| `models[].probe_host` | URL | refresh | What the probe asked. `claude`: `https://api.anthropic.com`. `codex`: `https://chatgpt.com/backend-api/codex` when the setup cache says it is logged in with ChatGPT (`auth_method: chatgpt`, from `codex login status`), else `https://api.openai.com`. The codex-cli binary names both base URLs. |
| `models[].via` | applet name or `direct` | refresh | The route of the probe. When a running root fleet on this machine routes the CLI through an applet (`session_env` in its run config, section "Session environment from an applet"), the probe goes through that route's `HTTPS_PROXY` and `via` is the applet. Otherwise the probe goes direct, ignoring proxy variables in the environment. |
| `models[].answered` | `2xx`, `3xx`, `4xx`, `5xx` or `none` | refresh | The HTTP class the host answered with, or `none` for no answer. `reachable` still means "the host answered", so a 401 or 429 is reachable; `answered` shows it. |
| `can_start_claude` | bool | refresh | `clis.claude.logged_in` and at least one `claude` model `reachable`, by the route-aware probe. |
| `fleets` | list of `{name, home, config, mode, running, release, root}`; a sub-fleet adds `parent` and `sub` | refresh, from `transport_inventory.inventory()` and each run's `config.json` | Fleets on this machine: every running run, and the run each fleet home's `latest.json` names. `config` is the run's `config.json` as an absolute path; callers pass it as `--config`. `home` is the fleet home for `remote --home`. A sub-fleet inherits its root's fleet home and so its name; `root: false`, `parent` (the root's name) and `sub` (its id, for example `M0`, or a named sub-fleet's id) tell the rows apart. A root fleet has `root: true`. `release` is the release the run's daemon runs: `<run>/release`, which the daemon writes at every start and resume; for an older daemon, the release directory in the run config's `python` path; else empty. |

`refresh` adds no prober. It reads the `fleetmux setup` cache (`fleetmux/discover.py`) and
`fleetmux/transport_inventory.py`. The only extra probe is the one HTTPS request per CLI in
`models.reachable`. A route without `HTTPS_PROXY` (or `https_proxy`) is
probed direct and recorded as `via: direct`. The setup cache lists no codex models (codex has no model listing,
design.md section 52), so today a machine has no codex rows and no codex probe runs.

#### Capability keys of a `machines` entry

`refresh` writes three more keys after the rest of the entry (`fleetmux/fleet_superpowers_capability.py`). They
come from a system query and one version command per toolchain, not from the setup cache. Multi-machine relay work
reads them to place heavy work on strong machines and to keep work off a machine that lacks a toolchain or disk.

| Key | Type | Filled by | Meaning |
|---|---|---|---|
| `hardware.cores` | int | refresh | Logical CPU cores (`sysctl hw.ncpu` on macOS, the CPU count elsewhere). |
| `hardware.perf_cores` | int | refresh | Performance cores (`hw.perflevel0.physicalcpu`); equal to `cores` on a machine without performance levels. |
| `hardware.mem_gb` | number | refresh | Memory in GiB (2^30 bytes), one decimal: a 32 GiB machine shows `32.0`. |
| `hardware.disk_free_gb` | number | refresh | Free space of the home volume in GiB (2^30 bytes), one decimal, measured at every refresh. Relay node records carry the same unit. |
| `always_on` | bool | refresh, then the owner | Whether the machine stays on. The first refresh writes the default: `false` on a machine with an internal battery (a laptop), else `true`. A stored value is never overwritten: edit it by hand to correct the default. |
| `toolchains` | map toolchain -> version | refresh | Toolchains found on the login shell PATH, then the process PATH (`discover.search_path`), with the version each prints: `rust` (`rustc --version`), `node`, `python` (`python3`), `go`. A missing tool is absent. Refresh only finds and records; it installs nothing. Job definitions name these toolchains in `needs`. |

#### `workflows` entry schema

| Key | Type | Meaning |
|---|---|---|
| `kind` | `workflow` | This entry type. Other designs may add other kinds. |
| `summary` | string | One sentence: what the workflow fixes. |
| `applies_when` | list of strings | The symptoms, in plain words, for a person or a model to match. |
| `check` | optional `{machine, argv}` | A deterministic command. Exit 0 = the workflow applies now. `fleetmux superpowers check` validates its shape; no command runs it. |
| `source.repo` | path | The applet project repository, at the same place on every machine that loads it. Absolute (`/srv/applets/route`), or under each user's home (`~/applets/route`). Use the `~/` form when the machines have different user names. |
| `source.ref` | commit sha | The commit of the applet files. `load` accepts a checkout at this commit, or at a later commit in which the applet files are unchanged (see "What `load` does on one machine"). A workflow file committed to the same repository therefore never blocks `load`. |
| `source.from` | machine name | Where `load` fetches the commit with `git fetch` over ssh when it is missing. |
| `applets` | ordered list | One item per machine. Loaded in order, unloaded in reverse. |
| `applets[].machine` | machine name | Where this applet runs. |
| `applets[].fleet` | fleet name | The running fleet on that machine that hosts it (a name in `machines.<m>.fleets`). A sub-fleet has its parent's name, so when several rows have the name, `load` uses the root service fleet (`root: true` and mode `service`; a row from an older refresh without `root`: mode `service` and no `parent_config` in its config), and refuses when there is more than one. |
| `applets[].host` | optional `{subfleet, supervisor_slot, supervisor_role, roster}` | The named sub-fleet that hosts the applet. `roster` and `supervisor_role` are paths inside the repo; that roster holds the `applets:` row. Absent: the root fleet hosts the applet, and `applets[].roster_rows` holds the `applets:` row (open question 4). |
| `saved_on` | list of machine names | Machines that hold this entry. |

#### Commands

| Command | What it does |
|---|---|
| `fleetmux superpowers refresh [--name <n>] [--json]` | Rewrite `machines.<self>` from local facts. `--name` is required on the first refresh: without it the command exits 1 and prints the suggested name (the host name up to the first dot, lower case) and the command to run. Later refreshes use the stored self name; another `--name` is refused. Keeps `reach`. |
| `fleetmux superpowers add <name> --ssh <dest> [--via <kind>]` | Append `{via, ssh}` to the `reach` of `<name>`, after its existing entries. `<name>` is a copy or this machine's own name. With no entry of that name, create a minimal copy entry so the first sync can reach that machine. `--via` defaults to `lan` and must be a kind of `fleet.yaml#remote.reach_order`; another value is exit 1 with the list of kinds. An entry with the same `via` and `ssh` is refused. The list position does not matter: the kind sets when an entry is tried. |
| `fleetmux superpowers add <name> --remove-reach <dest> [--via <label>]` | Remove the one `reach` entry of `<name>` (a copy or this machine) whose `ssh` is `<dest>` (and whose `via` is `<label>`, any stored label, when given). No match, or more than one, is refused. Removing the last entry leaves `reach: []`. |
| `fleetmux superpowers sync <name>` | Refresh self (refused before the first `refresh --name`), then `remote ... superpowers show --self --json` to replace the copy of `<name>`, then send our own section with `remote ... superpowers import -`. One reach probe serves both calls. Both imports merge `reach`: the entries the importing machine has for that machine stay, the owner's are added, each exact entry once. A far machine whose self name is not `<name>` is refused. Prints a warning when the two machines have different releases installed. |
| `fleetmux superpowers show [--self] [--json]` | Print the file, or only our own section. Without `--json`, `--self` adds one `#` line per fleet whose daemon runs now and runs another release than the installed one. |
| `fleetmux superpowers releases [--fleet <name>]... [--json]` | Print the installed release, the CLI's release and the release each host fleet runs now, read from each run (`--fleet`: picked as `load` picks the host; default: every fleet of this machine whose daemon runs now). `load` runs it on each far machine. |
| `fleetmux superpowers show --machines` / `--workflows` | List the machines (name, source, updated_at, reach) or the workflows (name, summary), one per line. |
| `fleetmux superpowers show --machine <n>` / `--workflow <n>` | Print one entry. |
| `fleetmux superpowers check` | Validate the whole file against this page's schemas. Exit 1 on any problem. |
| `fleetmux superpowers import -` | Store a section from stdin as a copy, `reach` merged as in `sync`. Refuses a section named like `self`. |
| `fleetmux superpowers save <file> [--also <name>]` | Write a `workflows` entry here, and with `--also` on that machine too (`remote ... superpowers save -`, the file on stdin). `<file>` may be `-` (stdin). |
| `fleetmux superpowers load <workflow> [--machine <m>] [--wait <s>] [--poll <s>] [--allow-mixed-release]` | Load the workflow's applets. Without `--machine`: every machine in order, the far ones over `remote` (one command each). First it reads the release each host fleet runs (`releases` over `remote` on a far machine) and refuses when they differ, or when a release is unknown and more than one machine is loaded; the message lists machine, fleet and running release. `--allow-mixed-release` (emergencies only) prints the same list and goes on. With `--machine <other>` it still compares every machine of the workflow; `--machine <self>` loads this machine only and does not compare. A far machine that does not connect counts as unknown. Refuses, before writing, while a process of an applet it would add still runs. Succeeds when every added applet is `up` in a process started by this load. `--wait` (default 120 s) and `--poll` (default 1 s) bound that wait. |
| `fleetmux superpowers unload <workflow> [--machine <m>] [--wait <s>] [--poll <s>]` | Remove what `load` added on this machine, in reverse order, then wait until every applet process those rows ran has ended. `--wait` default: per row `detach_wait_s` + `stop_grace_s` + 5 s (a host sub-fleet: its `drain_s` instead of `detach_wait_s`), the largest of them; `--poll` default 1 s. At the bound it exits 1, names what still runs and keeps the record; run `unload` again to wait again. It never unloads another machine by itself (open question 11); `--machine <other>` runs it there over `remote` with that machine's defaults (`--wait` and `--poll` are not forwarded). |

The commands are a table in `fleetmux/fleet_superpowers_cli.py`. `sync`, `save --also`, `releases`, `load` and
`unload` live in `fleetmux/superpowers_peer.py` and are registered with `register(name, help, add_arguments, run)`
(the `save` there replaces the local one). Their ssh calls use `remote.host`, `remote.pick` and
`remote.ssh_argv`, and run the command once on the picked entry; a 255 is never retried. The file
given to `save` holds one mapping, `{<workflow name>: <workflow>}`. Exit codes: 0 done, 1 refused (one line
`superpowers: <reason>` on stderr), 2 usage.

#### Complete example

```yaml
# ~/.fleetmux/fleet-superpowers/fleet-superpowers.yaml on the field machine
models: {}        # the fleetmux setup cache (design.md section 52); hot
quota: {}         # the quota readings; hot

machines:
  field:
    source: self
    updated_at: "2030-01-01T10:00:00+00:00"
    hostname: field.example
    addresses:
      - {kind: lan, address: 192.0.2.23}
    reach: []                       # no inbound ssh on a public network
    fleetmux:
      bin: ~/fleetmux-current/bin/fleetmux
      release: 0123abc
    clis:
      claude: {version: "2.1.0", logged_in: true}
      pi: {version: "1.5.0", logged_in: true}
    models:
      - {id: opus, cli: claude, source: card, reachable: false, checked_at: "2030-01-01T10:00:00+00:00",
         probe_host: "https://api.anthropic.com", via: direct, answered: none}
      - {id: deepseek-flash, cli: pi, source: listed, reachable: true, checked_at: "2030-01-01T10:00:00+00:00"}
    can_start_claude: false
    hardware: {cores: 10, perf_cores: 4, mem_gb: 32.0, disk_free_gb: 59.0}
    always_on: false                # a laptop: it sleeps when the lid closes
    toolchains: {python: "3.12.4", rust: "1.83.0"}
    fleets:
      - {name: field, home: ~/.fleetmux/fleets/field, config: ~/.fleetmux/fleets/field/runs/r1/config.json,
         mode: service, running: true, release: 0123abc, root: true}

  home:
    source: copy
    copied_at: "2030-01-01T10:05:00+00:00"
    updated_at: "2030-01-01T10:04:58+00:00"
    hostname: home.example
    addresses:
      - {kind: lan, address: 192.0.2.10}
      - {kind: overlay, address: 100.64.0.10}
    reach:
      - {via: lan, ssh: user@home.lan.example}
      - {via: overlay, ssh: user@home.example}
      - {via: tunnel, ssh: home-via-relay}     # an alias with ProxyJump in ~/.ssh/config
    fleetmux:
      bin: ~/fleetmux-current/bin/fleetmux
      release: 0123abc
    clis:
      claude: {version: "2.1.0", logged_in: true}
      codex: {version: "0.164.0", logged_in: true}
      pi: {version: "1.5.0", logged_in: true}
    models:
      - {id: opus, cli: claude, source: card, reachable: true, checked_at: "2030-01-01T10:04:58+00:00",
         probe_host: "https://api.anthropic.com", via: direct, answered: 4xx}
      - {id: openai-codex/gpt-6-astra, cli: pi, source: listed, reachable: true,
         checked_at: "2030-01-01T10:04:58+00:00"}
    can_start_claude: true
    hardware: {cores: 16, perf_cores: 12, mem_gb: 128.0, disk_free_gb: 132.4}
    always_on: true
    toolchains: {go: "1.22.3", node: "22.1.0", python: "3.12.4"}
    fleets:
      - {name: main, home: ~/.fleetmux, config: ~/.fleetmux/runs/r7/config.json,
         mode: service, running: true, release: 0123abc, root: true}
      - {name: main, home: ~/.fleetmux, config: ~/.fleetmux/runs/r7/sub/route-home/config.json,
         mode: service, running: true, release: 0123abc, root: false, parent: main, sub: route-home}
      - {name: dev, home: ~/.fleetmux/fleets/dev, config: ~/.fleetmux/fleets/dev/runs/r2/config.json,
         mode: service, running: true, release: 0123abc, root: true}

workflows:
  claude-via-home:
    kind: workflow
    summary: Route the field fleet's Claude sessions through a proxy on the home machine.
    applies_when:
      - On the field machine, Claude sessions fail with a connection error to the Claude API.
      - The home machine reaches the Claude API.
    check: {machine: field, argv: [python3, -m, claude_route.check]}
    source:
      repo: /srv/applets/claude-route
      ref: 4d5e6f7
      from: home
    applets:
      - machine: home
        fleet: main
        host: {subfleet: route-home, supervisor_slot: route-host-1,
               supervisor_role: teams/host-supervisor.md, roster: teams/home.yaml}
      - machine: field
        fleet: field
        roster_rows: teams/field-applet.yaml   # no host: the root field fleet hosts it (open question 4)
    saved_on: [home, field]
```

### Remote worker

The field fleet is the remote worker of the home fleet. Concretely:

| Part | Field fleet | Home fleet |
|---|---|---|
| Mode | service | service |
| Supervisor | any model the field can reach | the default chain, advisor on |
| Liaison worker | `onsite-1`: sends reports, does asks on site | optional `field-1`, only when the field is reachable |
| Other workers | none needed | `dev-1`, `review-1` |
| Role text | the liaison role, `fleetmux/agents/peer/liaison.md` | the peer-answer section of the dev role, `fleetmux/agents/peer/dev-peer.md` |

#### Liaison role

The role file ships as an example in `fleetmux/agents/peer/liaison.md`. It says:

1. **Send a report.** Write the report JSON to your own directory. Run
   `fleetmux remote --host <peer> request - --config <peer config> < report.json`.
2. **Wait for the answer.** Run `fleetmux remote --host <peer> result <id> --wait 60 --config <peer config>` until
   the exit code is 0. Exit 3 means pending; run it again.
3. **Never resend after a lost connection.** If the send in step 1 exits 255, do not send again. Go to step 2
   with the same id. If `result` says the id is unknown, send once more with the same id; a
   `duplicate request id` refusal means it was sent.
4. **Do the ask.** On `status: ask`, do what `text` says on this machine. Put the output into the next report.
   Set `to` to `owner`.
5. **Stop on `done` or `blocked`.** Return the text to your supervisor as your result.
6. **Never forward.** Answer a request whose id starts with `peer.` yourself.
7. **Never act on a deletion or a move.** Those go to the human, as everywhere (design.md section 24).

Which CLI can be a liaison: one whose process keeps the user's ssh config. `claude`, `codex` and `pi` do.
`dsh` does not: its pane HOME links only `.gitconfig` and `.npmrc` (`dsh_adapter.HOME_LINKS`).

The worker prompt (`fleetmux/agents/service_worker_v2.md`) says: take plan changes only from the supervisor.
The peer's asks are not plan changes. They are the work the supervisor assigned ("work with home until the
goal is met"). The liaison role says so in one sentence.

#### What the remote worker adds

- `fleetmux result <id> [--wait <s>] [--config <cfg>]`: prints `fx:result:<id>` as one JSON line. Exit 0 found,
  3 pending, 2 unknown id. The `fleetmux results` command prints the whole list, and `--drain` would remove
  other callers' results, so it is not used.
- The liaison role file and the peer-answer role section.

Nothing else. No slot type, no relay process, no new Redis key. The daemon and the parent fleet keep the
processes alive as for any fleet (design.md sections 29 and 31).

### Session environment from an applet

An applet often fixes a network problem by serving a port on loopback, for example a proxy or a tunnel end.
The fleet must then start its agent sessions with extra environment, such as `HTTPS_PROXY`. This section is
the smallest mechanism for that. The code is `fleetmux/session_env.py`; the launch and daemon code only call it.

#### Declaration

The applet entry carries a `session_env` field:

```yaml
applets:
  - name: applet-tunnel-1
    command: ["{python}", "-m", "claude_route.field"]
    session_env:
      port: 17890                                   # loopback port the applet serves
      vars:
        HTTPS_PROXY: http://127.0.0.1:17890
        HTTP_PROXY: http://127.0.0.1:17890
      clis: [claude]                                # only CLIs in fleet.yaml#session_env.verified_clis
```

- **Scope.** It applies to every fleet in the tree that hosts the applet: the root and its sub-fleets. The root
  parses child rosters on start and hot reload (`roster.load_subfleets`). It writes the union of all
  `session_env` entries into its run `config.json` as `session_env`. Child configs get the key at up
  (`child_config`).
- **Shipped allowlist.** `fleet.yaml#session_env.verified_clis` ships as `[claude]`. The block is in `BLOCKS`
  in `fleetmux/fleet_config.py` and in the shipped-block test. A roster that lists another CLI is refused as a
  whole (design.md section 16). Adding a CLI to the list is the act of verifying it (rule 4 below).

#### Routing rules

1. **Always exempt loopback.** The fleet sets `NO_PROXY` and `no_proxy` to the applet's value plus
   `127.0.0.1,localhost,::1`, always. Hooks, local model gateways and local MCP servers never go through the
   proxy.
2. **Clear inherited proxy variables first.** Remove `ALL_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` in
   upper and lower case, then set the declared ones. A session never mixes two routes.
3. **Fail closed.** A session that requires the route never starts while the port is closed. It is held, and
   it starts when the port answers. It never falls back to a direct connection.
4. **Route only verified CLIs.** Claude Code is the only CLI known to honour `HTTPS_PROXY`. Before another
   CLI is added to `verified_clis`, start it through the proxy and see a `CONNECT` to its API host in the
   proxy's log. Node-based CLIs (`pi`, `opencode`, `dsh`) may ignore the variable.
5. **Change only this process.** The variables go into the launched session only. The daemon's own
   environment is never changed (the same rule as channels, design.md section 53).

#### Every launch path that must carry the environment

A pane gets its environment in two steps. First, `Fleet.spawn` and `Fleet.spawn_advisor` pass `env_vars` to the
backend's `spawn_interactive_cli` (`backends/cmux.py`, `backends/fleetmux.py`, `backends/wezterm.py`), which
puts them on the pane's command line. Then the pane runs `fleet.actor()`, which updates `os.environ` per CLI
and execs the CLI. The second step is the last word, so the route is applied there. These are the paths that
start an agent CLI, found by listing every `subprocess` and `exec` call in the package:

| Path | Function | What it does | Route handling |
|---|---|---|---|
| Pane spawn | `Fleet.spawn()`, `Fleet.spawn_advisor()` (`env_vars`) | `PATH` plus per-CLI variables on the pane command line | None. Proxy variables stay off command lines; `actor()` sets them. |
| Every pane: supervisor, workers, resident advisor | `fleet.actor()` | Updates `os.environ` per CLI, then `execvp` | Applies rules 1 and 2 once (`session_env.apply`), after the per-CLI update and before `sandbox.os_wrap`, for slots whose CLI is in `clis`. |
| Claude pane | `fleet.actor()` claude branch | Adds `CLAUDE_ENV` and `claude_hook.ENV` | Covered by the row above. |
| Claude settings file | `fleet.claude_argv()` | Writes `.fleet-settings-g<n>.json` with `env: CLAUDE_ENV (+ claude_hook.ENV)` in two places: the resident advisor branch and the normal branch | Merges the same variables into both `env` dicts (`session_env.settings_env`). |
| pi pane | `pi_adapter.environment()` | `PI_CODING_AGENT_DIR`, bridge variables | Covered by `actor()`; routed only after rule 4. |
| opencode pane | `opencode_adapter.environment()`, `isolation_env()` | Isolated config and data directories | Covered by `actor()`; routed only after rule 4. |
| dsh pane | `dsh_adapter.environment()` | Replaces `HOME`, sets the API key variable | Covered by `actor()`; routed only after rule 4. |
| Codex pane | `fleet.actor_argv()` | Inherits `os.environ` | Covered by `actor()`; routed only after rule 4. |
| One-shot advisor | `advisor.run_claude()` | `subprocess.run(..., env=dict(os.environ, DISABLE_AUTOUPDATER='1'))` from the daemon | Passes the variables explicitly in that `env` (`session_env.environ`). Inline `--settings` in `advisor.argv()` needs no change. |
| Codex queue | `codex_queue.enqueue()`, `codex_queue.capability()` | `env=dict(os.environ, CODEX_HOME=...)` from the daemon | **Not built.** Pass the variables only if `codex` is ever verified and the queue call talks to the API. Unknown today. |
| Channel reader | `channels/reader.py` `run_cli()` | One-shot `claude -p` from the channel actor process, inheriting its environment | Passes the variables explicitly in its `subprocess.run` (`session_env.run_kwargs`). |

#### Fail closed without a rotation storm

Claude is the only routed CLI today, so the Claude paths matter most.

1. **Launch wait (in `actor()`, all CLIs).** Before exec, `session_env.wait` connects to `127.0.0.1:<port>`.
   Closed: it sets `route_wait=1` on the slot hash and keeps waiting, the same way `advice_hold` extends the
   launch deadline. Open: it clears `route_wait` and execs. The daemon treats `route_wait=1` like
   `advice_hold`: no startup timeout, no retry count, no new generation. Every new generation of every CLI goes
   through `actor()`, so this one check covers start, resume, rotation and wall rotation.
2. **No model-chain step (all CLIs).** A refused proxy shows transport errors (`ECONNREFUSED`,
   `connection error`). They match `runtime_v2.OFFLINE_ERROR`. They match no pattern in `fleet.yaml#walls`,
   which lists only the CLIs' own limit wording. So the wall chain (design.md section 23) is not triggered by
   the wording. A test pins this.
3. **Mid-turn loss for Claude.** If the port closes while a Claude session runs, the turn ends with
   `StopFailure`. The paths that may then act are the `StopFailure` re-delivery and the watchdog's stall and
   frozen rotation (design.md sections 24 and 43). Each first checks the port (`session_env.route_closed`);
   closed means hold the slot with `route_wait=1`, start nothing, and re-deliver only after the port answers.
4. **Bridge CLIs only (not built; needed once a bridge CLI is verified).** The offline waits of design.md
   section 35 (`Fleet.network_up`, `Watchdog.link_restart`, `Fleet.startup_retry_tick`) cover only bridge slots
   (pi, opencode, dsh); they exclude Claude and Codex. For a routed bridge slot, `network_up` would answer "the
   route port is open" instead of probing `NETWORK_PROBES`. Then the existing waits hold the slot and spend no
   attempt.

This mechanism replaces the launch shim of the field workflow for sessions the fleet starts. The shim is not
needed for them: the `env` of the fleet's `--settings` file wins over the user's Claude settings (open question 9,
measured on Claude Code 2.1.322).

### Lifecycle details

The steps above cover link up, work, finish and the next venue. This section adds what happens inside them.

#### Work on the home side

1. **Turn-take.** The home supervisor routes the first report, and the advisor picks the model. Later reports
   go straight to the worker named in `owner`. That worker brings the supervisor back with `blocked` or a split
   request when it needs another worker, for example a reviewer.
2. **Build.** The home fleet commits two applets and a workflow entry to one applet repository. The worker that
   writes it has that repository in its roster row's `writable:` list (design.md section 40).
3. **Finish.** The home fleet answers `done` with the workflow name.

#### What `load` does on one machine

1. **Check the commit.** `git -C <repo> cat-file -e <ref>`. Missing: fetch from `source.from`
   (`git fetch <reach entry>:<source.repo>`; git resolves a `~/` path in the home of the user on
   `source.from`), or refuse when that machine does not connect. Then check the checkout. `load` never
   changes it.
   1. **Pinned files.** The pinned files are what this machine's applets run: the roster files the workflow
      names (`roster_rows`, `host.roster`, `host.supervisor_role`) and, for each `applets:` row in them, the
      `command` arguments that name a file or directory under `cwd` (`-m pkg.mod` pins `pkg` or `pkg.py`) and
      `params.file`. Paths outside the repository are not pinned.
   2. **HEAD at `<ref>`.** Accepted.
   3. **HEAD after `<ref>`.** Accepted when `<ref>` is an ancestor of HEAD and no pinned file differs between
      them. Otherwise refuse, list the changed files and print the `git checkout` to run.
   4. **In `<ref>`.** Every pinned file must exist in `<ref>`. An untracked file, or one added after
      `<ref>`, is refused with its name.
   5. **Uncommitted changes.** A pinned file that differs from HEAD in the working tree or the index is
      refused, with the file names. A failed `git diff` refuses too; it never counts as unchanged.
   6. **Scope.** A row pins the files its `command` names, not what they import. Keep an applet's code in
      the package or directory its `command` names.
2. **Check the roster location.** The host fleet's roster file must be inside its run directory. Otherwise
   refuse: the edit would outlive the run. A run started with `start --roster` follows its run copy, so its
   roster is inside the run (open question 5).
3. **Add the rows.** With `host`: add the `supervisor_slot` worker row (if absent) and the `subfleets:` row
   (`{name, supervisor_slot, supervisor_role, roster: <repo>/<host.roster>, autostart: true, mode: service}`).
   Roster hot reload (design.md sections 16 and 29) starts the sub-fleet; the sub-fleet starts the applet.
   Without `host`: add the `applets:` row from `roster_rows` to the root roster (open question 4).
4. **Refuse a running copy.** Before any roster is written: when the host's record
   `<run>/applets/<name>/pid` of an applet to be added names a live process, refuse with its name and pid.
   `load` never stops a process it did not start. Wait for it to stop (run `unload` first if it is loaded).
5. **Wait for a new process.** `fleetmux applet status <name>` until `up`, and the pid in the host's record is
   live and differs from the one read before the write. A state `up` left by an earlier load does not count.
   Otherwise report the reason.

`load` appends the rows to the roster file in place, every other byte kept, and keeps the roster before and
after in `<run>/superpowers-load/<workflow>.json`. Rows from `roster_rows` get `cwd` and `params.file` as absolute
paths and `durable_state: false`. `unload` writes the saved roster back, so the file equals its state before
`load` byte for byte; a roster edited after `load` is refused and the added rows are printed. The applet gets
SIGTERM; a host sub-fleet drains first.

`unload` then waits until each applet it removed has stopped:

1. **Read the pids first.** Before the write, it reads each applet's pid from the host's record (a host
   sub-fleet: the applets in the parent's `fx:subfleet:<name>` mirror, under its `run_dir`). The daemon clears
   the applet's keys when the detach ends.
2. **Wait for the end.** An applet has stopped when its pid is gone and its state is no longer `starting`, `up`,
   `stalled` or `stopping` (`exited`, or absent once detached). A sub-fleet applet is checked by pid only.
3. **Bound.** Per row `detach_wait_s` + `stop_grace_s` + 5 s (a host sub-fleet: `drain_s` + its applets'
   largest `stop_grace_s` + 5 s), or `--wait`. At the bound `unload` fails, names each applet still running with
   its pid and state, and keeps `<run>/superpowers-load/<workflow>.json`. A second `unload` finds the roster
   already restored and only waits.
4. **Limits of a host sub-fleet.** `unload` waits only for the sub-fleet applets it can see in the parent's
   mirror. If the mirror is empty, or the parent hash is already gone (for example on a second `unload`), it has
   nothing to wait for and succeeds. Because the default bound includes `drain_s` (600 s by default), `unload`
   of a `host` item can block for about ten minutes, also when it runs over `remote`.

Without this wait, a `load` right after `unload` could put the row back before the daemon saw it go. The daemon
then cancels the detach, and the old process keeps running with the old code.

#### Restart of the host fleet

A loaded workflow survives a restart of the host fleet until it is unloaded. Without this, a fresh `start` would
build the run copy from the home roster and `start --resume --roster` would overwrite it, and the loaded rows
would be gone.

1. **Fresh start.** `start` of a root fleet reads the run that the fleet home's `latest.json` names before the
   start rewrites it. When that run has stopped and has load records, `start` loads each record again into the
   new run copy, before the daemon starts. `start --resume auto` that starts a new run does the same.
2. **Resume.** `start --resume --roster <file>` rebuilds the run copy. When the copy changed, `start` loads the
   run's own records again. `start --resume` without `--roster` keeps the copy and the records as they are.
3. **What is loaded again.** Only a record whose rows were still in the old copy. A record whose rows were
   already removed (an `unload` that timed out) is not loaded again. Records go in load order: each record's
   `before` is the previous record's `after`.
4. **How.** The same as `load`: the checks of [What `load` does on one machine](#what-load-does-on-one-machine)
   (the workflow is valid, the source checkout passes, the roster with the rows is valid), the rows appended to
   the new copy, and the record written again with `roster`, `before` and `after` of the new copy. `unload`
   then restores the right bytes. The daemon starts the applets like any other roster row. A worker row of a
   `host` item that the home roster already has is not added twice.
5. **A record that fails.** It is not started. `start` prints the reason, and the record moves to
   `<run>/superpowers-load/skipped/<workflow>.json` with the reason. `fleetmux status` lists it under
   `superpowers_load.skipped`. Fix the cause and run `load`, or drop it with `unload` (or delete the file).
   `unload` drops it also when the workflow no longer validates. The next `start` tries it again. A broken
   record, or an inventory that cannot be read, skips the record; it never stops the start.
6. **A task-mode start.** A task fleet hosts no applet. A task-mode `start` in the same fleet home loads
   nothing and keeps every record as skipped, so the next service start loads them again.
7. **The old run.** After the new run is recorded in `latest.json`, `start` removes the records from the old run.
8. **Right after a restart.** The inventory row of the fleet may still name the old run for about 30 s (the
   daemon refreshes it after start). `load` and `unload` therefore hand over from a stopped run to the root
   run that its fleet home's `latest.json` names. Records still in the stopped run are stale (a start of an
   older release did not carry them over). Both commands remove them with a note and treat the workflow as not
   loaded.
9. **Host sub-fleets.** A `host` item adds rows to the root fleet's roster: the supervisor worker row and the
   `subfleets:` row. Both come back with the root, and the root starts the sub-fleet from that row
   (`autostart: true`). The sub-fleet reads its roster from the applet repository, so nothing else needs to be
   carried. A load is never written into a sub-fleet's own run, so only root fleets load records again.

A host sub-fleet needs an adopted **Claude** supervisor. `subfleet.check` refuses to hand over a pi, opencode
or dsh pane, and design.md section 19 allows only official Claude panes. A field machine that cannot reach
Claude therefore cannot start a host sub-fleet. That is why the field applet in the example has no `host`.

#### What persists

| Kept | Where |
|---|---|
| The inventory entries (`machines` sections and the workflow) | `~/.fleetmux/fleet-superpowers/` on both machines |
| The applet source | The applet repository, on each machine that loaded it |
| What a workflow loaded | `<run>/superpowers-load/` of the host fleet's current run, carried to the next run on restart |

Nothing else outside the fleets. Run directories are part of the fleets. Workflow applets keep
`FLEETMUX_APPLET_STATE` empty (see open question 6).

### Example rosters

Field fleet (`field.yaml`; the shipped example is `fleetmux/agents/peer/field.example.yaml`):

```yaml
workers:
  - {name: onsite-1, role: roles/liaison.md}
```

Home dev fleet (`home-dev.yaml`; the shipped example is `fleetmux/agents/peer/home-dev.example.yaml`):

```yaml
advisor: on
workers:
  - {name: dev-1, role: roles/dev-peer.md, writable: [/srv/applets/claude-route]}
  - {name: review-1, role: roles/reviewer.md}
```

Rows the field applet adds to the root field roster (`teams/field-applet.yaml` in the applet repository):

```yaml
applets:
  - name: applet-tunnel-1
    command: ["{python}", "-m", "claude_route.field"]
    params: {file: ../claude_route.yaml, block: field}
    session_env:
      port: 17890
      vars: {HTTPS_PROXY: "http://127.0.0.1:17890", HTTP_PROXY: "http://127.0.0.1:17890"}
      clis: [claude]
```

### Open questions

Each has a recommended default. The design above assumes the default. Rows marked "Implemented as
recommended" are in the current release.

| # | Question | Recommended default |
|---|---|---|
| 1 | One source for hosts: `machines` or `fleet.yaml#remote.hosts`? | `machines`. **Implemented as recommended:** `fleet.yaml#remote` keeps only `default` and `reach_order` (see "Hosts come from the inventory"). |
| 2 | Should the program refuse to forward a request whose id starts with `peer.`? | No. A role rule is enough. Add a program check only if a loop is ever seen. **Implemented as recommended:** the roles forbid it; the program does not refuse it. |
| 3 | Should the home fleet be able to call a field machine that has no inbound reach? | No. Turn-taking covers the scenario. A person who wants it adds an overlay network or their own tunnel and lists it in `reach`. |
| 4 | May a root service fleet host applets directly, without a named sub-fleet? | Yes: a root host, with the root daemon watching the applet as it watches its channel actor. Without it the field machine cannot host its applet, because a host sub-fleet needs a working Claude supervisor. A machine that has Claude may still use a host sub-fleet, as the home side does in the example. **Implemented as recommended:** `docs/applets.md` section 13.1 ("Host", "Watcher"). |
| 5 | `load` refuses a fleet started with `--roster <file outside the run>`. Should `start --roster` copy the roster into the run, like `--kit-fleet`? | Yes. Then every run has a run-local roster, and `load` never edits a source file. **Implemented as recommended:** the copy is `<run>/roster.yaml` (design.md section 16, "Run copy"). |
| 6 | Workflow applets and `FLEETMUX_APPLET_STATE` | Workflow applets keep no durable state. The checklist verifies the directory is empty. If one needs state, list it as a third kept item; deleting it stays a human decision. **Implemented as recommended:** `load` writes workflow applet rows with `durable_state: false`. |
| 7 | Applet loopback listener vs `docs/applets.md` section 9 | Amend section 9: an applet may bind a loopback port that it declares in `session_env`. Nothing else. **Implemented as recommended** in `docs/applets.md` section 9. |
| 8 | Which inventory blocks are hot? | `models` and `quota`, merged in from `discovered.yaml` and `quota.json`: loaded into the fleet's Redis at start and written back every `hot.write_back_seconds`. `machines` and `workflows` stay cold. **Implemented as recommended** (`fleetmux/fleet_superpowers_hot.py`), except that the quota readers (`brake.read_quota` callers) still read `quota.json`, because its writer is outside this repository. |
| 9 | Does `env` in the fleet's `--settings` file win over `env` in the user's Claude settings? | Yes. **Measured on Claude Code 2.1.322:** the user's `~/.claude/settings.json` set `env.HTTPS_PROXY` to one loopback port; `claude --settings <file> -p`, without `--setting-sources`, with a file whose `env.HTTPS_PROXY` named another loopback port, sent `CONNECT api.anthropic.com:443` to the port from the `--settings` file. Measure again if a later Claude Code version changes the settings order. |
| 10 | Reuse one ssh connection for many calls? | Leave it to the user's ssh config (`ControlMaster`). No code. |
| 11 | Unload the home applet automatically when the field leaves? | No. `unload` is an explicit step of link down. **Implemented as recommended:** `unload` never unloads another machine by itself. |
| 12 | Does `start --backend fleetmux` work when started over ssh with no local terminal? | Expected (`fleetmux remote`'s `view` attaches to that backend). Verify once; if not, start the home fleet from `fleetmux remote --host home` with no command and start it inside that page. |

### Implementation checklist

Each line is one check an implementer can run.

- [ ] `fleetmux superpowers refresh --name field` writes `machines.<self>` with `source: self`, and leaves every other entry
      byte-identical.
- [ ] `fleetmux superpowers import -` refuses a section whose name is this machine's.
- [ ] `fleetmux superpowers sync home` leaves a `source: copy` entry for `home` on the field and for `field` on
      the home machine.
- [ ] `fleetmux remote --host home status` tries the reach entries in the fixed order of route kinds and uses
      the first that connects; a dead entry (exit 255) falls through to the next.
- [ ] `fleet.yaml#remote` has no `hosts` key; `tests/test_fleet_config.py` passes.
- [ ] The remote section in design.md (section 55) names `machines` as the host source and keeps the "no port, tunnel or
      VPN" sentence with the added applet clause.
- [ ] `fleetmux result <id> --wait 5` exits 0 with the result, 3 while pending, 2 for an unknown id, and never
      removes an entry from `fx:results`.
- [ ] A report from `onsite-1` reaches the home dev fleet, and its answer comes back with `status` and `owner`.
- [ ] A second report with `to: <owner>` reaches the same home worker session.
- [ ] A request with id prefix `peer.` sent to the field is answered on the field, not forwarded.
- [ ] A roster with `session_env.clis: [pi]` is refused while `verified_clis` is `[claude]`.
- [ ] A Claude session launched with the route has `NO_PROXY` containing `127.0.0.1,localhost,::1`, and no
      inherited proxy variable survives.
- [ ] The generation settings file `env` carries the route variables in both the normal and the resident
      advisor branch.
- [ ] `advisor.run_claude` passes the route variables in its `env`.
- [ ] With the applet port closed, a new Claude generation sets `route_wait=1`, starts no CLI, spends no retry,
      and starts within one tick after the port opens.
- [ ] With the applet port closed during a Claude turn, the `StopFailure` path and the watchdog's stall and
      frozen rotation start no generation and re-deliver nothing; delivery resumes after the port opens.
- [ ] A session failing with `ECONNREFUSED` through the proxy does not move down the model chain.
- [ ] The channel reader's one-shot `claude -p` carries the route variables.
- [ ] On the field machine with no reachable Claude, `load` starts the field applet in the root fleet.
- [ ] `fleetmux remote` never runs a command twice when the first attempt exits 255.
- [ ] `fleetmux superpowers load claude-via-home` refuses when the repo lacks `source.ref` and the source
      machine is unreachable.
- [ ] After `load`, both applets report `up`; after `unload`, both host roster files equal their state before
      `load`.
- [ ] After link down, no fleetmux process, Redis socket or ssh process from this work is left on either
      machine, and `FLEETMUX_APPLET_STATE` of each workflow applet is empty.
- [ ] At a new venue, `load` alone makes a field Claude session work, with no development.
- [ ] Every new file and section is pure ASCII.

## See also

- [applet-development.md](applet-development.md): how the home fleet builds an applet.
- [field-applet-workflow.md](field-applet-workflow.md): the field case.
