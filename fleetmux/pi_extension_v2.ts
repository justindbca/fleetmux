// Fleet v2: agent_settled reports; daemon mid-turn prompts steer the active delivery.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

type Delivery = {
  run_id: string; slot: string; generation: number; session_id: string;
  message_id: string; delivery_id: string; stream_id: string; fence: number;
  body: { text: string; title: string; mode: string; purpose: string; reply?: { required: string[]; allowed?: string[] } };
};
type Candidate = { tool: string; value: Record<string, unknown> };
type Answer = { status: string; reason?: string };
type Frame = { kind: string; call_id?: string; answer?: Answer; delivery?: Delivery;
  event_id?: string; card?: { render: string }; reason?: string; message_id?: string };
// §36 the supervisor's daemon watchdog: the bridge's daemon_beat answer and the fleet.yaml#daemon_watch knobs.
export type DaemonFacts = { status?: string; now?: number; beat?: number; daemon_running?: boolean; unfinished?: boolean;
  last_event?: string; last_event_at?: number; pending_events?: number; held?: string[] };
export type DaemonWatch = { check_interval_s: number; stale_after_s: number; realert_after_s: number };

// The text of one watchdog turn, or null: the beat is stale, no daemon holds the run, the run is unfinished,
// the session is idle and no watchdog turn started within realert_after_s. One line.
export function daemonWatchdog(facts: DaemonFacts | undefined, idle: boolean, lastAlertAt: number, now: number,
  knobs: DaemonWatch): string | null {
  if (!facts || facts.status !== "ok" || typeof facts.now !== "number" || typeof facts.beat !== "number") return null;
  const down = facts.now - facts.beat;
  if (down <= knobs.stale_after_s || facts.daemon_running !== false || facts.unfinished !== true) return null;
  if (!idle || now - lastAlertAt < knobs.realert_after_s) return null;
  const at = (s?: number) => (s ? new Date(s * 1000).toTimeString().slice(0, 8) : "never");
  return `[Daemon watchdog] The fleet daemon is down: last heartbeat ${at(facts.beat)}, ${Math.round(down)} s ago, ` +
    `and no daemon holds this run. Last daemon audit event: ${facts.last_event || "none"} at ${at(facts.last_event_at)}. ` +
    `Unprocessed events: ${facts.pending_events ?? "unknown"}. Held/expired deliveries: ${facts.held?.length ? facts.held.join(", ") : "none"}. ` +
    `Follow "Daemon down" in your AGENTS.md now: check, note it in PROGRESS.md, run the restart command there once, verify the heartbeat.`;
}

