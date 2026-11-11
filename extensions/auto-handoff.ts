/**
 * auto-handoff.ts — pi 版上下文接力（cmux-fleet-pi）
 *
 * 把 Claude Code 侧的 /handoff 搬到 pi：上下文逼近阈值 → 生成交接单 → 开新 cmux 窗口接力 → 本会话优雅退出。
 * 新窗口由 start-pi-autohandoff.command 启动（与扩展同目录即可，自洽部署）；首条 prompt 由本扩展在
 * session_start 读 PI_HANDOFF_FILE 注入，避免把长 prompt 塞进 cmux --command。
 *
 * 关闭：PI_AUTO_HANDOFF=0
 * 阈值：PI_AUTO_HANDOFF_PCT（默认 80，按 ctx.getContextUsage().percent）
 * 手动：/auto-handoff [额外指示]
 * 安装：launchers/install-auto-handoff.sh（本机 ~/.pi/agent/extensions；--host HOST 经 ssh 部署到另一台机器）
 *
 * 已核 pi 1.3.0：扩展发现 <agent-dir>/extensions/ 自动加载；内置 /reload 可热装到已开着的会话；
 * 事件 handler 拿到 ExtensionContext（无 newSession），但有 getContextUsage()/shutdown()/sessionManager/model/modelRegistry。
 */

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { AgentMessage } from "@earendil-works/pi-agent-core";
import { type Message, uuidv7 } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext, SessionEntry } from "@earendil-works/pi-coding-agent";
import { convertToLlm, serializeConversation } from "@earendil-works/pi-coding-agent";

// fleet 仓根目录（可选）：没设就只找扩展同目录 / 上一级 launchers/ 里的接力启动器。
// CMUX_FLEET_PI_DIR: legacy name, kept for compatibility.
const FLEET_DIR = process.env.FLEETMUX_PI_DIR || process.env.CMUX_FLEET_PI_DIR || "";
const HANDOFF_DIR = process.env.PI_HANDOFF_DIR ?? join(homedir(), ".handoff");
const TRIGGER_PCT = Number(process.env.PI_AUTO_HANDOFF_PCT ?? "80");
// 绝对 token 阈值（>0 时生效）：极短会话里 percent 可能 <1%，自测/小会话需要绝对阈值。
const TRIGGER_TOKENS = Number(process.env.PI_AUTO_HANDOFF_TOKENS ?? "0");
const DEBUG = process.env.PI_AUTO_HANDOFF_DEBUG === "1";
const ENABLED = process.env.PI_AUTO_HANDOFF !== "0";

const SYSTEM_PROMPT = [
	"你是上下文交接助手。给你一段对话历史和会话工作目录，生成一份「零上下文会话照着就能接着干」的交接单。",
	"",
	"要求：",
	"1. 只写事实：命令、文件路径、退出码、看到的结果。没跑过的验证写「未跑」，禁止「应该通过」。",
	"2. 用户原始指令必须引用原话，禁转述改向。",
	"3. 「下一步（按顺序）」必须具体到命令和文件，零上下文会话能照做。",
	"4. 只输出交接单本身，不要任何前言或代码围栏，也不要写标题行（标题由程序补）。",
	"",
	"从下面这七个二级标题开始，标题照抄：",
	"## 目标（用户原话）",
	"## 完成标准",
	"## 已做（事实）",
	"## 进行中",
	"## 下一步（按顺序）",
	"## 待用户拍板",
	"## 关键文件 / 命令",
	"## 风险 / 保留",
].join("\n");

function entryToMessage(entry: SessionEntry): AgentMessage | undefined {
	if (entry.type === "message") return entry.message;
	if (entry.type === "compaction") {
		return {
			role: "compactionSummary",
			summary: entry.summary,
			tokensBefore: entry.tokensBefore,
			timestamp: new Date(entry.timestamp).getTime(),
		} as AgentMessage;
	}
	return undefined;
}

// 与官方 examples/extensions/handoff.ts 同口径：有 compaction 时从 firstKeptEntryId 起接。
function getHandoffMessages(branch: SessionEntry[]): AgentMessage[] {
	let compactionIndex = -1;
	for (let i = branch.length - 1; i >= 0; i--) {
		if (branch[i].type === "compaction") {
			compactionIndex = i;
			break;
		}
	}
	if (compactionIndex < 0) {
		return branch.map(entryToMessage).filter((m): m is AgentMessage => m !== undefined);
	}
	const compaction = branch[compactionIndex];
	const firstKeptIndex =
		compaction.type === "compaction" ? branch.findIndex((e) => e.id === compaction.firstKeptEntryId) : -1;
	const compacted = [
		compaction,
		...(firstKeptIndex >= 0 ? branch.slice(firstKeptIndex, compactionIndex) : []),
		...branch.slice(compactionIndex + 1),
	];
	return compacted.map(entryToMessage).filter((m): m is AgentMessage => m !== undefined);
}

