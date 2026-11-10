/**
 * Project domains extension: gives a pi session the same per-domain context the
 * Claude Code hooks give in the host project.
 *
 * - Common skills: real (non-symlink) directories under <cwd>/.claude/skills are
 *   registered as pi skills. Symlinks there are domain skills a Claude session
 *   linked in; they are skipped and listed by domain instead.
 * - Domain rules: each prompt (and each read/edit/write path) is sent to
 *   <cwd>/scripts/context/pi_domain.py, which matches config/context_domains.yaml
 *   and returns the domain RULES.md + skill list + memory once per session.
 *
 * No-op in projects without scripts/context/pi_domain.py. Fails open.
 */

import { spawn } from "node:child_process";
import { existsSync, lstatSync, readdirSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const SCRIPT = join("scripts", "context", "pi_domain.py");
const TIMEOUT_MS = 8000;

type DomainResult = { domains: string[]; text: string };

function askDomains(cwd: string, payload: Record<string, unknown>): Promise<DomainResult> {
	const empty: DomainResult = { domains: [], text: "" };
	return new Promise((done) => {
		let out = "";
		const child = spawn("python3", [join(cwd, SCRIPT)], { cwd, env: { ...process.env, CLAUDE_PROJECT_DIR: cwd } });
		const timer = setTimeout(() => child.kill("SIGKILL"), TIMEOUT_MS);
		child.stdout.on("data", (chunk) => (out += chunk));
		child.on("error", () => (clearTimeout(timer), done(empty)));
		child.on("close", () => {
			clearTimeout(timer);
			try {
				const parsed = JSON.parse(out) as DomainResult;
				done(parsed.text ? parsed : empty);
			} catch {
				done(empty);
			}
		});
		child.stdin.end(JSON.stringify(payload));
	});
}

function sessionFields(ctx: ExtensionContext) {
	return { session_id: ctx.sessionManager.getSessionId(), session_file: ctx.sessionManager.getSessionFile() ?? "" };
}

function notify(ctx: ExtensionContext, domains: string[]) {
	if (ctx.hasUI) ctx.ui.notify(`已装领域细则：${domains.join(", ")}`, "info");
}

export default function projectDomains(pi: ExtensionAPI) {
	pi.on("resources_discover", (event) => {
		const dir = join(event.cwd, ".claude", "skills");
		if (!existsSync(join(event.cwd, SCRIPT)) || !existsSync(dir)) return;
		const skillPaths = readdirSync(dir)
			.map((name) => join(dir, name))
			.filter((p) => !lstatSync(p).isSymbolicLink() && existsSync(join(p, "SKILL.md")));
		return { skillPaths };
	});

	pi.on("before_agent_start", async (event, ctx) => {
		if (!existsSync(join(ctx.cwd, SCRIPT)) || !event.prompt) return;
		const res = await askDomains(ctx.cwd, { ...sessionFields(ctx), prompt: event.prompt });
		if (!res.text) return;
		notify(ctx, res.domains);
		return { message: { customType: "project-domain", content: res.text, display: false } };
	});

	pi.on("tool_result", async (event, ctx) => {
		if (!["read", "edit", "write"].includes(event.toolName) || event.isError) return;
		if (!existsSync(join(ctx.cwd, SCRIPT))) return;
		const raw = typeof event.input.path === "string" ? event.input.path : "";
		if (!raw) return;
		const path = isAbsolute(raw) ? raw : resolve(ctx.cwd, raw);
		const res = await askDomains(ctx.cwd, { ...sessionFields(ctx), path });
		if (!res.text) return;
		notify(ctx, res.domains);
		return { content: [...event.content, { type: "text", text: `\n\n${res.text}` }], structuredContent: event.structuredContent };
	});
}
