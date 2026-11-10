// Drives cmux_fleet/dsh_bridge.mjs with a fake dsh-TUI cordis ctx and a fake bridge (dsh_fake_bridge.mjs).
// argv: <plugin> <scenario>. Prints {frames, ...} as JSON on the last line.
import { readFileSync, existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join, dirname } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { createRequire, syncBuiltinESMExports } from "node:module"

const [plugin, scenario] = process.argv.slice(2)
const dir = mkdtempSync(join(tmpdir(), "dshharness-"))
const log = join(dir, "frames.jsonl")
const pkg = join(dir, "profile", "node_modules", "@deepseek-ai", "dsh-tools")
mkdirSync(pkg, { recursive: true })
writeFileSync(join(pkg, "package.json"), JSON.stringify({ name: "@deepseek-ai/dsh-tools", type: "module", main: "index.js" }))
writeFileSync(join(pkg, "index.js"), "export const defineTool = (t) => t\n")
const SID = "2d746058-3749-4cbc-8426-01643fe1a614", SID2 = "9c1f1a8e-0000-4000-8000-000000000002"
Object.assign(process.env, {
  DSH_FAKE_LOG: log, DSH_FAKE_REJECT_STARTUPS: scenario === "startup_retry" ? "1" : "0", DSH_FAKE_LATE_SETTLE: scenario === "late_settle" ? "1" : "0",
  CMUX_FLEET_DSH_BRIDGE: JSON.stringify([process.execPath, join(dirname(fileURLToPath(import.meta.url)), "dsh_fake_bridge.mjs"), "cfg.json", "worker-1", "0"]),
  CMUX_FLEET_DSH_SLOT: "worker-1", CMUX_FLEET_DSH_GEN: "0", CMUX_FLEET_DSH_MODE: "task", CMUX_FLEET_DSH_MODEL: "wb/deep-1",
  CMUX_FLEET_DSH_WINDOW: "400000", CMUX_FLEET_DSH_SESSION_DIR: join(dir, "sessions"), CMUX_FLEET_DSH_STARTUP: "Reply READY only.",
  CMUX_FLEET_DSH_LOG: join(dir, "plugin.log"), CMUX_FLEET_DSH_API_KEY: "sk-secret",
})
const require = createRequire(import.meta.url)
const cp = require("node:child_process"), realSpawn = cp.spawn
let bridge, bridgeEnv
cp.spawn = (...a) => { bridgeEnv = a[2]?.env; bridge = realSpawn(...a); return bridge }
syncBuiltinESMExports()