function taskName(cwd: string): string {
	const t = process.env.PI_HANDOFF_TASK;
	return t && t.trim() ? t.trim() : basename(cwd) || "task";
}

function nextHop(): number {
	const prev = Number(process.env.PI_HANDOFF_HOP ?? "0");
	return Number.isFinite(prev) && prev > 0 ? prev + 1 : 1;
}

function modelRef(ctx: ExtensionContext): string {
	const m = ctx.model as unknown as { provider?: string; id?: string; name?: string } | undefined;
	const provider = typeof m?.provider === "string" ? m.provider : "";
	const id = typeof m?.id === "string" ? m.id : "";
	if (provider && id) return `${provider}/${id}`;
	return typeof m?.name === "string" ? m.name : "";
}

function notify(ctx: ExtensionContext, text: string, level: "info" | "warning" | "error" = "info") {
	if (ctx.hasUI) ctx.ui.notify(text, level);
}

async function generateContract(ctx: ExtensionContext, goal: string): Promise<string | null> {
	if (!ctx.model) return null;
	const messages = getHandoffMessages(ctx.sessionManager.getBranch());
	if (messages.length === 0) return null;
	const conversationText = serializeConversation(convertToLlm(messages));
	const header =
		"## 会话工作目录\n\n" + ctx.cwd + "\n\n" +
		"## chain\n\nchain:" + ctx.sessionManager.getSessionId() + " hop:" + nextHop() + "\n\n" +
		"## 对话历史\n\n" + conversationText + "\n\n" +
		"## 本次交棒要补充的指示\n\n" + (goal || "（无额外指示：按上面对话历史里未完成的下一步收尾）");
	const userMessage: Message = {
		role: "user",
		content: [{ type: "text", text: header }],
		timestamp: Date.now(),
	};
	const response = await ctx.modelRegistry.complete(
		ctx.model,
		{ systemPrompt: SYSTEM_PROMPT, messages: [userMessage] },
		{ signal: ctx.signal, cacheRetention: "none", sessionId: uuidv7() },
	);
	if (response.stopReason === "aborted") return null;
	const out = response.content
		.filter((c): c is { type: "text"; text: string } => c.type === "text")
		.map((c) => c.text)
		.join("\n")
		.trim();
	if (!out) return null;
	// 标题由程序用真实时间补：模型曾把日期写成 2023-10-27 且任务名编成 task:Ack（幻觉），不能信它写的标题。
	// 标题用本地时间：上一版用 toISOString()（UTC），把本地 09:28 写成 01:28。
	const now = new Date();
	const p2 = (n: number) => String(n).padStart(2, "0");
	const stamp = `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())} ${p2(now.getHours())}:${p2(now.getMinutes())}`;
	return `# Handoff (pi auto-handoff)  ${stamp}  task:${taskName(ctx.cwd)}\n\n` + out;
}

function writeContract(sessionId: string, body: string): string {
	mkdirSync(HANDOFF_DIR, { recursive: true });
	const file = join(HANDOFF_DIR, sessionId + ".md");
	writeFileSync(file, body.endsWith("\n") ? body : body + "\n", "utf8");
	return file;
}

// ---- Relaunch the way this session was started -----------------------------------------
// The next session runs this pi's own command line again (same extensions, providers, session
// directory, …) with the model in use now, so a handoff never switches model or loses the setup
// a launcher made (e.g. the OpenCode free bridge). Dropped: what ties it to this session or
// would replay input. Kept env: launcher configuration, never anything credential-like.
const VALUE_FLAGS = new Set([
	"--provider", "--model", "--api-key", "--system-prompt", "--append-system-prompt", "--mode", "--fork",
	"--session", "--session-id", "--session-dir", "--name", "-n", "--models", "--tools", "-t", "--exclude-tools",
	"-xt", "--thinking", "--extension", "-e", "--skill", "--prompt-template", "--theme", "--use-theme", "--export",
	"--tui-mode",
]);
const DROP_WITH_VALUE = new Set(["--model", "--api-key", "--fork", "--session", "--session-id", "--export"]);
const DROP_FLAGS = new Set([
	"--continue", "-c", "--resume", "-r", "--no-session", "--print", "-p", "--help", "-h", "--version", "-v", "--list-models",
]);
const ENV_KEEP = /^(PI_|OPENCODE_|FLEETMUX_|FNM_|TZ$|PATH$|LANG$|LC_)/;
const ENV_DROP = /^(PI_HANDOFF_|PI_AUTO_HANDOFF_MODEL$)|KEY|TOKEN|SECRET|PASS|AUTH|CREDENTIAL|COOKIE/i;

