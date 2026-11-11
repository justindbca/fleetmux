# Dynamic agent routing phases 1–5

Dynamic routing extends the existing supervisor/worker/daemon architecture. The registry, deterministic router, planner, assignment metadata, original FIFO queues, task/service protocols and private Redis remain in place. Phase 5 adds explicitly opted-in runtime respawn at an idle delivery boundary. Metadata-only behavior remains the default. Redis transport v2 in design §11 is separate: its foundation modules exist, but runtime wiring is not implemented.

## Cards and planning

Cards live in `fleetmux/agents/` alongside role prompts. JSON needs only the standard library; YAML works if PyYAML is already installed. Required fields are `id`, `display_name`, `cli`, and nonempty `capabilities`. Optional fields are `model`, `profile`, `role_file`, and nonnegative cost/latency/risk ranks (lower is preferred).

| Profile | CLI | Model/default | Capabilities |
|---|---|---|---|
| `flashnext-coder` | Pi | `flashnext/<local-model>` | code-edit, test-fix, planning |
| `claude-reviewer` | Official Claude Code CLI, directly | `opus` (role `reviewer.md`, independent read-only reviewer) | code-review, safety-audit, planning |
| `claude-coder` | Official Claude Code CLI, directly | `opus` (role `worker.md`); preferred for implementation when fleet `worker_cli` is `claude` | code-edit, test-fix, planning |
| `researcher` | Codex | Existing worker/shared Codex profile and its model | web-research, planning |

Researcher stays on Codex to preserve the existing research runtime and configuration. Optional fields retain existing defaults when absent. A profile ID is never an agent identity: `worker-1` becomes `worker-1.1` on respawn, never `flashnext-coder`.

The router matches English/Chinese capability keywords. Review/safety prefers low risk; code/test work prefers low latency/cost; the ID breaks ties deterministically. The code-change planner emits implementation followed by independent review. Assignment uses configured slots in plan order, reuses slots when capacity is exhausted, and keeps repeated profiles on their first slot. Dependencies remain a supervisor constraint, not a second scheduler.

Offline inspection never starts a fleet:

```bash
python3 -m fleetmux.fleet agents list
python3 -m fleetmux.fleet agents match "fix failing pytest"
python3 -m fleetmux.fleet plan "implement dynamic router" --workers worker-1,worker-2
```

All output is JSON; `--json` is optional. Without `--workers`, plan output omits assignments.

## Task submission and authorization

A metadata-only task can use:

```json
{
  "goal": "implement a router fix and tests",
  "auto_agents": true,
  "apply_profiles_live": false,
  "hours": 2,
  "artifacts": []
}
```

`auto_agents` and `apply_profiles_live` must be JSON booleans, default false. Optional `agent_registry` must be an existing directory; only `Path.expanduser()` is applied, without shell/environment/glob expansion. Registry cards are loaded when planning is enabled. The existing `submit()` stores `dispatch_plan` and `profile_assignments` in the task hash and includes them in the supervisor message. Operator artifacts are preserved.

Supervisor dispatch still uses stable slot/label values for `dispatch.to`. Instructions should include `Recommended profile: <agent_id>` and `Subtask: <subtask_id>`. A marker must match a current task assignment for that slot and, if supplied, that subtask. Without a recommendation, candidates must agree on exactly one profile; prose mentioning profile IDs has no authority. Unknown cards, malformed/conflicting markers, unauthorized profiles and ambiguity are rejected. Capabilities come from the registry, never an assignment fallback.

Every item in a decision is checked before any item is queued. Rejections use the existing supervisor correction/three-invalid-replies path. The original text stays in FIFO order. Only the head instruction updates next-intent metadata; a later item cannot overwrite it. Immediately before delivery, the daemon revalidates the original instruction against the current task ID, assignment, registry and opt-in gates. It does this again after READY or normal handoff. Validation failures block rather than deliver. Live instructions without an authorized profile cannot downgrade themselves to metadata-only.

## Explicit live opt-in

Both conditions are required:

1. Start the fleet with `--allow-profile-live-switch`, which sets config `allow_profile_live_switch: true`.
2. Submit an auto-agent task with `apply_profiles_live: true`.

