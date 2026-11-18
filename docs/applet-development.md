# Applet development workflow

How to build an applet with fleetmux itself: a host fleet receives the goal, a development sub-fleet builds
the applet, and the finished applet is saved as a workflow that any machine can load.

> **Status:** process document. It uses only commands that exist in the current release, except where a
> step is marked **[pending]**.

## Overview

Three kinds of fleet take part:

| Fleet | Runs on | Role |
|---|---|---|
| **Host fleet** | The machine that will run the applet, or an always-on machine | Receives the goal, splits it into a development sub-fleet, talks to the user and to peers. Writes no code. |
| **Development sub-fleet** | Same machine as the host fleet | Does the whole development process: design, tests, code, review, workflow. |
| **Field fleet** (optional) | A second machine | Runs the steps that only that machine can run, and reports back. See [distributed-development.md](distributed-development.md). |

The host fleet keeps running when the field machine goes offline. Only the steps that need the field
machine wait.

The output is:

- the applet source in its own repository;
- a workflow in the `workflows` block of `fleet-superpowers` on each machine that uses the applet.

## When to use it

Use this workflow for any new applet, and for any change to an applet that adds a feature.

Do not give an applet to a single worker to build alone. A single session skips the test-first step and the
review, and nobody sees the plan.

## Steps

