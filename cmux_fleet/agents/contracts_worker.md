## 4. Interface contracts (mandatory for this task)

Your instruction names the task id (`[M2]`) and the interfaces you `provide` (write) or `consume` (read). Their exact shape is in `{{WORKSPACE}}/contracts.json`. Follow it to the letter.

### Prohibitions
- Do not write a provided file in any shape other than the registered one: same `path`, every `contains` string, every `json_keys` key.
- Do not read another worker's real output for an interface whose `status` is not `ready` and for which you have not received `[contract] ready`. Copy its `sample` to `./stubs/<id>.v<version>` and work from that.
- Do not change a registered interface yourself (path, fields, signature, format). Propose it (`interface_change` object at the end of your report) and keep working on the current version until the daemon tells you it is approved.
- **MUST verify before announcing ready**: `wc -c <path>` and `grep -n "<each contains string>" <path>` (or `python3 -c` for `json_keys`) all pass, or you do not announce.
- **Never settle a decision with another worker** (which version wins, wait or stub, breaking or compatible). Unsure about anything: write `Question for supervisor: <one sentence>` in your report, keep working on the part that is not blocked, and wait for the supervisor's instruction.
- Do not write a field whose `writers` entry names another task; do not edit an existing file that is not in your instruction's `edits` list (need it: report `Question for supervisor:`).
- Use the answer in the interface's `decisions` even if you would choose differently; disagree only via `Question for supervisor:`.
- Code tasks: never use `pytest.importorskip` or hard-coded worktree paths; a test run with any `skipped` is not a pass.
- Code tasks: run only the tests for what you changed (touched modules' test files + new tests, red → green); never the full suite for an intermediate delivery or a fix — the full suite runs once, at the delivery gate / end of the batch.
- Do not start real work on any task before the interfaces are frozen; the skeleton run is the only work allowed before that.

### Command table
| To do this | Run this | If it fails |
|---|---|---|
| See the shape of an interface and who consumes it | `cat {{WORKSPACE}}/contracts.json` | Missing: report "contracts.json missing" and stop |
| Start consuming before the producer is done | Copy its `sample` to `./stubs/<id>.v<n>`; build against it | — |
| Propose a new or changed interface | Write the new sample to `./stubs/<id>.v<n>`; end your report with exactly one JSON object: `{"interface_change": {"id": "IF-M0-sources", "version": 2, "change": "breaking", "reason": "...", "path": "...", "contains": [...], "sample_file": "stubs/IF-M0-sources.v2.md"}}` (only the fields that change; `sample_file` is relative to your own directory and required for `breaking`) | `[contract] your interface_change was rejected: ...` arrives: fix and propose again; keep working on the current version |
| Announce your real output is ready | Verify (`wc -c`, `grep -n` each `contains`), then report `Interfaces: IF-x@n provided: ready` | Verification fails: fix the file first; never announce |
| Received `[contract] approved IF-x@n` | Switch when your next instruction names `@n`; finish the current batch first | — |
| Received `[contract] ready IF-x@n` | Switch input from `./stubs/` to the registered `path`, re-run, write it in your report | Real file does not match the shape: report `Question for supervisor:` with the mismatch |
| Received `[contract] not ready IF-x@n` | Producer: fix the file, verify, announce again. Consumer: keep using the stub | — |
| Your task is too big for one worker, or the daemon sent `[FLEET LONG-RUNNING fx-<id>]` (finish the current step first, write no handoff note) | End your report with the JSON object `{"escalate": "<why>", "proposal": [{"id": "M0.0", "instruction": "...", "output": "m0.0.md", "kind": "<what kind of work>"}], "workers": 2}` (ids = your task id + `.<n>`; the same single object as any `interface_change`); keep Done / Evidence for what you did so far, then wait | Refused or rejected: do the task yourself; asked to revise: send a corrected object |
| Assigned the P1 skeleton run (instruction contains `[skeleton]`) | Read `{{WORKSPACE}}/contracts.json`; in dependency order feed each upstream `sample` into its consumer (code: a minimal stub function per interface and one call chain; research: walk the outline using the samples); write `./skeleton.md`, one line per interface: `IF-x@1 · producer → consumer · PASS/FAIL · evidence` | A sample cannot be consumed as written: FAIL with the exact mismatch; never edit the samples yourself |

### Report format additions
Add these lines after `Blocked:` (write `none` when empty); an `interface_change` object, if any, is the very last thing in the report:
```
Interfaces: <id@version provided: ready/not ready> ; <id@version consumed: stub/real>
Peer notices: none
Question for supervisor: <one sentence, or none>
```
(Pi workers: put `interfaces`, `question` and `interface_change` as extra keys of the fleet_result object.)

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
