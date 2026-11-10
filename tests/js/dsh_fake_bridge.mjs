// Fake agent_bridge.py for the dsh bridge plugin harness: logs every frame to DSH_FAKE_LOG, answers like the
// bridge, and echoes "fixture" frames back as bridge frames (delivery / permission_reply / ui).
import { appendFileSync } from "node:fs"
import { createInterface } from "node:readline"
const log = process.env.DSH_FAKE_LOG
let startups = 0, active = null
const out = (f) => process.stdout.write(JSON.stringify(f) + "\n")
createInterface({ input: process.stdin }).on("line", (line) => {
  const f = JSON.parse(line)
  appendFileSync(log, JSON.stringify(f) + "\n")
  if (f.kind === "fixture") {
    if (f.frame.kind === "delivery") active = f.frame.delivery.message_id
    out(f.frame); return
  }
  if (f.kind === "shutdown") process.exit(0)
  let status = "recorded"
  if (f.kind === "init") status = "registered"
  if (f.kind === "startup") status = (++startups <= Number(process.env.DSH_FAKE_REJECT_STARTUPS || 0)) ? "rejected" : "ready"
  if (f.kind === "settled" && process.env.DSH_FAKE_LATE_SETTLE === "1") status = "pending"  // a busy daemon (design §47)
  else if (f.kind === "settled") { if (active) out({ kind: "completed", message_id: active }); active = null; status = "committed" }
  out({ kind: "reply", call_id: f.call_id, answer: { status } })
})