Permission in config alone does not activate switching. A task requesting live switching without config permission is rejected before task mutation. Live tasks also require `auto_agents: true` and valid assignments. Neither flag accepts strings or numbers. The older reserved config `apply_profiles_live` never substitutes for the per-task opt-in. Service requests retain their existing protocol and do not opt into live profile assignment.

Generate request files with a JSON serializer and validate the actual file before submission:

```bash
python3 - <<'PY'
import json
with open('/tmp/fleet-live-task.json', 'w') as stream:
    json.dump({'goal': 'implement dynamic router', 'auto_agents': True,
               'apply_profiles_live': True, 'hours': 1, 'artifacts': []}, stream)
PY
python3 -m json.tool /tmp/fleet-live-task.json >/dev/null
# Only after successful validation, target your explicitly selected DEV config:
python3 -m fleetmux.fleet task /tmp/fleet-live-task.json --config /path/to/dev/config.json
```

## Runtime mapping and isolation

`profile_launch.py` resolves cards into a frozen launch description; `profile_runtime.py` remains a pure authorization/transition module without Redis, spawning or process control.

- Pi: `model` overrides `pi_model`; a model must be explicit. Pi >= 1.1.0 and `allow_unsandboxed_pi` remain mandatory. An isolated per-run Pi home links only the selected Pi auth/models files. No Claude subscription credentials or proxy are involved.
- Claude: `model` overrides `worker_model`, defaulting to `sonnet`. The existing actor directly execs the configured official `claude` binary with its Stop hook. Hook settings are generation-specific, so an old process cannot reread a new generation's callback arguments.
- Codex: `profile` overrides the worker/shared profile, default `fleet`; `model`, when present, is passed as `--model`. Codex home isolation and profile-file requirements remain. Profile names must be simple config names, not paths.
- `profile` is a Codex config profile. Specifying it for Pi or Claude is an explicit unsupported error; the daemon does not invent a corresponding option.
- `role_file` is resolved relative to the registry, must exist and must remain inside it after symlink resolution. Its contents are appended to the base fleet role (identical packaged base content is not duplicated). Missing optional fields use existing defaults.

The daemon checks binary availability and local prerequisites before retirement. It passes the selected model unchanged; it never retries with a different CLI/model. Model/auth/backend failures must produce a valid READY or hit startup failure/timeout. READY does not independently attest a remote provider's actual weights. Unknown CLI values fail closed in live mode.

## Metadata semantics

| Field | Meaning |
|---|---|
| `desired_profile` | Authorized profile intent for the queued instruction |
| `advisory_profile` | First accepted advisory profile; carries Phase 4's former metadata meaning |
| `runtime_profile` | Profile used for this generation's spawn, possibly still starting; initially empty for default runtimes |
| `current_profile` | READY-confirmed applied profile of this generation; empty during spawn/failed startup (preflight failure preserves the old runtime). No longer advisory metadata |
| `runtime_ready` | Whether this generation passed READY |
| `runtime_launch` | Frozen profile ID, CLI/model/profile overrides and role contents |
| `profile_state` | `none`, `pending`, `switching`, `ready`, `applied-live`, `applied-metadata`, `blocked` |
| `profile_capabilities` | JSON array from the authorized card |
| `profile_reason`, `profile_task` | Explanation and associated task ID |

Metadata-only mode applies the first advisory at idle (or initial starting-before-delivery). A different advisory remains pending; busy/handoff states only record pending intent. It does not change `current_profile`, `runtime_profile`, CLI/model, role, generation, label, credentials or process. A runtime selected by an earlier live task is retained; opting out does not reset that runtime.

`switching` means a controlled respawn is needed or in progress, not active. `ready` means this generation passed startup but the queued instruction still needs revalidation. `applied-live` requires a matching READY runtime and successful delivery-time revalidation. `applied-metadata` never proves a CLI/model switch.

## Controlled respawn and failure gates

At an idle worker delivery boundary:

