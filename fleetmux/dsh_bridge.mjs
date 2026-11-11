// fleetmux bridge plugin for the dsh-TUI (design §41 v1): a cordis plugin row inserted by fleet's patch,
// running in the TUI's own process. Port of pi_extension_v2.ts: spawns agent_bridge.py (JSONL over piped
// stdin/stdout) and talks to the daemon only through it. Never writes stdout (the TUI owns the terminal);
// its log is $FLEETMUX_DSH_LOG.
//
// Measured on dsh 0.2.1-rc.5 + dsh-tui 0.18.0 (plan §6.1):
// - the TUI creates its root agent at runtime: ctx.agents.roots() / 'agent/created' {agent, source, signal};
//   agent.id is the DSH session id.
// - agent.followup(frozen user message with our id) starts a turn; 'agent/inbox/claimed' {message, turn, agent}
//   names which message ids a turn took; 'session/event' (session, ev) with ev.type 'turn/end' and
//   ev.data.reason.kind in completed / aborted / error / blocked / max-tokens ends it.
// - 'approval/request' is a waterfall (req, next); req = {agent, toolName, callId, reason, displayReason, signal}.
//   The command / path come from the same call id's tool-call in the preceding 'assistant/message'.
// Rules (as opencode's plugin): a fleet turn settles only when it claimed exactly our id. Mixed with other
// input -> hold (reported, never success, never re-sent). Turns without our id are manual and never settle.
import { appendFileSync, mkdirSync } from "node:fs"
import { spawn } from "node:child_process"
import { createInterface } from "node:readline"
import { randomUUID } from "node:crypto"
import { join } from "node:path"
import { pathToFileURL } from "node:url"
import { permissionClass } from "./opencode_plugin/lib.js"

const env = process.env
const COMMAND = JSON.parse(env.FLEETMUX_DSH_BRIDGE || "[]")
const SLOT = env.FLEETMUX_DSH_SLOT || ""
const GENERATION = Number(env.FLEETMUX_DSH_GEN ?? NaN)
const SERVICE = env.FLEETMUX_DSH_MODE === "service"
const MODEL = env.FLEETMUX_DSH_MODEL || ""
const WINDOW = Number(env.FLEETMUX_DSH_WINDOW || 0) || null
const SESSION_DIR = env.FLEETMUX_DSH_SESSION_DIR || ""
const STARTUP = env.FLEETMUX_DSH_STARTUP || ""
const LOG = env.FLEETMUX_DSH_LOG || ""
const KEY_ENV = "FLEETMUX_DSH_API_KEY"
const ARG_KEYS = ["command", "file_path", "path", "pattern", "url"]

export const name = "fleetmux-bridge"  // dsh_adapter.PLUGIN_ID
export const inject = ["agents", "tools"]

