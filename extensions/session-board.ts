/**
 * session-board.ts — fleetmux cross-session board for pi (docs/contracts.md §7)
 *
 * Writes this pi session onto the shared board (~/.fleetmux/board, FLEETMUX_BOARD_DIR) and relays
 * collision notices ("another session is editing the same repo / branch / file"). All storage and
 * detection lives in fleetmux/session_board.py; this file only calls it through pi.exec.
 *
 * Board writes: session_start, input (the user's goal), tool_result (edit / write / bash), session_shutdown.
 * Comparison: on those events and every 30 s; the timer starts in session_start and is cleared in
 * session_shutdown, so nothing outlives the pi process.
 * Delivery: busy -> deliverAs "steer"; idle -> "steer" + triggerTurn (wakes the session);
 * fleet-managed panes (supervisor / workers) only "nextTurn" so the fleet delivery protocol is not disturbed.
 *
 * Off: PI_SESSION_BOARD=0
 * Script: PI_SESSION_BOARD_SCRIPT, else session_board.py beside this file (launchers/install-session-board.sh
 *         links it there), else $FLEETMUX_PI_DIR/fleetmux/session_board.py
 * Interval: PI_SESSION_BOARD_INTERVAL_MS (default 30000)
 */

import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

type Phase = "start" | "input" | "tool" | "timer";
type Delivery = { deliverAs: "steer" | "nextTurn"; triggerTurn?: boolean };

/** Env set on fleet-launched pi panes: fleetmux/pi_adapter.py environment() — v2 BRIDGE / SLOT, v1 CALLBACK. */
export function isFleetManaged(env: Record<string, string | undefined>): boolean {
	return Boolean(env.FLEETMUX_PI_SLOT || env.FLEETMUX_PI_BRIDGE || env.FLEETMUX_PI_CALLBACK);
}

export function deliveryOptions(s: { fleet: boolean; idle: boolean; phase: Phase }): Delivery {
	if (s.fleet || s.phase === "input") return { deliverAs: "nextTurn" }; // input: the user's turn is about to start
	return s.idle ? { deliverAs: "steer", triggerTurn: true } : { deliverAs: "steer" };
}

function scriptPath(): string {
	if (process.env.PI_SESSION_BOARD_SCRIPT) return process.env.PI_SESSION_BOARD_SCRIPT;
	try {
		const sibling = join(dirname(fileURLToPath(import.meta.url)), "session_board.py");
		if (existsSync(sibling)) return sibling;
	} catch {}
	return join(process.env.FLEETMUX_PI_DIR || "", "fleetmux", "session_board.py");
}

export default function (pi: ExtensionAPI) {
	if (process.env.PI_SESSION_BOARD === "0") return;
	const script = scriptPath();
	const fleet = isFleetManaged(process.env);
	const interval = Number(process.env.PI_SESSION_BOARD_INTERVAL_MS) || 30_000;
	let timer: ReturnType<typeof setInterval> | null = null;
	let last: ExtensionContext | null = null;
	let checking = false;

	async function post(ctx: ExtensionContext, phase: Phase, event: string, extra: Record<string, unknown> = {}) {
		try {
			const payload = {
				event,
				session_id: ctx.sessionManager.getSessionId(),
				cwd: ctx.cwd,
				name: pi.getSessionName() || undefined,
				pid: process.pid,
				...extra,
			};
			const res = await pi.exec("python3", [script, "hook", "pi", JSON.stringify(payload)], { timeout: 5000 });
			const text = (res.stdout || "").trim();
			if (!text || event === "end") return;
			pi.sendMessage(
				{ customType: "session-board", content: text, display: true },
				deliveryOptions({ fleet, idle: ctx.isIdle(), phase }),
			);
		} catch {
			// the board never gets in the session's way; the Python side logs to <board>/errors.log
		}
	}

	pi.on("session_start", async (_event, ctx) => {
		last = ctx;
		if (!timer) {
			timer = setInterval(async () => {
				if (checking || !last) return;
				checking = true;
				try {
					await post(last, "timer", "check");
				} finally {
					checking = false;
				}
			}, interval);
		}
		await post(ctx, "start", "start");
	});

	pi.on("input", async (event, ctx) => {
		last = ctx;
		if (event.source !== "extension" && typeof event.text === "string") await post(ctx, "input", "prompt", { prompt: event.text });
		return { action: "continue" };
	});

	pi.on("tool_result", async (event, ctx) => {
		last = ctx;
		const input = (event.input ?? {}) as { path?: unknown; command?: unknown };
		if ((event.toolName === "edit" || event.toolName === "write") && typeof input.path === "string") {
			await post(ctx, "tool", "tool", { path: input.path });
		} else if (event.toolName === "bash" && typeof input.command === "string") {
			await post(ctx, "tool", "tool", { command: input.command });
		}
	});

	pi.on("session_shutdown", async (_event, ctx) => {
		if (timer) clearInterval(timer);
		timer = null;
		last = null;
		await post(ctx, "timer", "end");
	});
}
