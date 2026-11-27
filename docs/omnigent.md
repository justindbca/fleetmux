# Use fleetmux from Omnigent (`fleetmux acp`)

## Overview

`fleetmux acp` serves the running root fleet as an Agent Client Protocol (ACP) agent on stdin and stdout.
Omnigent can register it as a custom ACP agent. A `fleetmux` entry then appears in Omnigent's new-session
picker and in `omni run --harness acp:fleetmux`.

Each message you send in that session becomes one job of the running fleet (docs/design.md section 58).
The job runs in its own task-mode sub-fleet. Its progress streams back into the conversation. The last
paragraph of the reply is the program's acceptance result: the fleetmux daemon checks the declared artifacts,
not the model's own report.

    Omnigent --ACP over stdio--> fleetmux acp --CLI--> running root fleet (daemon, supervisor, workers)

The ACP process only submits and watches jobs. It never starts or stops a fleet. Closing the Omnigent session
does not stop a job that was already submitted.

## Requirements

- fleetmux with the `acp` and `cancel` commands, and `fleetmux` on the PATH of the Omnigent server.
- A running root fleet on the same machine, started by you in a terminal. A service fleet is the usual choice:

      fleetmux start --lifecycle service --allow-development-v2

- At least one worker of that fleet on the official Claude Code CLI. Only such a worker can supervise a job's
  sub-fleet.
- Omnigent with custom ACP agents (`acp.agents` in `~/.omnigent/config.yaml`). Tested with Omnigent 0.22.0.

## Steps

1. **Start a fleet** in a terminal (see Requirements), or check that one runs with `fleetmux status`.

2. **Register the agent.** Add this entry to `~/.omnigent/config.yaml`:

   ```yaml
   acp:
     agents:
       - name: fleetmux
         command: fleetmux acp
   ```

   These two keys are enough. Two more keys are recommended:

   ```yaml
   acp:
     agents:
       - name: fleetmux
         command: fleetmux acp
         inject_system_prompt: false
         omnigent_mcp: false
   ```

   - `inject_system_prompt: false` keeps Omnigent's system prompt out of the first message. Without it,
     `fleetmux acp` removes that prompt itself (see Limits).
   - `omnigent_mcp: false` skips Omnigent's tool relay, which the fleet does not use.

3. **Set fleetmux variables in the command if you use them.** The ACP process gets only a small set of
   environment variables: HOME, PATH, TERM, TMPDIR, locale, proxy and SSL variables, among others. Your
   shell's other variables do not reach it. Omnigent's runner drops them before `env_passthrough` could
   forward them. With the default layout (`~/.fleetmux`, and fleets under `~/.fleetmux/fleets/`) nothing is
   needed: the running fleet is found from HOME. If your fleet runs with `FLEETMUX_HOME`, `FLEETMUX_STATE`,
   `FLEETMUX_CONFIG_DIR` or `FLEETMUX_JOIN_ROOT` set to other places, put them in the command. The command is
   split like a shell line but runs without a shell, so use `env`:

   ```yaml
         command: env FLEETMUX_HOME=/path/to/fleet-home FLEETMUX_JOIN_ROOT=/path/to/fleet-home fleetmux acp
   ```

4. **Start a session.** Pick `fleetmux` in the Omnigent app, or run:

   ```bash
   omni run --harness acp:fleetmux -p "Survey the three most-cited papers on X and write a comparison"
   ```

