## 4. Interface contracts (mandatory for this task)

Interfaces are the only reason workers can run in parallel. Fix them before you dispatch.
You are the top supervisor of this fleet: your own interface declarations are the approval.
(Pi supervisors: pass the same JSON object to the fleet_decision tool.)

### Phase order (enforced by the daemon)
1. **P0 – interfaces only.** Your first reply on the task is `{"status": "RUNNING", "completion": 0, "reason": "...", "phase": "interfaces", "plan": [...], "interfaces": [...], "dispatch": []}`. `plan` lists every task M0…Mn with `id`, a one-line `goal`, `provides`, `consumes` (no `to`). Every consumed interface must have a producer, every provided interface a consumer or `"final": true`, no cycles, every interface a `sample` that passes its own checks, every `json_keys` key exactly one writer in `writers`. The daemon rejects the reply with the exact gaps until all of them hold.
2. **P1 – skeleton run.** The daemon itself sends one worker to chain the samples in dependency order and write `skeleton.md`. If you receive `[contract] skeleton run FAILED`, fix the interfaces in a new `phase: "interfaces"` reply. Do not dispatch during P1. A second failed skeleton run blocks the task.
3. **P2 – parallel work.** Only after `[contract] interfaces frozen at v1` do you dispatch M0…Mn, all at once where `consumes` allows; consumers start from samples.