const handlers = new Map(), tools = new Map(), followups = [], disposers = []
const mkAgent = (id) => ({ id, ctx: { tools: { register: (t) => tools.set(t.name, t) } }, followup: (m) => followups.push({ agent: id, ...m }) })
const agent = mkAgent(SID), roots = [agent]
const session = { id: SID }
const ctx = {
  baseUrl: pathToFileURL(join(dir, "profile") + "/").href,
  agents: { roots: () => roots },
  on: (name, cb, opts) => handlers.set(name, cb),
  get: (name) => name === "tokenMeter" ? { measure: (s) => ({ totalTokens: s === session ? 1234 : -1 }) } : undefined,
  effect: (fn) => disposers.push(fn()),
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const frames = () => existsSync(log) ? readFileSync(log, "utf8").trim().split("\n").filter(Boolean).map((l) => JSON.parse(l)) : []
const kinds = () => frames().map((f) => f.kind)
const until = async (pred, ms = 3000) => { const end = Date.now() + ms; while (Date.now() < end) { if (pred()) return true; await sleep(10) } return false }
const ev = (type, data, s = session) => handlers.get("session/event")(s, { type, data })
const claim = (id, turn, a = agent) => handlers.get("agent/inbox/claimed")({ message: { id }, turn, agent: a })
const say = (turn, text, src = { kind: "model", provider: "wb", model: "deep-1" }, extra = []) =>
  ev("assistant/message", { turn, step: 1, message: { role: "assistant", content: [{ type: "text", text }, ...extra], source: src } })
const fixture = (frame) => bridge.stdin.write(JSON.stringify({ kind: "fixture", frame }) + "\n")
const delivery = (id, purpose = "task") => ({ run_id: "r", slot: "worker-1", generation: 0, session_id: SID, message_id: id,
  delivery_id: "d-" + id, stream_id: "1-0", fence: 1, body: { text: "do " + id, title: "t", mode: "task", purpose } })
const REPORT = { result: { done: ["wrote ok.txt"], evidence: ["ok.txt 2 bytes"], not_done: "none", blocked: "none" } }

async function startupTurn(turn = 1) {
  await until(() => followups.length >= 1)
  ev("turn/start", { turn }); claim(followups.at(-1).id, turn)
  // measured: the TUI's narration line and the reply arrive as separate text parts
  say(turn, "⏵ 启动检查", undefined, [{ type: "text", text: "READY" }]); ev("turn/end", { turn, reason: { kind: "completed" } })
  await until(() => kinds().includes("startup") && kinds().includes("availability"))
}
async function deliver(id, turn, purpose) {
  const n = followups.length
  fixture({ kind: "delivery", delivery: delivery(id, purpose) })
  await until(() => followups.length > n)
  ev("turn/start", { turn }); claim(followups.at(-1).id, turn)
  await until(() => kinds().includes("started"))
}
const settle = async (turn, reason = { kind: "completed" }) => { ev("turn/end", { turn, reason }); await until(() => kinds().includes("settled") || kinds().includes("hold")) ; await sleep(50) }

const mod = await import(pathToFileURL(plugin).href)
mod.apply(ctx)
const out = { scenario }
await until(() => kinds().includes("init"))
if (scenario !== "startup_retry") await startupTurn()
if (scenario === "happy") {
  out.startup_text = frames().find((f) => f.kind === "startup").text
  await deliver("m1", 2)
  out.delivered_text = followups.at(-1).content[0].text
  out.tool = await tools.get("fleet_result").execute(REPORT, { concludeTurn: () => { out.concluded = true } })
  await settle(2)
  out.bridge_has_key = "CMUX_FLEET_DSH_API_KEY" in (bridgeEnv || {})
  out.transcript = existsSync(join(dir, "sessions", SID + ".jsonl"))
} else if (scenario === "late_settle") {
  await deliver("m1", 2)
  await tools.get("fleet_result").execute(REPORT, { concludeTurn: () => {} })
  await settle(2)
  const idle = () => frames().filter((f) => f.kind === "availability" && f.idle === true).length
  out.idle_before = idle()
  fixture({ kind: "completed", message_id: "m1" })
  await until(() => idle() > out.idle_before)
  out.idle_after = idle()
} else if (scenario === "manual") {
  ev("turn/start", { turn: 2 }); claim("human-1", 2)
  await until(() => frames().filter((f) => f.kind === "availability").length >= 2)
  fixture({ kind: "delivery", delivery: delivery("m2") })
  await until(() => kinds().includes("reject_delivery"))
  try { await tools.get("fleet_result").execute(REPORT, {}); out.tool = "accepted" } catch (e) { out.tool = String(e.message) }
  ev("turn/end", { turn: 2, reason: { kind: "completed" } }); await sleep(200)
} else if (scenario === "mixed") {
  await deliver("m3", 2); claim("human-2", 2); await settle(2)
} else if (scenario === "aborted" || scenario === "error") {
  await deliver("m4", 2)
  await settle(2, scenario === "aborted" ? { kind: "aborted" } : { kind: "error", message: "upstream 429" })
} else if (scenario === "model_lock") {
  await deliver("m5", 2); say(2, "x", { kind: "model", provider: "other", model: "m" }); await settle(2)
} else if (scenario === "handoff") {
  await deliver("m6", 2, "handoff")
  try { await tools.get("fleet_result").execute(REPORT, {}); out.tool = "accepted" } catch (e) { out.tool = String(e.message) }
  say(2, "HANDOFF"); await settle(2)
} else if (scenario === "approval_delete" || scenario === "approval_other") {
  await deliver("m7", 2)
  const del = scenario === "approval_delete"
  const args = del ? { command: "rm -rf /outside/build" } : { file_path: "/outside/x.txt", content: "x" }
  say(2, "", undefined, [{ type: "tool-call", id: "call_1", name: del ? "bash" : "write", arguments: JSON.stringify(args) }])
  let release
  const person = new Promise((r) => { release = r })
  const answer = handlers.get("approval/request")({ agent, toolName: del ? "bash" : "write", callId: "call_1", reason: "escalate", signal: undefined }, () => person)
  await until(() => kinds().includes("permission"))
  fixture({ kind: "permission_reply", request_id: "call_1", decision: "once" })
  if (del) { await until(() => kinds().includes("permission_refused")); release("rejected") }
  out.answer = await answer
  await until(() => kinds().includes("permission_resolved"))
} else if (scenario === "rebind") {
  const other = mkAgent(SID2); roots.push(other)
  handlers.get("agent/created")({ agent: other, source: "new" })
  await until(() => kinds().includes("shutdown"))
} else if (scenario === "startup_retry") {
  await startupTurn(1)
  ev("turn/start", { turn: 2 }); claim("typed-nudge", 2); say(2, "READY"); ev("turn/end", { turn: 2, reason: { kind: "completed" } })
  await until(() => frames().filter((f) => f.kind === "startup").length === 2)
  await sleep(100)
}
out.frames = frames()
for (const d of disposers) if (typeof d === "function") d()
console.log(JSON.stringify(out))
setTimeout(() => process.exit(0), 100)
