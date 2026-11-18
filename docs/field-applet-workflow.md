# Field workflow

Solve a problem once on site. Reuse the fix anywhere.

> **Status:** design. See [Requirements](#requirements) for the parts that
> are not merged yet.

## Overview

This workflow uses two machines:

| Machine | Role |
|---|---|
| **Field machine** | The laptop you have with you. It observes and acts. |
| **Home machine** | An always-on machine with a stable fleet. It thinks and builds. |

When the two fleetmux instances connect, they work as one team. The field
machine can use any model it can reach, even a weak one. The home fleet
does the reasoning and the development.

The output is two applets, one per machine. A `workflows` entry for them is saved in
`fleet-superpowers` on both machines, so next time you load the applets and skip
development.

## When to use it

Use this workflow when:

- something fails on the field machine because of where you are, and
- the home machine is not affected.

## Steps

1. **Connect.** The field machine connects to the home machine and starts
   fleetmux there. The connection details come from the `machines` block of
   the local `fleet-superpowers` inventory.
2. **Report.** The field machine tells the home fleet two things: the
   problem and the goal.
3. **Investigate.** The field machine acts as a remote worker of the home
   fleet. The home fleet asks for facts or actions on site. The field
   machine does them and reports back.
4. **Build.** The home fleet splits the work into a development sub-fleet,
   which builds two applets: one for the home machine, one for the field
   machine. See [Applet development workflow](applet-development.md).
5. **Load.** Each fleet hot-loads its applet. Check the result against the
   goal from step 2.
6. **Save.** Register a `workflows` entry in `fleet-superpowers` on both machines: where
   the applets are, how to load them, and when they apply.
7. **Stop.** Stop both fleets. Nothing is left behind.

Next time, run step 1, load both applets from `fleet-superpowers`, and you are
done.

## Rules

1. **Write nothing outside the fleet.** An applet runs only child
   processes. It does not edit shell profiles, CLI settings, system proxy
   settings, or another program's state files.
2. **No system services.** Do not use launchd, systemd, or cron. The host
   fleet starts, restarts, and stops the applet.
3. **Keep only two things.** The `fleet-superpowers` entry and the applet source
   code.
4. **No model calls at run time.** Models help build the applet. A loaded
   applet is deterministic.
5. **Keep the connection simple.** It is one entry in the inventory. Do
   not build anything else for it.

## Example: Claude is unreachable on public Wi-Fi

**Problem:** the field machine cannot reach the Claude API on a cafe
network.

**Goal:** Claude sessions in fleetmux work again.

### Home applet

Runs one proxy process (for example, mihomo) that listens on loopback
only. The applet:

- starts the process and owns it;
- checks health by asking the proxy's control endpoint for its version,
  so that another program on the same port does not pass as healthy;
- restarts the process if it exits;
- applies the outbound route from its own configuration on every start.

### Field applet

Runs one SSH tunnel. The tunnel forwards a loopback port on the field
machine to the proxy port on the home machine. The field machine needs no
proxy of its own.

### Launch shim

The fleet starts Claude through a small shim. The shim:

1. finds the real `claude` binary, skipping its own directory in `PATH`;
2. clears inherited proxy variables (`ALL_PROXY`, `HTTP_PROXY`,
   `HTTPS_PROXY`, `NO_PROXY`, upper and lower case);
3. tests `https://api.anthropic.com` through the local tunnel port, and
   exits with an error if the test fails;
4. sets `HTTPS_PROXY`, `HTTP_PROXY`, and
   `NO_PROXY=localhost,127.0.0.1,::1` for this process, then runs `claude`
   with `exec`.

The shim never falls back to a direct connection.

### Existing proxy settings

The `env` block in the user's Claude settings file overrides the shim's
variables. Do not edit that file. On each start, the shim writes a
temporary settings file without the proxy fields and passes it:

```sh
claude --settings "$TMP_SETTINGS" --setting-sources project,local
```

### Scope

- Only Claude sessions started in fleetmux use this route.
- Claude Code honours `HTTPS_PROXY`. Test other agent CLIs before you
  route them the same way.

## Checklist

- [ ] The connection is one entry in the `fleet-superpowers` inventory.
- [ ] Each machine has one applet, and every run-time process is its child.
- [ ] After both fleets stop, nothing remains outside the fleets.
- [ ] The `workflows` entry is registered in `fleet-superpowers` on both machines.
- [ ] At a new location, the applets load from `fleet-superpowers` and work with
      no development.

## Requirements

| Part | Used in |
|---|---|
| `fleet-superpowers` inventory with connection details for each machine | Step 1 |
| A link between two fleetmux instances | Steps 1-2 |
| A reply path from the home supervisor, and a worker slot on another machine | Step 3 |
| Applet hot-load | Step 5 |
| Saved workflows in the `workflows` block | Step 6 |

## See also

- [Distributed development with multiple fleets](distributed-development.md): the steps, the link between two fleets, the `fleet-superpowers`
  inventory, session environment from an applet, and saved workflows.