1. **Install the release.** Install the same fleetmux release on every machine that takes part. See
   [Dependencies](#dependencies).
2. **Create the applet repository.** One private repository per applet, laid out as in
   [applet-team-guide.md](applet-team-guide.md) section 4.1.
3. **Write the brief.** One file: goal, rules, acceptance tests, what is out of scope. The acceptance tests
   must be commands that print a clear pass or fail.
4. **Start the host fleet.** Use service mode, the fleetmux backend and `--supervisor-split`:

   ```sh
   FLEETMUX_HOME=~/.fleetmux/fleets/<name> FLEETMUX_STATE=~/.fleetmux/fleets/<name>/state \
   fleetmux start --lifecycle service --mode service --backend fleetmux --supervisor-split \
     --allow-development-v2 --name <name> \
     --supervisor-cli claude --worker-cli claude \
     --roster ~/.fleetmux/fleets/<name>/roster.yaml --roles ~/.fleetmux/fleets/<name>/roles
   ```

   - Give every worker row `writable:` with the applet repository.
   - Write role paths in the roster as absolute paths.
5. **Open the window on the host machine's screen.** Run this on the host machine, from its own terminal
   or over ssh:

   ```sh
   fleetmux gui --runtime ~/.fleetmux/fleets/<name>/state/fleetmux/fleetmux
   ```

   Check that the window is visible. On macOS: `lsappinfo visibleProcessList | tr ':' '\n' | grep fleetmux`.
6. **Hand over the goal.** Send one request that points at the brief:

   ```sh
   fleetmux request - <<'EOF'
   {"id": "<name>.goal.1", "payload": {"kind": "goal", "brief": "<path to brief>"},
    "instruction": "Build the applet in the brief. Split it into a development sub-fleet.",
    "reply": {"required": ["status", "text"]}}
   EOF
   ```

7. **Split.** The host supervisor dispatches the goal with `split` (tier one). The proposal has at least
   three parts: one per applet, and a review. The program starts a task sub-fleet; the chosen worker
   becomes its supervisor.
8. **Develop in the sub-fleet.** The sub-fleet supervisor plans and dispatches. Its workers:
   1. write a failing test for each change and keep the failing output;
   2. change the code until the test passes;
   3. run only the applet repository's tests.
9. **Test on site.** Steps that need another machine go to the field fleet as peer asks
   ([distributed-development.md](distributed-development.md), "Message set"). The sub-fleet writes each ask as a
   command that can be run as is.
10. **Review.** A reviewer worker in the sub-fleet checks the code against the brief's acceptance tests.
    The report starts with `Type: functional review`. A P0 or P1 finding comes with a command or test that
    fails.
11. **Write the workflow.** Add the `workflows` entry (schema in [distributed-development.md](distributed-development.md), "`workflows` entry schema").
    `fleetmux superpowers check` must report no problems.
12. **Load on every machine.** `fleetmux superpowers load <workflow>`. The work is done only when the applet
    is loaded and `fleetmux applet status <name>` says `up` on every machine the workflow names, and the
    applet's tab shows `<name> up` in the daemon area of each host fleet's window. `up` in the CLI alone does not count.
    Over ssh, without a screen, check the tab with `fleetmux tabs`: the applet's row is in the `lower` group and its
    title is `<name> up`. `fleetmux tabs read <name>` prints the tab's text.
13. **Save.** `fleetmux superpowers save <workflow.yaml> --also <peer>` stores the workflow on both machines.
14. **Stop.** `fleetmux superpowers unload <workflow>`, then stop the fleets. No process, listener or file
    outside the fleets is left.

## Rules

1. **Stay inside the framework.** Every mechanism an applet needs uses what fleetmux provides. A launchd item, a
   cron job, a resident script outside the fleet, or a background process that bypasses the daemon is outside the
   framework and is not allowed. If one is unavoidable, it needs the operator's explicit approval and must be
   recorded as a stopgap that names the framework fix. When the framework lacks a capability, extend the
   framework instead of working around it.
2. **Run under the daemon, visible in the fleet window.** An applet runs under its host's daemon and is shown in
   the daemon area's tab bar with its concrete state ([applet-interface.md](applet-interface.md) section 2.6).
   Acceptance of an applet includes seeing its tab and state in the fleet window. `fleetmux applet list` showing
   `up` alone does not count.
3. **Split every applet build into a sub-fleet.** The host supervisor never dispatches an applet build to a
   single worker.
4. **Write the test first.** Every change starts with a test that fails.
5. **Keep the host fleet free of code.** The host supervisor plans, dispatches and rules. It answers peers
   and the user.
6. **Show the work.** The host fleet's window is open on the host machine's screen while it works.
7. **Do not call models at run time.** Models help build the applet. A loaded applet is deterministic.
8. **Do not change the machine.** No edits to shell profiles, ssh configuration, system settings or agent
   CLI settings. Every run-time process is a child of the applet.

## Dependencies

The same rules apply to the fleetmux release and to every applet.

1. **Use one resolver.** Find a program on the interactive login shell's `PATH` first, then on the process
   `PATH`. Use the same code for the dependency check, `start` and applets. If the login shell cannot be
   read, use the process `PATH` and print one line that says so.
2. **Record absolute paths.** Write the path of each program that was found to the install record.
3. **Never install or upgrade for the user.** Report what is missing and stop.
4. **Treat agent CLIs as the user's.** pi is required. codex, claude and opencode are used when they are
   present and usable, and skipped otherwise. A missing or old one never blocks a release.

## Known issues

| Symptom | Cause | Workaround |
|---|---|---|
| `start` exits 2: "data under the old name ... is not read any more" | Data from the old product name is not migrated, and other fleets still use it. | **[pending]** fix in the start-up guard. Until then, create an empty `~/.fleetmux/runs` and `~/.fleetmux/state`. |
| `start requires --lifecycle <mode>` | The flag is required. | Pass `--lifecycle service`. |
| `transport v2 requires --allow-development-v2` | The flag is required. | Pass it. |
| `role file not found` | Role paths are resolved against the roster file. | Write absolute role paths. |
| A program is reported missing although it is installed | The check read only the process `PATH`. | **[pending]** one resolver (see Dependencies). |
| `superpowers load` refuses: "these applet files changed since" | The checkout is after `source.ref` and a file the applets run differs from `source.ref`. | Save the workflow again with `source.ref` set to the commit that holds those files, or check out `source.ref`. |
| `superpowers load` refuses: "is still running (pid N)" | A process of that applet still runs, for example one whose row is still detaching. `load` never stops a process it did not start. | Wait for it to stop. If the workflow is loaded, run `superpowers unload <workflow>` first; it returns only after the process has ended. |
| `superpowers unload` fails: "still running after Ns" | The daemon did not stop the applet within the bound (open leases, a slow stop, or a daemon that is not running). | Run `superpowers unload <workflow>` again to wait again, or pass a larger `--wait`. |
| A session starts while the far proxy is down | `session_env` checks only that the local port is open. | **[pending]** add an identity check to `session_env`. |

## Checklist

- [ ] The host fleet was started with `--supervisor-split`, in service mode, on the fleetmux backend.
- [ ] The host fleet's window was visible on the host machine's screen.
- [ ] The applet was built by a sub-fleet with a supervisor, developers and a reviewer.
- [ ] Every change has a recorded failing test.
- [ ] The review passed against the brief's acceptance tests.
- [ ] The applet is loaded and `up` on every machine the workflow names.
- [ ] After load, the applet's tab is in the daemon area's tab bar of each host fleet's window, titled
      `<name> up`, and its header shows the phase, heartbeat age and open leases. Check the applet tab with
      `fleetmux tabs` (group `lower`, title `<name> up`) and its header with `fleetmux tabs read <name>`.
- [ ] After unload, the applet's tab is gone from the window (`fleetmux tabs` no longer lists it).
- [ ] No launchd item, cron job or other scheduler entry, and no process outside the fleet, was added for the
      applet.
- [ ] The workflow is saved on every machine that uses it.
- [ ] After unload and stop, nothing is left outside the fleets.

## See also

- [applet-team-guide.md](applet-team-guide.md): the applet project layout and its run-time team.
- [applets.md](applets.md) and [applet-interface.md](applet-interface.md): the applet design and contract.
- [distributed-development.md](distributed-development.md): peer fleets, `fleet-superpowers` and workflows.
- [field-applet-workflow.md](field-applet-workflow.md): the field case, with two applets and two machines.
