---
name: fleet-task-planner
description: "Split a complex task into dispatchable subtasks and hand them to fleetmux / Fleet agents to execute. Use when the user asks to split a task, plan a task, dispatch work in parallel, hand work to fleet agents, schedule execution the way Claude Code Task/subagent does, or needs supervisor+workers collaboration."
---

# Fleet Task Planner — split a task and dispatch it to Fleet agents

Goal of this skill: **the main model only plans, submits, validates and summarizes; the actual execution goes to fleetmux's supervisor/worker agents.**

Triggers:
- "split this task" / "plan and dispatch" / "let the fleet agents do it"
- "like Claude Code's Task/subagent mechanism"
- a large task that needs parallel research, implementation, verification, review
- the user explicitly runs `/skill:fleet-task-planner ...`

Not for:
- Small single-step changes: just do them, do not start a fleet.
- Off-limits areas such as trade execution, real-money paths, production daemon restarts, production Redis writes: stop and report that a human has to handle it.

---

## 0. Principles

1. **Plan first, dispatch second**: never submit while still thinking; first write down the task tree, the worker split, the output paths and the acceptance criteria.
2. **Prompts must be self-contained**: fleet workers do not have the current conversation; the `goal` submitted to the fleet must include background, constraints, prohibitions, inputs and output format.
3. **The fleet executes**: do not do a worker's substantive work in the main session; the main session only orchestrates and validates.
4. **File outputs first**: in task mode results must land in files; the daemon can only check them literally, and the main session then reads the files to check quality.
5. **Conservative concurrency**: FlashNext / local models use `--max-busy 2`; default workers=2 unless the user explicitly asks for more.
6. **Stay out of production**: when an AGENTS.md prohibition applies, stop and report "a human needs to do X".
7. **Reports go to `reports/`**: plans, role files and validation reports are all written under `reports/fleet-task-plans/<run_id>/`.

---

## 1. Preflight

### 1.1 Look for an existing workflow first

From the project root:

```bash
rg -n "<user task keywords>|fleet|cmux-fleet|subagent|Task" memory/ references/ .claude/skills .agents/skills 2>/dev/null || true
```

If the environment has the EverMemOS MCP, search it first as the project rules say:

```text
search_memory(query="<keywords in Chinese>", user_id="claude-code", retrieve_method="hybrid", top_k=5)
search_memory(query="<English keywords>", user_id="claude-code", retrieve_method="hybrid", top_k=5)
```

### 1.2 Check that the fleet is available

```bash
FLEET_BIN=""
if command -v cmux-fleet >/dev/null 2>&1; then
  FLEET_BIN="cmux-fleet"
elif [ -x "$HOME/cmux-fleet/.venv/bin/cmux-fleet" ]; then
  FLEET_BIN="$HOME/cmux-fleet/.venv/bin/cmux-fleet"
elif [ -f "$HOME/Documents/projects/cmux-fleet-pi/cmux_fleet/fleet.py" ]; then
  FLEET_BIN="python3 $HOME/Documents/projects/cmux-fleet-pi/cmux_fleet/fleet.py"
fi
printf '%s\n' "$FLEET_BIN"
```

If it is empty: report "fleetmux not found on this machine, cannot dispatch".

Check the status:

```bash
$FLEET_BIN status 2>/dev/null || true
```

---

## 2. Split the task

Rewrite the user's request as a WBS:

```markdown
# Fleet Task Plan: <short title>

## User goal
<one-sentence restatement of the original goal>

## Boundaries and prohibitions
- Do not touch: <taken from AGENTS.md / the user's requirements>
- Read-only / writable scope: <paths>
- Needs user confirmation first: <high-risk actions>

## Subtasks
| ID | Worker | Goal | Input | Output file | Acceptance criteria |
|---|---|---|---|---|---|
| T1 | worker-1 | ... | ... | worker-1/T1.md | ≥N chars, includes ... |
| T2 | worker-2 | ... | ... | worker-2/T2.md | ≥N chars, includes ... |

## Consolidation
- supervisor consolidates: <how to integrate>
- main-session validation: which files to read, which points to check
```

Save it to:

```bash
RUN_ID="$(date +%Y%m%d-%H%M%S)-fleet-plan"
PLAN_DIR="reports/fleet-task-plans/$RUN_ID"
mkdir -p "$PLAN_DIR/roles"
# write the plan to "$PLAN_DIR/plan.md"
```

---

## 3. Generate role files

Generate minimal role prompts for the fleet's `--roles`. No long methodology: only duties, prohibitions and output format.

`$PLAN_DIR/roles/supervisor.md`:

```markdown
# Your role: Fleet Supervisor

You split the user's goal across the workers, track progress, and integrate the results at the end. Do not do the workers' substantive work yourself.

Hard rules:
- Dispatch only the tasks in the plan; on an off-limits area or missing input, return BLOCKED.
- Every worker's output must be written to its assigned file.
- Write the final summary to `supervisor/final.md`.
- The summary must list each worker's file path, conclusion and open issues.
```

One per worker, e.g. `$PLAN_DIR/roles/worker-1.md`:

```markdown
# Your role: worker-1

You are responsible only for the worker-1 subtask in the plan.

Hard rules:
- First read the files named in the task goal; do not assume any conversation context.
- Do not touch production areas; do not run live-trading / order-placing / production-service-restart commands.
- Write the result to the assigned output file.
- End with: Conclusion, Evidence, Risks, Next steps.
```

### 3.1 Coding tasks: use the devflow role pack (tests first, interfaces frozen first)