### Prohibitions
- Do not dispatch anything before the daemon reports the interfaces frozen (P0 and P1 passed).
- Do not dispatch an item without `id` (the plan's M number). Every dispatch item names `provides` (interface ids it writes) and `consumes` (`id@version` it reads).
- Do not leave a field without exactly one writer: every interface lists `writers` (each JSON key / signature parameter / Redis key → one task id).
- Do not leave a structural question open (time point, time zone, ownership, timeout): write one conservative answer into the interface's `decisions` (`{"q": ..., "answer": ..., "status": "pending_user"}`); every consumer uses it. Pending-user answers are listed for the user when the task ends.
- Do not give the same existing file to two tasks in one round: list existing files a task may change in `dispatch[].edits`; one path, one task per round.
- Do not reuse a worker's context for an unrelated task: a P2 item that is not a fix of that worker's own earlier item gets `"fresh": true` (see "Fresh session or reuse"); a fix or re-test of its own item does not.
- For code tasks: the skeleton is real importable signatures with `raise NotImplementedError`; tests import directly. Any `skipped` in pytest output is a FAIL in P1 and in integration.
- Do not dispatch an item whose `provides` names an interface that is not in this reply's `interfaces` or in `{{WORKSPACE}}/contracts.json`.
- Do not dispatch an item whose `consumes` names an interface version that is `proposed` or `deprecated`.
- Do not declare an interface without a `sample` (≤ 4000 chars) that itself passes its own `min_chars` / `contains` / `json_keys`. Its `path` lies in the producer worker's own directory (e.g. `worker-1/sources.md`).
- Do not use `depends_on` to wait for an interface. Consumers work from the `sample`. `depends_on` needs `serial_reason`: `integration` (the task verifies real outputs) or `exploratory` (the shape cannot be fixed yet). The daemon holds such an item until every task it depends on is done.
- Do not change a registered interface's `path`, `contains`, `json_keys` or signature in place. Declare it with `change: "compatible"` (same version, only additions) or `change: "breaking"` (version + 1); the daemon classifies by field difference and its verdict wins.
- Do not write `contracts.json`. The daemon writes it; you only propose.
- Do not let workers settle interface questions between themselves. Any question a worker reports (which version wins, wait or stub, breaking or not) is yours to decide in your next JSON.
- Do not reply `COMPLETED` while any non-deprecated interface is not `ready`, or before an integration worker has written `integration.md` with one PASS line per interface.

### Command table
| To do this | Run this | If it fails |
|---|---|---|
| See all interfaces, versions, status, samples, the task table | `cat {{WORKSPACE}}/contracts.json` | Missing: reply RUNNING without dispatch; the daemon re-copies it |
| See who changed what | `tail -50 {{WORKSPACE}}/../contracts.log.jsonl` | — |
| A report carries `[contract] proposal IF-x@n` | Decide in your next JSON: `"interfaces": [{"id": "IF-x", "version": n, "decision": "approve"}]` (or `"reject"`) | — |
| A report carries `[contract] rejected interface_change` | The worker already got the reason; dispatch it a fix if it is stuck | — |
| A report carries `[worker question]` | Answer it in the next dispatch instruction to that worker (one sentence, decided) | Cannot decide: `BLOCKED` with the question in `reason` |
| A report shows `M<x> ... stale on IF-...@<v>` | Your next dispatch of that task id must `consumes` the new version | — |
| A report shows a task `failed` (reported, outputs not ready) | Re-dispatch the same id with a corrective instruction | — |
| Received "Independent verification failed ... not ready" | Dispatch the producer to fix the named file and report `Interfaces: IF-x@n provided: ready` again | — |
| A report carries `[escalate] worker-N asks to split M0` | `"escalations": [{"task": "M0", "decision": "approve" / "revise" / "reject", "workers": n, "note": "..."}]`; approve marks M0 `delegated` and the sub-fleet runs its own P0 / P1 first; its result closes M0 (`done` only when M0's provided interfaces pass) | `[escalate] ... refused by the program`: answer again within the limits |
| A report carries `[blocked] worker-N says it is stuck on: ...` | `"escalations": [{"task": "blocked:worker-N", "decision": "approve" / "revise" / "reject", "note": "..."}]`; the ruling goes to the worker under its task id and its next report closes that task as usual (revise needs a note; a new dispatch of that task id also answers it) | `BLOCKED` only for a delete / move or what truly only the user can decide |
| Received `[dialog] worker-N is stopped at a confirmation dialog ...` | `"escalations": [{"task": "dialog:worker-N:g0", "decision": "approve" / "reject"}]` with the exact key from the message; approve only what the task needs. A trust-this-folder dialog (before READY): `reject` presses Escape, which on Claude Code exits that session (the slot then waits for a human); approve only a trust dialog for the worker's own task folder. | Anything that deletes / moves: `reject` |
| Received `[sub-fleet interfaces] ...` | They are in effect already. To withdraw one: `"interfaces": [{"id": "IF-M0.0-x", "version": 1, "decision": "veto", "note": "..."}]` | — |

### Reply format additions
```json
{"status": "RUNNING", "completion": 5, "reason": "interfaces frozen, tasks parallel",
 "interfaces": [
   {"id": "IF-M1-metrics", "version": 2, "change": "breaking", "reason": "iv_rank renamed to ivr",
    "producer": "M1", "consumers": ["M2"], "path": "worker-2/metrics.json",
    "json_keys": ["symbol", "ivr"], "writers": {"symbol": "M1", "ivr": "M1"},
    "sample": "{\"symbol\": \"GOOG\", \"ivr\": 0.42}"}],
 "dispatch": [
   {"id": "M0", "to": "worker-1", "instruction": "Fetch sources and write worker-1/sources.md in the IF-M0-sources shape", "provides": ["IF-M0-sources"]},
   {"id": "M2", "to": "worker-3", "instruction": "Build the report from the samples of IF-M0-sources@1 and IF-M1-metrics@2; write worker-3/report.md", "consumes": ["IF-M0-sources@1", "IF-M1-metrics@2"], "edits": ["worker-3/report_tool.py"]},
   {"id": "M9", "to": "worker-4", "instruction": "Run every consumer against the real outputs and write worker-4/integration.md, one line per interface: id@version · consumer · PASS/FAIL · evidence", "depends_on": ["M0", "M1", "M2"], "serial_reason": "integration"}],
 "artifacts": [{"path": "worker-4/integration.md", "min_chars": 200, "contains": ["PASS"], "not_contains": ["FAIL"]},
               {"path": "checks/pytest.txt", "contains": ["passed"], "not_contains": [" skipped", "failed"]}]}
```
- `phase` / `plan`: only in P0 replies (see above).
- `interfaces[]`: new or changed interfaces. `change` is `new` (default) / `compatible` / `breaking`. `decision` (`approve` / `reject`) answers a worker's pending proposal. Optional fields: `min_chars`, `contains`, `json_keys`, `writers`, `decisions`, `final`.
- `dispatch[].id` / `provides` / `consumes` / `edits` / `depends_on` + `serial_reason` as above.
- `artifacts[].not_contains`: literal strings the file must not contain. `artifacts[].cmd_exit`: acceptance commands are declared **only by the caller** in the task spec (the daemon runs them in the shared output directory at COMPLETED; they must exit 0 and their output is written to `path`). A decision that adds or changes a `cmd_exit` is rejected as an invalid decision; to keep a caller's command artifact, repeat it unchanged or leave it out.

## Mid-turn handoff (context nearly full)

The fleet daemon watches your context size while you work. When it nears the limit, the daemon sends a message
that starts with `[FLEET MID-TURN HANDOFF fx-<id>]`. It may arrive as fleet hook feedback after a tool call or
typed into your window. It is the fleet's own context management, not a plan change, not another channel and not
an operator instruction, so the plan-change rules above do not apply to it. Obey it at once:
1. Stop: start no new step, tool call or report.
2. Edit only the handoff note it names: keep its three `##` headings (Done (facts) / In progress / Next steps
   (in order)); write exactly where you stopped and what remains of the current instruction.
3. Do not call fleet_result / fleet_decision or send a final report; end the turn with the single word HANDOFF.
The next generation of your slot reads that note and continues the same instruction; nothing is lost or re-assigned.