export function relaunchArgs(argv: string[], model: string): string[] {
	const out: string[] = [];
	for (let i = 0; i < argv.length; i++) {
		const arg = argv[i];
		if (arg === "--") break;  // the rest is an initial message
		const flag = arg.startsWith("-") && arg.includes("=") ? arg.slice(0, arg.indexOf("=")) : arg;
		if (DROP_FLAGS.has(flag)) continue;
		if (DROP_WITH_VALUE.has(flag)) { if (flag === arg) i++; continue; }
		if (!arg.startsWith("-")) continue;  // positional: an initial message
		out.push(arg);
		if (VALUE_FLAGS.has(arg) && i + 1 < argv.length) out.push(argv[++i]);
	}
	return model ? [...out, "--model", model] : out;
}

export function relaunchEnv(env: NodeJS.ProcessEnv): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [name, value] of Object.entries(env)) {
		if (value !== undefined && ENV_KEEP.test(name) && !ENV_DROP.test(name)) out[name] = value;
	}
	return out;
}

const shq = (value: string) => "'" + value.replace(/'/g, "'\\''") + "'";

export function relaunchScript(cwd: string, env: Record<string, string>, command: string[]): string {
	return [
		"#!/bin/bash",
		"# Written by auto-handoff: start the next session the way the handed-off one was started.",
		"cd " + shq(cwd) + " || exit 1",
		...Object.keys(env).sort().map((name) => "export " + name + "=" + shq(env[name])),
		"exec " + command.map(shq).join(" "),
		"",
	].join("\n");
}

function writeRelaunch(sessionId: string, cwd: string, extraEnv: Record<string, string>, model: string): string {
	mkdirSync(HANDOFF_DIR, { recursive: true });
	const file = join(HANDOFF_DIR, sessionId + ".relaunch.sh");
	const command = [process.execPath, process.argv[1], ...relaunchArgs(process.argv.slice(2), model)];
	writeFileSync(file, relaunchScript(cwd, { ...relaunchEnv(process.env), ...extraEnv }, command), { mode: 0o700 });
	return file;
}

function launcherPath(): string {
	const explicit = process.env.PI_AUTO_HANDOFF_LAUNCHER;
	if (explicit && explicit.trim()) return explicit.trim();
	// 自洽部署：扩展与接力启动器同目录（或上一级 launchers/）优先，这样在 Studio 这类
	// 没有 cmux-fleet-pi 仓、没有 PI_* 环境的机器上也能跑。
	const here = dirname(fileURLToPath(import.meta.url));
	const candidates = [
		join(here, "start-pi-autohandoff.command"),
		join(here, "..", "launchers", "start-pi-autohandoff.command"),
		...(FLEET_DIR ? [join(FLEET_DIR, "launchers", "start-pi-autohandoff.command")] : []),
	];
	for (const c of candidates) if (existsSync(c)) return c;
	return candidates[candidates.length - 1];
}

async function startContinuation(
	file: string,
	task: string,
	hop: number,
	cwd: string,
	model: string,
): Promise<boolean> {
	const env: NodeJS.ProcessEnv = {
		...process.env,
		PI_WORKDIR: cwd,
		PI_HANDOFF_FILE: file,
		PI_HANDOFF_TASK: task,
		PI_HANDOFF_HOP: String(hop),
	};
	if (model) env.PI_AUTO_HANDOFF_MODEL = model;
	try {
		env.PI_HANDOFF_RELAUNCH = writeRelaunch(basename(file, ".md"), cwd,
			{ PI_WORKDIR: cwd, PI_HANDOFF_FILE: file, PI_HANDOFF_TASK: task, PI_HANDOFF_HOP: String(hop) }, model);
	} catch (err) {  // the relay falls back to its default launcher
		if (DEBUG) process.stderr.write(`[auto-handoff] relaunch script failed: ${err instanceof Error ? err.message : String(err)}\n`);
	}
	const launcher = launcherPath();
	if (DEBUG) process.stderr.write(`[auto-handoff] spawning launcher=${launcher} file=${file}\n`);
	return new Promise<boolean>((done) => {
		let settled = false;
		const finish = (ok: boolean) => {
			if (settled) return;
			settled = true;
			done(ok);
		};
		try {
			// 用 /bin/bash 包一层：不依赖 .command 的可执行位与 shebang 解析。
			const child = spawn("/bin/bash", [launcher, file], { detached: true, stdio: "ignore", env });
			child.on("spawn", () => {
				if (DEBUG) process.stderr.write(`[auto-handoff] spawned pid=${String(child.pid)}\n`);
				child.unref();
				finish(true);
			});
			child.on("error", (err) => {
				if (DEBUG) process.stderr.write(`[auto-handoff] spawn error: ${err.message}\n`);
				finish(false);
			});
			// 保底：spawn 事件迟迟不来也不卡住交棒。
			setTimeout(() => finish(true), 800);
		} catch (err) {
			if (DEBUG) process.stderr.write(`[auto-handoff] spawn threw: ${err instanceof Error ? err.message : String(err)}\n`);
			finish(false);
		}
	});
}

export default function autoHandoff(pi: ExtensionAPI) {
	let fired = false;
	let busy = false;

	// 接力窗口启动后：读 PI_HANDOFF_FILE 当首条 prompt 注入（每个会话一次）。
	pi.on("session_start", (_event, ctx) => {
		const file = process.env.PI_HANDOFF_FILE;
		if (!file || !existsSync(file)) return;
		try {
			const body = readFileSync(file, "utf8").trim();
			if (!body) return;
			pi.sendUserMessage(body, { deliverAs: "followUp" });
			notify(ctx, "已载入交接单：" + file);
		} catch (err) {
			notify(ctx, "读交接单失败：" + (err instanceof Error ? err.message : String(err)), "error");
		}
	});

	const runHandoff = async (ctx: ExtensionContext, reason: string, goal: string) => {
		if (busy || fired) return;
		busy = true;
		try {
			const task = taskName(ctx.cwd);
			const hop = nextHop();
			notify(ctx, "自动交棒：" + reason + "；正在生成交接单（autohandoff " + hop + ":" + task + "）…");
			const body = await generateContract(ctx, goal);
			if (!body) {
				notify(ctx, "交棒中止：生成交接单失败（无模型或空输出），本会话继续。", "error");
				return;
			}
			const footer = [
				"",
				"---",
				"这是自动接力会话 autohandoff " + hop + ":" + task + " 的任务书。",
				"先核对「下一步（按顺序）」与现场是否一致，再逐条执行；没跑过的验证写「未跑」，禁编。",
				"<!-- parent-session: " + (ctx.sessionManager.getSessionFile() ?? "?") + " -->",
				"",
			].join("\n");
			const sessionId = ctx.sessionManager.getSessionId();
			const file = writeContract(sessionId, body + "\n" + footer);
			const started = await startContinuation(file, task, hop, ctx.cwd, modelRef(ctx));
			fired = true;
			notify(
				ctx,
				"已交棒：cmux 工作区 autohandoff " + hop + ":" + task + "\n交接单 " + file +
					(started ? "" : "\n（启动器 spawn 失败：请手动运行该交接单）") +
					"\n本会话即将退出。",
			);
			// 官方语义：graceful shutdown，deferred until agent is idle。
			ctx.shutdown();
		} catch (err) {
			notify(ctx, "交棒失败：" + (err instanceof Error ? err.message : String(err)), "error");
		} finally {
			busy = false;
		}
	};

	pi.on("turn_end", async (_event, ctx) => {
		if (!ENABLED || fired || busy) return;
		const usage = ctx.getContextUsage();
		if (!usage) return;
		// 官方 trigger-compact.ts 在 turn_end 里直接动作、不做 isIdle 判定；这里同样只记录不拦截。
		if (DEBUG) {
			process.stderr.write(
				`[auto-handoff] turn_end tokens=${String(usage.tokens)} percent=${String(usage.percent)} idle=${String(ctx.isIdle())}\n`,
			);
		}
		const pctHit = usage.percent !== null && usage.percent >= TRIGGER_PCT;
		const tokHit = TRIGGER_TOKENS > 0 && typeof usage.tokens === "number" && usage.tokens >= TRIGGER_TOKENS;
		if (!pctHit && !tokHit) return;
		const reason = tokHit
			? `tokens ${usage.tokens} ≥ ${TRIGGER_TOKENS}`
			: `上下文 ${Math.round(usage.percent as number)}% ≥ ${TRIGGER_PCT}%`;
		await runHandoff(ctx, reason, "");
	});

	pi.registerCommand("auto-handoff", {
		description: "立即交棒：写交接单 + 开新 cmux 接力窗口 + 本会话退出",
		handler: async (args, ctx) => {
			await runHandoff(ctx, "手动 /auto-handoff", args.trim());
		},
	});
}