export function apply(ctx) {
  const log = (k, v) => { if (LOG) try { appendFileSync(LOG, JSON.stringify({ t: Date.now(), k, v }) + "\n") } catch {} }
  if (!COMMAND.length || !COMMAND.every((p) => typeof p === "string")) throw new Error("Fleet dsh bridge command missing")
  if (COMMAND.at(-2) !== SLOT || Number(COMMAND.at(-1)) !== GENERATION) throw new Error("Fleet dsh slot/generation mismatch")
  if (!MODEL.includes("/") || !SESSION_DIR || !STARTUP.trim()) throw new Error("Fleet dsh environment incomplete")
  mkdirSync(SESSION_DIR, { recursive: true })

  let defineTool = null, agent = null, sid = null, child = null
  let ready = false, shutdown = false, unhealthy = false, startupRetry = false
  let active = null                // the bridge delivery being worked on
  let openTurn = null              // turn number between turn/start and turn/end
  let candidates = [], ctxUsed = null, sequence = 0
  const ours = new Map()           // our followup message id -> {kind: "startup"|"delivery", env?}
  const turns = new Map()          // turn -> {kind: "startup"|"delivery"|"manual"|null, fleet, foreign: [], text: [], problem}
  const calls = new Map()          // tool call id -> {name, args}
  const pending = new Map()        // bridge call id -> {resolve, reject, timer}
  const perms = new Map()          // reported approval id -> {req, cls, rule: resolve of the fleet ruling}

  const transcript = () => join(SESSION_DIR, sid + ".jsonl")
  const activity = (o) => { if (sid) try { appendFileSync(transcript(), JSON.stringify({ t: Date.now(), ...o }) + "\n") } catch {} }
  const metadata = () => ({ thread: sid, transcript: transcript(), ctx_used: ctxUsed, ctx_window: WINDOW })

  function fault(reason) {
    if (unhealthy) return
    unhealthy = true; log("fault", String(reason)); activity({ fault: String(reason || "bridge unavailable") })
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("Fleet bridge unavailable")) }
    pending.clear()
  }
  function send(kind, fields = {}) {
    if (!child || shutdown || !sid) return Promise.reject(new Error("Fleet bridge is not running"))
    const call_id = String(++sequence)
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(call_id); reject(new Error("Fleet command unconfirmed")) }, 35000)
      pending.set(call_id, { resolve, reject, timer })
      child.stdin.write(JSON.stringify({ ...metadata(), ...fields, kind, call_id }) + "\n", (e) => {
        if (e) { clearTimeout(timer); pending.delete(call_id); reject(e) }
      })
    })
  }
  const sendSafe = (kind, fields) => send(kind, fields).catch((e) => fault(e))
  function available() {
    if (ready && !active && !unhealthy && openTurn === null) sendSafe("availability", { idle: true })
  }
  function stopBridge() {
    if (!child || shutdown) return
    const proc = child
    try { proc.stdin.end(JSON.stringify({ kind: "shutdown", call_id: "shutdown" }) + "\n") } catch {}
    shutdown = true
    const timer = setTimeout(() => proc.kill("SIGTERM"), 5000)
    timer.unref?.()
    proc.once("exit", () => clearTimeout(timer))
    for (const p of pending.values()) { clearTimeout(p.timer); p.reject(new Error("Fleet session shut down")) }
    pending.clear()
  }

  /** A fleet turn: our own message id, so the claimed event tells which turn ran it. */
  function inject(kind, text, delivery) {
    const id = randomUUID()
    ours.set(id, { kind, env: delivery })
    activity({ inject: kind, id, message_id: delivery?.message_id ?? null })
    agent.followup(Object.freeze({ id, role: "user", content: [{ type: "text", text }], source: { kind: "user" } }))
  }

  function handle(frame) {
    if (shutdown) return
    if (frame.kind === "reply") {
      const p = pending.get(frame.call_id || "")
      if (p) { clearTimeout(p.timer); pending.delete(frame.call_id); p.resolve(frame.answer) }
    } else if (frame.kind === "ui") {
      activity({ ui: frame.card?.render ?? "" })
    } else if (frame.kind === "fault") {
      fault(frame.reason)
    } else if (frame.kind === "completed") {
      // available(): after a late commit (design §47) nothing else reports idle again
      if (active?.message_id === frame.message_id) { active = null; candidates = []; available() }
    } else if (frame.kind === "permission_reply") {
      rule(frame)
    } else if (frame.kind === "delivery" && frame.delivery) {
      const d = frame.delivery
      if (unhealthy || active || !ready || openTurn !== null) { sendSafe("reject_delivery", { reason: "dsh is occupied by a manual/other run" }); return }
      if (d.slot !== SLOT || d.session_id !== sid || d.generation !== GENERATION) { fault("delivery identity mismatch"); return }
      active = d; candidates = []
      inject("delivery", d.body.text, d)
    }
  }

  function bind(a) {
    if (!a || shutdown) return
    if (agent) {
      // A second root agent is a new session (/new, resume): a bound session is never replaced without
      // retirement (pi's rule). Stop the bridge: the daemon sees the link lost and restarts the slot (§35).
      if (a.id !== agent.id && ctx.agents.roots().includes(a)) { fault("dsh session replaced by " + a.id + "; restart required"); stopBridge() }
      return
    }
    agent = a; sid = String(a.id)
    log("bind", sid); activity({ bound: sid })
    a.ctx.tools.register(defineTool({
      name: "fleet_result",
      description: "Submit your final business JSON result for daemon acceptance. Identity is bound by code. Call exactly once, as the only final tool call, when the fleet task is done.",
      parameters: { result: { type: "object", required: true, additionalProperties: true } },
      output: { schema: { type: "string" }, render: (_a, v) => [{ type: "text", text: v }] },
      async execute(args, exec) {
        const t = turns.get(openTurn)
        if (unhealthy || !active || t?.kind !== "delivery" || t.fleet?.env !== active || t.foreign.length
            || active.body?.purpose === "handoff") throw new Error("No active fleet delivery for this tool")
        const value = args?.result
        if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Candidate must be a business object")
        const blockedOnly = Object.keys(value).length === 1 && typeof value.blocked === "string"
        const spec = active.body?.reply
        if (!SERVICE) {
          const list = (k) => Array.isArray(value[k]) && value[k].length > 0 && value[k].every((x) => typeof x === "string")
          if (!list("done") || !list("evidence") || typeof value.not_done !== "string" || typeof value.blocked !== "string")
            throw new Error("result needs done (non-empty string array), evidence (non-empty string array), not_done (string), blocked (string)")
        } else if (spec && !blockedOnly && (spec.required.some((k) => !(k in value)) || (spec.allowed && Object.keys(value).some((k) => !spec.allowed.includes(k))))) {
          throw new Error("Business result violates required/allowed reply keys")
        }
        const candidate = { tool: "fleet_result", value }
        candidates.push(candidate); activity({ candidate: active.message_id })
        await send("candidate", { candidate })
        exec?.concludeTurn?.()
        return "候选已记录，等待回合结束与验收。"
      },
    }))
    const bridgeEnv = { ...env, HOME: env.FLEETMUX_DSH_USER_HOME || env.HOME }
    delete bridgeEnv[KEY_ENV]  // the key is DSH's alone
    child = spawn(COMMAND[0], COMMAND.slice(1), { stdio: ["pipe", "pipe", "pipe"], env: bridgeEnv })
    createInterface({ input: child.stdout }).on("line", (line) => { let f; try { f = JSON.parse(line) } catch { return fault("bad bridge frame") } handle(f) })
    child.stderr.on("data", (d) => log("bridge-stderr", String(d).slice(0, 2000)))
    child.on("error", fault); child.on("exit", () => { if (!shutdown) fault("bridge exited") })
    send("init").then((ack) => {
      if (ack?.status !== "registered") throw new Error(ack?.reason || "Fleet binding rejected")
      inject("startup", STARTUP)
    }).catch((e) => fault("boot: " + e))
  }

  function claimed(p) {
    if (!agent || p?.agent?.id !== agent.id) return
    const id = p.message?.id, n = p.turn
    const t = turns.get(n) ?? { kind: null, fleet: null, foreign: [], text: [], problem: "" }
    turns.set(n, t)
    const mine = ours.get(id)
    activity({ claimed: id, turn: n, fleet: mine?.kind ?? null })
    if (mine) {
      ours.delete(id)
      t.kind = mine.kind; t.fleet = mine
      if (mine.kind === "delivery" && !t.foreign.length) sendSafe("started")
      return
    }
    t.foreign.push(id)
    if (t.fleet) return                     // mixed: decided at turn end
    if (!ready && startupRetry) { t.kind = "startup"; startupRetry = false; return }  // the daemon's typed READY nudge
    t.kind = "manual"
    if (ready && !active) sendSafe("availability", { idle: false })
  }

  async function turnEnd(n, reason, session) {
    const t = turns.get(n); turns.delete(n)
    if (openTurn === n) openTurn = null
    try { const m = ctx.get("tokenMeter")?.measure(session); if (Number.isInteger(m?.totalTokens)) ctxUsed = m.totalTokens } catch (e) { log("measure", String(e)) }
    const kind = String(reason?.kind ?? "error")
    const aborted = kind === "aborted", failed = kind !== "completed" && !aborted
    const error = failed ? "turn ended: " + JSON.stringify(reason).slice(0, 1000) : ""
    const text = t ? t.text.join("\n") : ""
    for (const [rid, open] of perms) if (open.turn === n) closePerm(rid, null, "turn ended", "none")
    if (t?.kind === "startup") {
      const mixed = t.fleet && t.foreign.length
      const ack = await send("startup", { text, failed: failed || !!mixed, aborted, error: mixed ? "startup mixed with other input " + JSON.stringify(t.foreign) : error })
        .catch((e) => (fault(e), { status: "rejected" }))
      ready = ack.status === "ready"
      startupRetry = !ready
      activity({ startup: ack.status, reason: ack.reason ?? null })
      available()
    } else if (t?.kind === "delivery" && t.fleet?.env === active && active) {
      if (t.foreign.length) {
        await send("hold", { reason: "turn " + n + " mixed fleet " + active.message_id + " with other input " + JSON.stringify(t.foreign) }).catch(() => {})
        fault("held: mixed input"); return
      }
      let bad = failed
      if (t.problem) { bad = true; await send("candidate", { candidate: { tool: "model", error: t.problem } }).catch(fault) }
      else if (failed) await send("candidate", { candidate: { tool: "model", error } }).catch(fault)
      const batch = candidates; candidates = []
      await send("settled", { candidates: batch, failed: bad, aborted, unfinished: false }).catch(fault)
      available()
    } else {
      activity({ manual_turn: n, kind })
      available()
    }
  }

  function onEvent(session, ev) {
    if (!agent || String(session?.id) !== sid) return
    const type = ev?.type, d = ev?.data ?? {}
    if (type === "turn/start") { openTurn = d.turn; if (!turns.has(d.turn)) turns.set(d.turn, { kind: null, fleet: null, foreign: [], text: [], problem: "" }) }
    if (["turn/start", "turn/end", "assistant/message", "user/message", "approval/asked", "approval/decided"].includes(type)) activity({ ev: type, turn: d.turn ?? null })
    if (type === "assistant/message") {
      const t = turns.get(d.turn), msg = d.message ?? {}
      for (const part of msg.content ?? []) {
        if (part?.type === "text" && t) t.text.push(part.text || "")
        if (part?.type === "tool-call") { let args = {}; try { args = JSON.parse(part.arguments || "{}") } catch {} calls.set(part.id, { name: part.name, args }) }
      }
      const src = msg.source ?? {}
      if (t && t.fleet && src.kind === "model" && src.provider + "/" + src.model !== MODEL)
        t.problem = "model_lock: expected " + MODEL + ", got " + src.provider + "/" + src.model
    } else if (type === "turn/end") {
      turnEnd(d.turn, d.reason, session).catch((e) => fault("turn end: " + e))
    }
  }

  // Approvals: the TUI's own dialog (next) asks the person at the pane; deletes / moves wait for that answer
  // only. Any other request is also reported to the fleet, and the supervisor's ruling (permission_reply) may
  // answer it first. Never answered by a timeout.
  function closePerm(rid, reply, reason, decidedBy) {
    if (!perms.delete(rid)) return
    activity({ permission_resolved: rid, reply, decided_by: decidedBy })
    sendSafe("permission_resolved", { request_id: rid, reply, reason, decided_by: decidedBy })
  }
  function rule(frame) {
    const rid = frame.request_id, open = typeof rid === "string" ? perms.get(rid) : undefined
    const refuse = (why, held) => { activity({ permission_refused: rid ?? null, why }); if (typeof rid === "string") sendSafe("permission_refused", { request_id: rid, reason: why, held_for_user: !!held }) }
    if (!open) return refuse("not an open request of this session")
    if (open.cls !== "other" || permissionClass(open.req) !== "other") return refuse("delete/move/rename request: held for the user, never answered by fleet", true)
    if (frame.decision !== "once" && frame.decision !== "reject") return refuse("decision not allowed: " + JSON.stringify(frame.decision))
    open.rule(frame.decision)
  }
  async function approval(req, next) {
    if (!agent || !sid || unhealthy || !child) return next()
    const call = calls.get(req.callId) ?? { name: req.toolName, args: {} }
    const args = call.args && typeof call.args === "object" ? call.args : {}
    const rid = String(req.callId || "approval-" + randomUUID())
    const request = { id: rid, sessionID: sid, action: String(req.toolName || call.name || ""),
      resources: ARG_KEYS.filter((k) => args[k]).map((k) => String(args[k])), message: String(req.reason || ""),
      metadata: Object.fromEntries(Object.entries(args).filter(([, v]) => ["string", "number"].includes(typeof v)).map(([k, v]) => [k, String(v).slice(0, 300)])) }
    const cls = permissionClass(request)
    let ruled
    const fleet = new Promise((resolve) => { ruled = resolve })
    perms.set(rid, { req: request, cls, turn: openTurn, rule: ruled })
    activity({ permission: rid, class: cls })
    sendSafe("permission", { request, class: cls })
    const person = Promise.resolve(next()).then((out) => ({ by: "tui_user", out }))
    const winner = cls === "other" ? await Promise.race([person, fleet.then((d) => ({ by: "fleet", out: d === "once" ? "allowed-once" : "rejected" }))]) : await person
    const reply = winner.out === "allowed-once" ? "once" : winner.out === "rejected" ? "reject" : null
    closePerm(rid, reply, "answered " + winner.out, reply ? winner.by : "none")
    return winner.out
  }

  const require = process.getBuiltinModule("node:module").createRequire(ctx.baseUrl)
  const ready$ = import(pathToFileURL(require.resolve("@deepseek-ai/dsh-tools")).href).then((m) => {
    defineTool = m.defineTool
    for (const a of ctx.agents.roots()) bind(a)
  }).catch((e) => log("boot", String(e?.stack || e)))
  ctx.on("agent/created", (p) => { ready$.then(() => bind(p?.agent)) })
  ctx.on("agent/inbox/claimed", (p) => claimed(p))
  ctx.on("session/event", (session, ev) => onEvent(session, ev))
  ctx.on("approval/request", (req, next) => approval(req, next), { prepend: true })
  ctx.effect(() => () => { for (const rid of [...perms.keys()]) closePerm(rid, null, "dsh-TUI stopped", "none"); stopBridge() })
}
