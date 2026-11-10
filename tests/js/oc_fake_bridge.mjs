// Fake agent_bridge.py for the opencode plugin harness: logs every frame, answers init / startup.
import { appendFileSync } from "node:fs"
import { createInterface } from "node:readline"
const log = process.env.OC_FAKE_LOG
createInterface({ input: process.stdin }).on("line", (line) => {
  const f = JSON.parse(line)
  appendFileSync(log, JSON.stringify(f) + "\n")
  const status = f.kind === "init" ? "registered" : f.kind === "startup" ? "ready" : "recorded"
  if (f.call_id && f.kind !== "shutdown") process.stdout.write(JSON.stringify({ kind: "reply", call_id: f.call_id, answer: { status } }) + "\n")
  if (f.kind === "shutdown") process.exit(0)
})