export default function (pi: ExtensionAPI) {
  const command: string[] = JSON.parse(process.env.FLEETMUX_PI_BRIDGE || "[]");
  if (!command.length || !command.every((part) => typeof part === "string")) throw new Error("Fleet v2 bridge command missing");
  const slot = process.env.FLEETMUX_PI_SLOT || command.at(-2)!;
  const supervisor = slot === "supervisor";
  const service = process.env.FLEETMUX_PI_MODE === "service";
  // Set by the daemon for the main fleet's supervisor only (pi_adapter.environment).
  const watch: DaemonWatch | null = supervisor ? JSON.parse(process.env.FLEETMUX_DAEMON_WATCH || "null") : null;
  let watchTimer: ReturnType<typeof setInterval> | undefined;
  let lastAlertAt = 0;
  let child: ChildProcessWithoutNullStreams | undefined;
  let context: ExtensionContext | undefined;
  let binding: string | undefined;
  let phase: "startup" | "manual" | "delivery" | "idle" = "startup";
  let ready = false;
  let startupBegan = false;
  let shutdown = false;
  let unhealthy = false;
  let active: Delivery | undefined;
  let settling: string | undefined; // §47: the delivery whose settled the daemon answered late
  let candidates: Candidate[] = [];
  let failed = false;
  let lastError = "";
  let startupText = "";
  let sequence = 0;
  const pending = new Map<string, { resolve: (value: Answer) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  const seen = new Set<string>();
  const toolsRunning = new Set<string>();

  function metadata(ctx: ExtensionContext) {
    if (binding && ctx.sessionManager.getSessionId() !== binding) throw new Error("Fleet v2 session binding changed; restart required");
    const usage = ctx.getContextUsage();
    return { thread: ctx.sessionManager.getSessionId(), transcript: ctx.sessionManager.getSessionFile(),
      ctx_used: usage?.tokens ?? null, ctx_window: usage?.contextWindow ?? ctx.model?.contextWindow ?? null };
  }
  function title(ctx: ExtensionContext) {
    const label = ctx.sessionManager.getSessionName();
    if (ctx.mode === "tui" && label) ctx.ui.setTitle(label);
  }
  function fault(text?: unknown) {
    unhealthy = true;
    // Only a bridge fault frame carries display text; .catch(fault) passes an Error.
    context?.ui.setStatus("fleet", typeof text === "string" && text ? text : "通信中断；fleet 将自动重启本会话");
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("Fleet bridge unavailable")); }
    pending.clear();
  }
  function send(kind: string, fields: object = {}, ctx = context): Promise<Answer> {
    if (!child || shutdown || !ctx) return Promise.reject(new Error("Fleet bridge is not running"));
    const call_id = String(++sequence);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(call_id); reject(new Error("Fleet command unconfirmed; inspect control before retrying")); }, 35000);
      pending.set(call_id, { resolve, reject, timer });
      child!.stdin.write(JSON.stringify({ ...metadata(ctx), ...fields, kind, call_id }) + "\n", (error) => {
        if (error) { clearTimeout(timer); pending.delete(call_id); reject(error); }
      });
    });
  }
  function entry(eventId: string, render: string) {
    if (seen.has(eventId)) return;
    seen.add(eventId);
    pi.appendEntry("fleet-ui", { event_id: eventId, render });
  }
  function availableAfterNotification(ctx: ExtensionContext) {
    // Run on the next Node turn after the notification handler has returned.
    // No delivery can be offered while Pi is emitting agent_settled.
    setImmediate(() => {
      if (!shutdown && !unhealthy && ready && !active && ctx.isIdle() && !ctx.hasPendingMessages()) {
        send("availability", { idle: true }, ctx).catch(fault);
      }
    });
  }
  function daemonCheck() {
    // Through the bridge, which reads Redis and daemon.lock itself: it answers while the daemon is dead.
    const ctx = context;
    if (!watch || !ctx || shutdown || !ready || phase === "delivery") return;
    send("daemon_beat", { stale_after: watch.stale_after_s }, ctx).then((facts) => {
      const text = daemonWatchdog(facts as DaemonFacts, phase !== "delivery" && ctx.isIdle() && !ctx.hasPendingMessages(),
        lastAlertAt, Date.now() / 1000, watch);
      if (!text || shutdown) return;
      lastAlertAt = Date.now() / 1000;
      // Before ready / during a delivery no check runs; this turn is a manual run (never a fleet result).
      pi.sendMessage({ customType: "fleet-daemon-watchdog", content: text, display: true }, { triggerTurn: true });
    }).catch(() => undefined);
  }
  function handle(frame: Frame) {
    const ctx = context;
    if (!ctx || shutdown) return;
    if (frame.kind === "reply") {
      const item = pending.get(frame.call_id || "");
      if (item) { clearTimeout(item.timer); pending.delete(frame.call_id!); item.resolve(frame.answer!); }
    } else if (frame.kind === "ui") {
      if (frame.event_id && frame.card) entry(frame.event_id, frame.card.render);
    } else if (frame.kind === "fault") {
      fault(frame.reason);
    } else if (frame.kind === "completed") {
      if (active?.message_id === frame.message_id) {
        active = undefined; candidates = []; if (phase !== "manual") phase = "idle"; ctx.ui.setStatus("fleet", "等待派工");
        // A late commit: the post-settle availability found the delivery still active and sent nothing.
        if (settling === frame.message_id) { settling = undefined; availableAfterNotification(ctx); }
      }
    } else if (frame.kind === "delivery" && frame.delivery) {
      const env = frame.delivery;
      if (unhealthy || active || !ready || phase !== "idle" || !ctx.isIdle() || ctx.hasPendingMessages()) {
        // No steer, followUp, nextTurn or local hidden queue. PEL stays held.
        send("reject_delivery", { reason: "Pi is occupied by a manual/other run" }).catch(fault);
        return;
      }
      if (env.slot !== slot || env.session_id !== binding || env.generation !== Number(command.at(-1))) { fault(); return; }
      active = env; phase = "delivery"; candidates = []; failed = false; lastError = ""; toolsRunning.clear();
      pi.appendEntry("fleet-binding", { message_id: env.message_id, delivery_id: env.delivery_id });
      ctx.ui.setStatus("fleet", "执行派工任务");
      // Only body text enters model context. details are non-model transport metadata.
      // Pi 1.1.0 starts this at agent_start without before_agent_start/input; Pi >= 1.2.0 also
      // fires before_agent_start (messageType "custom") first, which must keep phase "delivery".
      pi.sendMessage({ customType: "fleet-delivery", content: env.body.text, display: true,
        details: { ...env, body: { title: env.body.title } } }, { triggerTurn: true });
    }
  }

  pi.registerEntryRenderer<{ render: string }>("fleet-ui", (entry) => new Text(entry.data?.render || "", 0, 0));
  pi.registerMessageRenderer<{ body: { title: string } }>("fleet-delivery", (message) => new Text(message.details?.body.title || "派工任务", 0, 0));
  pi.registerMessageRenderer("fleet-daemon-watchdog", (message) => new Text(String(message.content || ""), 0, 0));
  const artifact = Type.Object({ path: Type.String(), min_chars: Type.Optional(Type.Integer({ minimum: 1 })), contains: Type.Optional(Type.Array(Type.String())) });
  const dispatch = Type.Object({ to: Type.String(), instruction: Type.String({ minLength: 12 }),
    id: Type.Optional(Type.String()), advise_note: Type.Optional(Type.String()), fresh: Type.Optional(Type.Boolean()),
    // §44 监工直接开子 fleet (supervisor_split): the daemon checks the switch, the shape and the limits
    split: Type.Optional(Type.Object({ reason: Type.String({ minLength: 1 }),
      proposal: Type.Array(Type.Object({ instruction: Type.String({ minLength: 12 }), output: Type.String({ minLength: 1 }),
        kind: Type.Optional(Type.String()) }), { minItems: 1 }),
      workers: Type.Optional(Type.Integer({ minimum: 1 })), note: Type.Optional(Type.String()),
      supervisor_profile: Type.Optional(Type.String()), worker_profiles: Type.Optional(Type.Array(Type.String())) })) });
  const consult = Type.Object({ worker: Type.String(), task: Type.String(), finding: Type.String({ minLength: 1 }),
    want: Type.Optional(Type.String()) });
  // §20 split requests, plan F' [dialog] (approve / reject) and [blocked] (revise needs a note: checked by the daemon).
  const escalation = Type.Object({ task: Type.String({ minLength: 1 }),
    decision: Type.Union([Type.Literal("approve"), Type.Literal("revise"), Type.Literal("reject")]),
    workers: Type.Optional(Type.Integer({ minimum: 1 })), note: Type.Optional(Type.String()),
    // §44: an approve may pick the split's models (cards, hosts and capacity are checked by the daemon)
    supervisor_profile: Type.Optional(Type.String()), worker_profiles: Type.Optional(Type.Array(Type.String())),
    // §46: an approve of a sub-fleet report may narrow the paths the program grants (never widen: the daemon checks)
    paths: Type.Optional(Type.Array(Type.String({ minLength: 1 }))) });
  const decisionSchema = service ? Type.Object({ status: Type.Union([Type.Literal("RUNNING"), Type.Literal("BLOCKED")]),
    reason: Type.Optional(Type.String()), dispatch: Type.Array(dispatch), skip: Type.Optional(Type.Array(Type.Object({ reason: Type.String() }))),
    escalations: Type.Optional(Type.Array(escalation)),
    // M0.1: answer the bound request yourself; result is checked against its reply schema (request_id bound by code)
    answer: Type.Optional(Type.Array(Type.Object({ request_id: Type.Optional(Type.String()),
      result: Type.Record(Type.String(), Type.Any()) }))),
    // design §29: approve / reject the bound external sub-fleet up / down (id defaults to the bound request)
    subfleet: Type.Optional(Type.Object({ id: Type.Optional(Type.String()),
      decision: Type.Union([Type.Literal("approve"), Type.Literal("reject")]), note: Type.Optional(Type.String()) })) }) :
    Type.Object({ status: Type.Union([Type.Literal("RUNNING"), Type.Literal("COMPLETED"), Type.Literal("BLOCKED")]),
      completion: Type.Integer({ minimum: 0, maximum: 100 }), reason: Type.Optional(Type.String()),
      dispatch: Type.Array(dispatch), consult: Type.Optional(Type.Array(consult)), artifacts: Type.Optional(Type.Array(artifact)),
      escalations: Type.Optional(Type.Array(escalation)),
      advisor: Type.Optional(Type.Union([Type.Literal("on"), Type.Literal("off")])),
      withdraw: Type.Optional(Type.Array(Type.String())),
      // M15: an explicit, bounded dependency wait — only with an empty dispatch; the daemon checks the rest.
      waiting: Type.Optional(Type.Object({ for: Type.String({ minLength: 12 }), check: Type.String({ minLength: 12 }),
        for_hours: Type.Optional(Type.Number({ exclusiveMinimum: 0, maximum: 12 })) }, { additionalProperties: false })) });
  pi.registerTool({
    name: supervisor ? "fleet_decision" : "fleet_result", label: supervisor ? "派工候选" : "结果候选",
    description: supervisor ? "Submit your final fleet decision for daemon validation. Route only this delivery's request; transport IDs are bound by code." :
      "Submit your final business JSON result for daemon acceptance. Identity is bound by code.",
    parameters: supervisor ? decisionSchema : Type.Object({ result: service ? Type.Record(Type.String(), Type.Any()) : Type.Object({
      done: Type.Array(Type.String(), { minItems: 1 }), evidence: Type.Array(Type.String(), { minItems: 1 }),
      not_done: Type.String(), blocked: Type.String() }) }),
    exposure: "model-only", executionMode: "sequential",
    async execute(_id, params, signal) {
      if (!active || phase !== "delivery" || active.body.purpose === "handoff" || signal?.aborted || unhealthy) throw new Error("No active fleet delivery for this tool");
      const value = supervisor ? params as Record<string, unknown> : (params as { result: Record<string, unknown> }).result;
      if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Candidate must be a business object");
      // Plan F' (service): exactly {"blocked": "<text>"} asks the supervisor to rule; the daemon checks it.
      const blockedOnly = Object.keys(value).length === 1 && typeof (value as Record<string, unknown>).blocked === "string";
      if (!supervisor && active.body.reply && !blockedOnly) {
        const spec = active.body.reply;
        if (spec.required.some((key) => !(key in value)) || spec.allowed?.some(() => Object.keys(value).some((key) => !spec.allowed!.includes(key)))) {
          throw new Error("Business result violates required/allowed reply keys");
        }
      }
      const candidate = { tool: supervisor ? "fleet_decision" : "fleet_result", value };
      candidates.push(candidate); // multiple/conflicting candidates never become success
      pi.appendEntry("fleet-candidate", { message_id: active.message_id, candidate });
      await send("candidate", { candidate });
      return { content: [{ type: "text", text: "候选已记录，等待回合结束与验收。" }], details: { pending: true },
        // Actual Pi only skips automatic follow-up if EVERY completed tool in this
        // batch returns terminate. No claim that this interrupts sibling tools.
        terminate: true };
    },
    renderCall: () => new Text(supervisor ? "提交派工候选" : "提交结果候选", 0, 0),
    renderResult: (result) => new Text((result.details as { pending?: boolean } | undefined)?.pending === true ? "候选已记录，等待验收。" :
      "候选未记录：工具执行或校验失败。详情见 control。", 0, 0),
  });

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.mode !== "tui") throw new Error("Fleet v2 requires the interactive Pi TUI");
    if (child) throw new Error("Fleet v2 cannot replace a bound session without retirement");
    context = ctx; binding = ctx.sessionManager.getSessionId(); title(ctx);
    for (const item of ctx.sessionManager.getBranch()) {
      if (item.type === "custom" && item.customType === "fleet-ui") {
        const data = item.data as { event_id?: string }; if (data?.event_id) seen.add(data.event_id);
      }
    }
    child = spawn(command[0], command.slice(1), { stdio: ["pipe", "pipe", "pipe"] });
    createInterface({ input: child.stdout }).on("line", (line) => {
      try { handle(JSON.parse(line)); } catch { fault(); }
    });
    child.stderr.on("data", () => ctx.ui.setStatus("fleet", "桥接错误；详情见 control"));
    child.on("error", fault); child.on("exit", () => { if (!shutdown) fault(); });
    const ack = await send("init", {}, ctx);
    if (ack.status !== "registered") { fault(); throw new Error(ack.reason || "Fleet binding rejected"); }
  });
  pi.on("input", async (event, ctx) => {
    if (active && event.source === "interactive" && event.text.startsWith("[FLEET MID-TURN HANDOFF fx-"))
      return { action: "continue" };
    context = ctx;
    if (!ready && phase === "startup" && !startupBegan) return { action: "continue" };
    if (!ready) { ctx.ui.notify("Fleet 尚未 READY，输入未受理；请先检查启动状态。", "error"); return { action: "handled" }; }
    if (event.source === "extension") return { action: "continue" };
    if (event.text.trim().startsWith("/")) return { action: "continue" };
    if (supervisor) {
      if (event.images?.length) { ctx.ui.notify("Fleet 输入支持文字；请提供图片文件路径。", "warning"); return { action: "handled" }; }
      try {
        const ack = await send("operator_input", { text: event.text, correlation: "ui-" + randomUUID() }, ctx);
        entry("input-" + randomUUID(), event.text); // preserved human text, no model call here
        ctx.ui.setStatus("fleet", ack.status === "accepted" ? "指令已排队" : "指令未确认；详情见 control");
        if (ack.status === "rejected") ctx.ui.notify(ack.reason || "输入被拒绝", "error");
      } catch { ctx.ui.notify("输入未确认；请先查看 control 再决定是否重试。", "error"); }
      return { action: "handled" };
    }
    if (active || !ctx.isIdle()) { ctx.ui.notify("派工执行期间请等待；手工输入未加入该任务。", "warning"); return { action: "handled" }; }
    phase = "manual";
    await send("availability", { idle: false }, ctx);
    return { action: "continue" };
  });
  pi.on("before_agent_start", async (_event, ctx) => {
    context = ctx; title(ctx);
    // Pi 1.1.0 never calls this hook for our sendMessage(triggerTurn) delivery run; Pi >= 1.2.0
    // does (messageType "custom", see tests/fixtures/pi_events_1.*.json). Flipping to "manual"
    // here made fleet_decision/fleet_result reject and left the delivery held forever.
    // While a delivery is active no other run can start (input is handled/refused above),
    // so any run start now is that delivery: keep phase/active; agent_start reports "started".
    if (phase === "delivery" && active) return;
    if (ready) phase = "manual"; // user prompt runs are never a fleet delivery
    else { phase = "startup"; startupBegan = true; }
    failed = false; lastError = ""; startupText = ""; toolsRunning.clear();
    if (ready) await send("availability", { idle: false }, ctx);
  });
  pi.on("agent_start", async (_event, ctx) => {
    context = ctx;
    if (phase === "delivery" && active) await send("started", {}, ctx);
  });
  pi.on("tool_execution_start", (event) => { toolsRunning.add(event.toolCallId); auditTool(event); });
  function auditTool(event: any) {
    // P8j: after-the-fact audit only (never blocks a tool); the bridge checks its binding, redacts and writes
    // security.tool_call. A failed write is answered as an ordinary error and swallowed here: never a fault.
    if (!child || shutdown || unhealthy || !context) return;
    let args = "";
    try { args = typeof event?.args === "string" ? event.args : JSON.stringify(event?.args ?? {}); } catch { args = ""; }
    send("tool_call", { tool: String(event?.toolName ?? "").slice(0, 200), args: String(args).slice(0, 200) })
      .catch(() => undefined);
  }
  pi.on("tool_execution_end", async (event, ctx) => {
    toolsRunning.delete(event.toolCallId);
    if (event.isError && phase === "delivery" && active) {
      // Audit only; this does not add a candidate or a model message.
      await send("candidate", { candidate: { tool: event.toolName, error: event.result } }, ctx);
    }
  });
  pi.on("message_end", async (event, ctx) => {
    if (event.message.role === "assistant") {
      failed = event.message.stopReason === "error" || event.message.stopReason === "aborted";
      lastError = event.message.errorMessage || "";
      if (failed && lastError && phase === "delivery" && active) {
        await send("candidate", { candidate: { tool: "model", error: lastError } }, ctx);
      }
      if (phase === "startup") startupText = event.message.content.filter((p) => p.type === "text").map((p) => p.text).join("\n");
    }
  });
  pi.on("agent_settled", async (event, ctx) => {
    context = ctx; title(ctx);
    // Notification only. No sendMessage, appendEntry or continuation at this boundary.
    if (phase === "startup") {
      const ack = await send("startup", { text: startupText, failed, aborted: event.aborted, error: lastError }, ctx);
      ready = ack.status === "ready"; phase = ready ? "idle" : "startup";
      if (ready && watch && !watchTimer) { watchTimer = setInterval(daemonCheck, watch.check_interval_s * 1000); watchTimer.unref?.(); }
      if (!ready) {
        ctx.ui.setStatus("fleet", "启动失败：尚未 READY；详情见 control");
        ctx.ui.notify(ack.reason || "Fleet 启动未获 READY，禁止派工。", "error");
      }
      availableAfterNotification(ctx);
    } else if (phase === "delivery" && active) {
      const batch = candidates, id = active.message_id; candidates = []; phase = "idle";
      // §47: a busy daemon has not answered yet (or the bridge, serial, was still waiting on an earlier
      // command when our 35 s ran out): the bridge keeps the lease and sends completed later.
      const ack = await send("settled", { candidates: batch, failed, aborted: event.aborted, unfinished: toolsRunning.size > 0 }, ctx)
        .catch((): Answer => ({ status: "pending" }));
      if (ack.status === "pending" && !unhealthy && active?.message_id === id) { settling = id; ctx.ui.setStatus("fleet", "已结算，等 fleet 确认"); }
      availableAfterNotification(ctx);
    } else if (phase === "manual") {
      phase = "idle";
      availableAfterNotification(ctx);
    }
  });
  pi.on("session_shutdown", async () => {
    if (watchTimer) { clearInterval(watchTimer); watchTimer = undefined; }
    if (shutdown) return;
    const process = child;
    // Stop is idempotent; stdin EOF also closes Python resources.
    if (process) {
      process.stdin.end(JSON.stringify({ kind: "shutdown", call_id: "shutdown" }) + "\n");
      shutdown = true;
      await new Promise<void>((resolve) => {
        if (process.exitCode !== null) { resolve(); return; }
        const timer = setTimeout(() => { process.kill("SIGTERM"); resolve(); }, 5000);
        process.once("exit", () => { clearTimeout(timer); resolve(); });
      });
    } else shutdown = true;
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error("Fleet session shut down")); }
    pending.clear(); child = undefined;
  });
}