1. Reauthorize the queue head and compare desired versus the READY runtime. The same applied profile needs no respawn. Busy, handoff, starting and dead workers cannot switch. After Escape/stall abort, an unconfirmed idle state blocks switching; Codex native children defer it.
2. Resolve/preflight launch configuration before retiring anything. Save the old terminal snapshot and profile-switch history. Keep the same slot, increment generation, and register the successor label as another alias of that slot.
3. Call existing `spawn()` with the actor's generation pinned in argv. A successor tab is created, but its actor waits behind `launch_ready=0` before executing any model CLI. Per-generation config snapshots prevent callback/runtime reinterpretation.
4. Move the successor into the old worker pane using `tab_into`. Retire the old process tree/tab using `kill_pane`, require success and disappearance, then open the launch gate. The actor exits without launching if the gate stays closed for 60 seconds or its generation is retired/blocked.
5. Leave the queued task untouched. Accept only successful, non-aborted, exact `READY` from the new generation; old-generation callbacks are ignored. All CLI startup paths now require READY. `profile_ready_timeout` defaults to 180 seconds, must be finite and positive, and still applies with `stall_timeout=0`.
6. Mark the runtime READY, then revalidate the queue on the next idle delivery. If the card's launch fields or role contents changed since spawn, block. Only then pop/send the instruction with an applied-runtime prefix. Future context handoff retains the chosen runtime.

Spawn/layout/retirement failures revoke the launch gate, disable the generation, and attempt cleanup of the successor tab. Failed command delivery returns its created surface ID so cleanup can target it. A failed old retirement records `retired_pane` for inspection. There is no automatic rollback or fallback. Late READY cannot revive a disabled generation.

Startup failure/timeout blocks the task and leaves the tab available for diagnosis. `dead/blocked` disables dispatch; it is not proof that every OS process has stopped. The existing task failure lifecycle clears queues; repair and resubmit. This is a conservative first implementation, without automatic recovery after ambiguous process retirement.

## Dev validation (2026-10-11)

The code was changed on `feat/pi-agents`, preserving the pre-existing uncommitted Phase 1–4 work. No production install, existing fleet, trading system or external repository was modified.

Required checks:

```bash
python3 -m pytest -q tests
python3 -m fleetmux.fleet plan "implement dynamic router" --workers worker-1,worker-2
python3 -m fleetmux.fleet agents match "fix failing pytest"
git diff --check
```

Results: **243 passed**; plan maps implementation to `worker-1` / `flashnext-coder` and review to `worker-2` / `claude-reviewer`; match selects `flashnext-coder` / `test-fix`; diff whitespace check passes. Tests exercise both opt-in gates, unsafe/aborted boundaries, generation fencing, held launch, queued delivery until READY, stale/duplicate callbacks, registry drift, three CLI mappings, roles, startup/cleanup failures, unchanged labels, context handoff and Pi supervisor operator-input events. Fake infrastructure is not real supervisor TUI acceptance.

The following isolated two-worker dev deployment command was attempted:

```bash
FLEETMUX_HOME=/tmp/cmux-fleet-phase5-dev FLEETMUX_STATE=/tmp/cmux-fleet-phase5-dev/state python3 -m fleetmux.fleet start --workers 2 --supervisor-cli pi --worker-cli pi --pi-model flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP --allow-unsandboxed-pi --allow-profile-live-switch --runs-dir /tmp/cmux-fleet-phase5-dev/runs --codex-home /tmp/cmux-fleet-phase5-dev/codex-home
```

It exited **1**, before provisioning: `run this inside a cmux terminal: cmux only accepts processes started in its windows`. Direct read-only `cmux ping` returned `Failed to connect to socket at ~/.local/state/cmux/cmux.sock (Operation not permitted, errno 1)`. Installed CLI discovery succeeded; Pi reported `1.1.0`.

Closest existing integration fallback, with temporary private Redis and fake cmux:

```bash
python3 tests/pi_smoke.py --model flashnext/<local-model>
```

It exited **1**: Redis 8.6.0 reported `Failed opening Unix socket: bind: Operation not permitted`. The smoke now checks early Redis exit/socket creation so this failure is reported without lengthy connection retries. The initial retrying attempt was interrupted through its cleanup path; the subsequent attempt failed promptly. Temporary Redis processes and directories were cleaned. No model task was launched; this fallback did **not** reach live-profile switching.

Real dev deployment, real supervisor TUI input and mixed Pi/Claude/Codex model switching remain **unverified due to sandbox socket permissions**. Before production deployment, run these in an authorized cmux terminal with isolated state/home, exercise actual worker retirement/new READY and operator input, verify model/auth availability and failure cleanup, and obtain independent review. No independent review, merge or production deployment is claimed.