5. **Send a goal.** The message text is the job's goal. To set job keys, end the message with a fenced JSON
   block:

   ````text
   Survey the five vendor APIs and write a comparison report.

   ```json
   {"hours": 3, "workers": 2,
    "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}
   ```
   ````

   - Only a block at the very end of the message counts. A JSON block anywhere else stays part of the goal.
   - Allowed keys: `hours`, `artifacts`, `budget`, `lifecycle`, `workers`, `supervisor_profile`,
     `worker_profiles`, `name`, `roster`, `roles`, `deliver`, `tree`, `by`. They have the
     meanings given in the README section "Jobs: submit work to a running root fleet". With the decompose
     switch on, a `lifecycle: dev_task` job needs `tree` (run `fleetmux tree check <file>` first).
   - `goal` is not a key: the message text is the goal.
   - With no `artifacts` (or `"artifacts": []`), the job is checked against a default artifact:
     `worker-1/result.md` with at least 300 characters. The goal gets one added sentence that asks for this
     file.
   - A job with `roster` must declare `artifacts`. The default path assumes a worker named `worker-1`.

6. **Read the result.** The reply shows:
   - one line each time the job's state changes (queued, running in a sub-fleet, why it waits);
   - a heartbeat about every 90 seconds while nothing changes;
   - at the end, the start of `worker-1/result.md` when that file was written;
   - the program check as the last paragraph: `OK`, `BLOCKED` or `FAILED`, the outcome and reason, failed
     checks, and the run and workspace directories.

7. **Cancel.** Stopping the turn in Omnigent sends `session/cancel`. Only that job's sub-fleet is stopped. The
   fleet and its other jobs keep running. From a terminal, the same is:

   ```bash
   fleetmux cancel job-0123456789
   ```

## Example

A session with no fleet running:

    $ omni run --harness acp:fleetmux -p "hello"
    ERROR: no fleetmux root fleet is running on this machine, so the goal was not submitted. Start one in a
    terminal, then send the goal again:
      fleetmux start --lifecycle service --allow-development-v2

A session with a running fleet (shortened):

    [fleetmux] job job-0123456789 accepted by the running fleet .../config.json. Progress follows; ...
    [fleetmux] job-0123456789: queued - waiting: no idle worker
    [fleetmux] job-0123456789: running in sub-fleet M3 on worker-2

    worker-1/result.md:
    ...

    fleetmux program check: OK (job job-0123456789)
    outcome: completed
    run: .../sub/M3
    workspace: .../sub/M3/workspace

`fleetmux result job-0123456789` prints the same result as JSON at any later time.

## Limits

- **No fleet is started for you.** Without a running root fleet, the reply is an error with the `fleetmux
  start` command to run.
- **Queued jobs cannot be stopped at once.** A job that has no sub-fleet yet is registered for cancellation by
  its job id. The ACP process retries every 15 seconds and stops the job once its sub-fleet starts. If the
  Omnigent session closes first, the registration is lost and the job runs. Stop it by hand with `fleetmux
  cancel <job id>`; `fleetmux cancel <job id> --wait 600` keeps retrying for up to ten minutes.
- **Closing the session does not stop a job.** The job runs to its end and its result stays available through
  `fleetmux result <job id>`.
- **A dead fleet daemon ends the turn.** If the root fleet's daemon stops while a job is open (no heartbeat for
  3 minutes and no process holds its lock, or no heartbeat for 15 minutes), the reply ends with an error that
  gives the `fleetmux start --allow-development-v2 --resume <run>` command. The job stays recorded in the fleet;
  after the restart, read it with `fleetmux result <job id>`.
- **The prompt wrapper is version-dependent.** Omnigent puts its system prompt, and after a restart the
  replayed conversation, in front of the first message of a session. `fleetmux acp` removes both by matching
  the wrapper of Omnigent 0.22.0. If a later Omnigent changes that wrapper, the whole text may become the
  goal. Set `inject_system_prompt: false` to avoid this.
- **Run it unsandboxed.** Sessions from the picker or from `--harness acp:fleetmux` run without a sandbox. If
  you write your own agent YAML for this harness, set `os_env.sandbox.type: none`. The fleet needs your home
  directory, its private Redis socket and the agent CLIs' logins.
- **The transcript is not the fleet's panes.** To watch individual agents, use `fleetmux attach` or `fleetmux
  web`.
- **Not supported:** `session/load`, `session/set_model` and `session/set_config_option` (answered with
  "not supported"), and images in prompts.