When several workers change code in parallel, do not write role files yourself; use the framework's own role pack `fleetmux/agents/devflow/`:

```bash
PACK="<fleetmux source>/fleetmux/agents/devflow"
# add to the start arguments: --roles "$PACK" --roster <a yaml copied from $PACK/roster.example.yaml with the worker count changed>
```

The goal must spell out these four things, or the supervisor cannot apply the pack's gates:
1. **Which worker is `contract`**; in round 0 it delivers first: the skeleton (real signatures, bodies only raise NotImplementedError), `keys.md` (one line per key/field, exactly one writer each), contract tests + one end-to-end test without test doubles, `decisions.md`, `phase0_check.txt`.
2. **The split of the implementation blocks**: which skeleton functions each block owns, which `keys.md` lines it writes, which files it alone may change this round (one file goes to one person per round).
3. **Known structural open questions** (timing / time zone / ownership): ask the user before work starts if possible; if not, tell `contract` to pick the most conservative option and record it in `decisions.md`, and every block uses it.
4. **Acceptance**: add to artifacts `contract/phase0_check.txt` (contains `ROWS_WITHOUT_WRITER: 0`), `contract/integration_check.txt` (contains `CONTRACT_TESTS:`), and each implementation worker's `REPORT.md` contains `CONTRACT_TESTS:`.

Never write in the goal "a worker is writing interface X in parallel; assume your own minimal interface and stub it in tests" — that is exactly the practice this pack removes.

---

## 4. Submit to the fleet

### 4.1 If a fleet is already running

Prefer reusing the existing fleet; avoid several fleets on one machine:

```bash
$FLEET_BIN status
```

If a task is already running, do not overwrite it; report the current status and ask whether to wait for or stop the old task.

### 4.2 Start a task-mode fleet (only if needed)

New starts support only explicitly authorized all-Pi Redis v2 development runs. If the user names Claude/Codex, do not silently switch to Pi; report that this v2 CLI combination is not supported yet. Existing v1 instances are not changed live. The example below uses the FlashNext already configured on this machine:

```bash
$FLEET_BIN start --mode task --workers 2 \
  --supervisor-cli pi \
  --pi-model flashnext/<local-model> \
  --pi-thinking off \
  --worker-cli pi \
  --roles "$PLAN_DIR/roles" \
  --allow-unsandboxed-pi --allow-development-v2 \
  --max-busy 2
```

If the command fails, report the exact error; do not fall back to the v1 file mailbox, the default Codex profile or another model, and do not remove the development gate to get it started. If the user has not authorized unsandboxed Pi / development mode, ask for confirmation first.

### 4.3 Build the task JSON

`$PLAN_DIR/task.json`:

```json
{
  "goal": "<self-contained task description: background, inputs, prohibitions, subtask table, output paths, final summary requirements>",
  "hours": 2,
  "artifacts": [
    {"path": "worker-1/T1.md", "min_chars": 1000, "contains": ["Conclusion", "Evidence", "Risks"]},
    {"path": "worker-2/T2.md", "min_chars": 1000, "contains": ["Conclusion", "Evidence", "Risks"]},
    {"path": "supervisor/final.md", "min_chars": 1200, "contains": ["Summary", "Open issues", "File paths"]}
  ]
}
```

Submit:

```bash
$FLEET_BIN task "$PLAN_DIR/task.json"
```

---

## 5. Monitoring and validation

Monitor:

```bash
$FLEET_BIN status
```

Find the run directory:

```bash
python3 - <<'PY'
import json, os
for p in [os.path.expanduser('~/.fleetmux/latest.json'), os.path.expanduser('~/.cmux-fleet/latest.json'), os.path.expanduser('~/.cmux-fleet-pi-validation/latest.json')]:
    if os.path.exists(p):
        d=json.load(open(p)); print(d.get('run') or d.get('run_dir') or d)
PY
```

Read these when validating:

```text
<run>/workspace/worker-1/T1.md
<run>/workspace/worker-2/T2.md
<run>/workspace/supervisor/final.md
```

The main session must validate a second time:
- do the files exist and meet the length;
- do they cover the user's goal;
- do they break any prohibition;
- is anything fabricated, uncited, or claimed as checked without having been run.

Write the validation report to:

```bash
$PLAN_DIR/validation.md
```

---

## 6. Final reply template

```markdown
Executed with Fleet agents.

- Plan: reports/fleet-task-plans/<run_id>/plan.md
- fleet run: <run_dir>
- worker outputs:
  - worker-1: <path>
  - worker-2: <path>
  - supervisor: <path>
- Main-session validation: PASS / FAIL / BLOCKED

Conclusions: <three to five points>

Open issues / needs human confirmation:
- <if any>
```

---

## 7. Common mistakes

- **Saying it was dispatched without calling the fleet**: not allowed. Either run `$FLEET_BIN task` or report the exact reason it cannot be dispatched.
- **Worker prompts that are not self-contained**: not allowed. They must include background, paths, output format and prohibitions.
- **Letting workers write production config / place orders / restart services**: not allowed. Workers only analyze, propose and verify read-only.
- **Several `date +%s` calls giving inconsistent paths**: generate `RUN_ID` / `PLAN_DIR` once and reuse them.
- **Starting a second fleet on a machine that already runs one**: not allowed. The target machine may already run a scheduled fleet; check status first and report/wait if needed.
- **Leaving content quality to the daemon's check**: not allowed. The daemon only checks literally; the main session must read the files a second time.
