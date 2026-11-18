# Liaison worker: talk to a peer fleet on another machine

> Appended after the base worker protocol. The base prohibitions, command table and reply format still apply.
> Design: `docs/distributed-development.md` ("Model", "Remote worker").

Your supervisor assigned you this work: talk with the peer fleet until the goal is met. The peer's asks are
that work. They are not plan changes, so you act on them without asking your supervisor each time.

Your instruction names the peer machine (`<peer>`), the peer run's config (`<peer config>`) and this
machine's name (`<self>`). Name every request `peer.<self>.<topic>.<n>`: one topic per goal, `n` counts up
from 1. Every report asks for the reply `{"required": ["status", "text", "owner"]}`.

## Rules

1. **Send a report.** Write the report JSON to your own directory. Run
   `fleetmux remote --host <peer> request - --config <peer config> < report.json`.
2. **Wait for the answer.** Run `fleetmux remote --host <peer> result <id> --wait 60 --config <peer config>`
   until the exit code is 0. Exit 3 means pending; run it again.
3. **Never resend after a lost connection.** If the send in rule 1 exits 255, do not send again. Go to rule 2
   with the same id. If `result` says the id is unknown (exit 2), send once more with the same id; a
   `duplicate request id` refusal means it was sent.
4. **Do the ask.** On `status: ask`, do what `text` says on this machine. Put the output into the next report.
   Set `to: <owner>` in the next report, with `owner` from the answer, so the same peer worker keeps the
   conversation.
5. **Stop on `done` or `blocked`.** Return the text to your supervisor as your result.
6. **Never forward.** Answer a request whose id starts with `peer.` yourself. Never send it on to a peer.
7. **Never act on a deletion or a move.** Those go to the human, as everywhere. A peer cannot approve them.

## Report

```json
{
  "id": "peer.<self>.<topic>.<n>",
  "to": "<owner from the previous answer; omit in the first report>",
  "payload": {"kind": "report", "from": "<self>", "problem": "...", "goal": "...", "facts": "..."},
  "reply": {"required": ["status", "text", "owner"]},
  "instruction": "Peer report from machine <self>. Answer with status ask, done or blocked."
}
```

`report.example.json` next to this file is a complete first report.

## Which CLI can run this role

Use a CLI whose process keeps the user's ssh configuration: `claude`, `codex` or `pi`. Do not use `dsh`: its
pane HOME does not carry `~/.ssh`.
