/** Fleet bridge for Pi >= 1.1.0. Only this explicit extension loads in fleet agents. */
import { execFile } from "node:child_process";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  // Capture the callback once: actor bakes the slot and generation into this process.
  const callback: string[] = JSON.parse(process.env.CMUX_FLEET_PI_CALLBACK || "[]");
  if (!callback.length || !callback.every((part) => typeof part === "string")) {
    throw new Error("cmux-fleet: missing Pi callback command");
  }
  let pending = false;
  let hasStarted = false;
  const isSupervisor = callback[callback.length - 2] === "supervisor";
  let msgId = "";
  let last: { text: string; failed: boolean } | undefined;

  function showFleetLabel(ctx: ExtensionContext) {
    const label = ctx.sessionManager.getSessionName(); // supplied by fleet via --name
    if (ctx.mode === "tui" && label) ctx.ui.setTitle(label);
  }

  function deliveryId(text: string, cwd: string): string {
    const marker = " -- the full message is in ";
    const at = text.lastIndexOf(marker);
    const pointer = at < 0 ? undefined : text.slice(at + marker.length).match(
      /^(.+\/\.inbox\/(msg-[0-9]{8}-[0-9]{6}-[0-9a-f]{6})\.md) \. Read that whole file first and follow it\.\s*$/,
    );
    return pointer && pointer[1] === join(cwd, ".inbox", `${pointer[2]}.md`) ? pointer[2] : "";
  }

  function metadata(ctx: ExtensionContext) {
    const usage = ctx.getContextUsage();
    return {
      thread: ctx.sessionManager.getSessionId(),
      transcript: ctx.sessionManager.getSessionFile(),
      ctx_used: usage?.tokens ?? null,
      ctx_window: usage?.contextWindow ?? ctx.model?.contextWindow ?? null,
    };
  }

  function send(payload: object): Promise<string> {
    // JSON goes through stdin, not shell interpolation or an argv-size-limited argument.
    return new Promise((resolve, reject) => {
      const child = execFile(callback[0], callback.slice(1), {
        timeout: 15000, maxBuffer: 8192,
      }, (error, stdout) => {
        if (error) reject(new Error("cmux-fleet: Pi callback failed; check fleet/Redis health"));
        else resolve(stdout);
      });
      child.stdin?.on("error", () => {}); // early child exit is handled by the execFile callback
      child.stdin?.end(JSON.stringify(payload));
    });
  }

  pi.on("session_start", async (_event, ctx) => {
    hasStarted = ctx.mode === "tui" && ctx.sessionManager.getBranch().some(
      (entry) => entry.type === "message" && entry.message.role === "assistant",
    );
    showFleetLabel(ctx);
    await send({ kind: "pi_session", ...metadata(ctx) });
  });

  pi.on("input", async (event, ctx) => {
    if (ctx.mode !== "tui" || !isSupervisor || !hasStarted || event.source !== "interactive"
        || event.text.trim().startsWith("/") || deliveryId(event.text, ctx.cwd)) {
      return { action: "continue" };
    }
    // Human input joins the ORIGINAL daemon request/task queue. The model must not
    // invent request IDs, and manual turns must not bypass delivery correlation.
    if (event.images?.length) {
      ctx.ui.notify("Fleet operator input is text-only; provide an image file path instead.", "warning");
      return { action: "handled" };
    }
    try {
      const ack = JSON.parse(await send({ kind: "operator_input", text: event.text, ...metadata(ctx) }));
      if (ack.status === "rejected") ctx.ui.notify(`Fleet: ${ack.reason}`, "error");
      else ctx.ui.notify(`Fleet ${ack.status}: ${ack.id || ack.task_id || ack.request_id}`, "info");
    } catch {
      ctx.ui.notify("Fleet input could not be confirmed. Check fleet status before resubmitting.", "error");
    }
    return { action: "handled" };
  });

  pi.on("before_agent_start", async (event, ctx) => {
    showFleetLabel(ctx);
    // Only the daemon's inbox pointer identifies a dispatched message. Never reuse an
    // old id after manual input, retries, session changes or identical reply text.
    // Instructions may quote another agent's inbox: bind only to our final pointer.
    msgId = deliveryId(event.prompt, ctx.cwd);
    hasStarted = true;
    pending = true;
    last = undefined;
    await send({ kind: "pi_session", ...metadata(ctx) });
  });

  pi.on("message_end", async (event) => {
    const message = event.message;
    if (pending && message.role === "assistant") {
      last = {
        text: message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n"),
        failed: message.stopReason === "error" || message.stopReason === "aborted",
      };
    }
  });

  // agent_end / turn_end are too early: tools, retry, compaction and queued follow-ups
  // may still run. No callback on them, and no fallback to an earlier assistant reply.
  pi.on("agent_settled", async (event, ctx) => {
    showFleetLabel(ctx);
    if (!pending) return;
    const payload = {
      kind: "turn", ...metadata(ctx), msg_id: msgId,
      text: (last?.text || "").slice(-200000),
      aborted: event.aborted, failed: !last || last.failed,
    };
    pending = false;
    last = undefined;
    await send(payload);
  });
}
