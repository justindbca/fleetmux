## 5. Peer notices (Pi / Codex workers: fleet broker)

When you propose a change to an interface you provide (or add a new one), tell its consumers yourself, in the same turn: an `UNAPPROVED` notice when you propose it, and a `READY` notice once the real file is written and verified. The daemon's `[contract]` messages stay the authority; your notice only gives consumers a head start.

Workers talk to each other only through the fleet broker. The daemon validates the notice, rejects any target outside this fleet, appends it to the target's inbox, copies the supervisor and logs it. The supervisor never answers these; everything for the supervisor goes in your report.

Rules:
- Send only to consumers listed for that interface in `{{WORKSPACE}}/contracts.json`.
- One way: do not reply, thank, discuss or forward. The only allowed reply is one notice answering a notice that literally contains `blocking question:`.
- Facts only (what changed, where the sample is, that a file is ready). Never settle a decision with another worker. Unsure: `Question for supervisor: <one sentence>` in your report; keep working on the part that is not blocked.
- No interface change, no notice. Progress, chatter and requests for help go to the supervisor in your report.

| To do this | Run this | If it fails |
|---|---|---|
| Send a notice to one consumer | Write the fixed notice to `./notice.txt`, then `{{FLEET_PEER}} --to <worker-name> --iface <IF-id> --text-file ./notice.txt` | Error text on stdout: fix and resend once; then report it verbatim under `Peer notices:` |
| Send a notice to every consumer of an interface | `{{FLEET_PEER}} --to consumers:<IF-id> --iface <IF-id> --text-file ./notice.txt` | same |
| A notice arrives in your inbox (`[peer] ...`) | `UNAPPROVED`: keep working on the current version, you may prepare `./stubs/`; `READY`: switch input from `./stubs/` to the registered `path` and re-run. Do not reply; cite it under `Peer notices:` in your next report | — |

Fixed notice (at most 600 characters, nothing else in the file):
```
[iface-notice] IF-M0-sources 1 -> 2 | breaking | UNAPPROVED
changed: column `title` renamed `headline`; new column `lang`
sample: {{WORKSPACE}}/worker-1/stubs/IF-M0-sources.v2.md
spec: {{WORKSPACE}}/contracts.json
from: worker-1 (M0)
```
Line 1 = `[iface-notice] <id> <old> -> <new> | new|compatible|breaking|ready|correction | UNAPPROVED|APPROVED|READY` (a new interface is `0 -> 1`; READY repeats the current version, e.g. `2 -> 2 | ready | READY`). Only the producer may send `READY`; at most 20 notices per hour.

Report line (replaces `Peer notices: none` when you sent or received any): `Peer notices: sent: IF-x@n kind to <worker> ; received: IF-x@n kind from <worker>`.

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
