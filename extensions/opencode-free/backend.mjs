// Official OpenCode local API only. No inference endpoint, OAuth copying, or paid fallback.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { homedir, tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createInterface } from 'node:readline';

export const MODEL_ID = 'deepseek-v4.1-flash-free';
export const PROVIDER_ID = 'opencode-cli';
export const MODEL_REF = Object.freeze({ id: MODEL_ID, providerID: 'opencode' });
const MAX_RESPONSE = 16 * 1024 * 1024;
const MAX_STEPS = 3;  // first step + at most two retries after native-tool slips
const MAX_TOOL_CALLS = 32;
const MCP_READY_MS = 20000;  // measured 3-6 s from server start to connected
// OpenCode adds a connected server's tools to its registry asynchronously and signals nothing
// (no event, /api/mcp already says connected). A prompt sent right away ran its first step
// without the pi_* tools; 2 s later it had them (2026-10-19, OpenCode 2.0.30). Stopgap until the
// bridge reuses a warm server (then only the first request waits).
const MCP_SETTLE_MS = 2000;

// Pi's tools reach the model as OpenCode-native tools served by a stub MCP server named
// MCP_SERVER (mcp-stub.mjs). OpenCode names them `${MCP_SERVER}_<tool>`; the model calls them
// natively with structured arguments, the bridge captures each call from the event stream and
// rejects its permission request, so nothing runs inside OpenCode. Pi runs the call and sends
// the result in the next transcript. The transcript uses the same names.
const MCP_SERVER = 'pi';
export const TOOL_PREFIX = MCP_SERVER + '_';

// The history is rendered as a readable record, not as message JSON: with JSON the model once
// continued the pattern and wrote its tool calls as text (`[{"type":"toolCall",…}]`, 10-19 05:41),
// so nothing ran. Past calls appear as "(called pi_x [id] with {…})" lines that the instructions
// mark as records only; real calls go through the native pi_* tools.
const textOf = content => typeof content === 'string' ? content :
  content.filter(block => block.type === 'text').map(block => block.text).join('\n');

export function encodePrompt(system, messages) {
  const alias = name => TOOL_PREFIX + name;
  const turns = messages.map(message => {
    if (message.role === 'toolResult') {
      return `### result of ${alias(message.toolName)} [${message.toolCallId}]${message.isError ? ' (error)' : ''}\n` + textOf(message.content);
    }
    const calls = Array.isArray(message.content) ? message.content.filter(block => block.type === 'toolCall')
      .map(call => `(called ${alias(call.name)} [${call.id}] with ${JSON.stringify(call.arguments)})`) : [];
    return `### ${message.role}\n` + [textOf(message.content), ...calls].filter(Boolean).join('\n');
  });
  return [
    'You are the model for a Pi coding-agent session. Below are its system instructions and the conversation so far;',
    "write the assistant's next turn.",
    `Act only through the ${TOOL_PREFIX}* tools, called natively. Pi runs them and returns their results in the next turn;`,
    'never invent tool results. Do not use any other tool. When finished, answer in plain text.',
    '"(called …)" lines only record earlier calls: writing such a line, or any tool call as text, runs nothing.',
    '',
    '## System instructions',
    system,
    '',
    '## Conversation',
    ...turns,
  ].join('\n');
}

// OpenCode's codemode runs its inner tools (worktree_remove, session_move, ...) without any
// permission request, and no config policy can stop it. So the server process may only write
// its own transient directory and OpenCode's log and shell-service directories.
export function sandboxProfile(writable) {
  const quote = path => '"' + path.replace(/[\\"]/g, '\\$&') + '"';
  return ['(version 1)', '(allow default)', '(deny file-write*)',
    '(allow file-write* (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper")',
    '  (regex #"^/dev/tty") (regex #"^/dev/fd/")',
    ...writable.map(path => '  (subpath ' + quote(path) + ')'), ')'].join('\n');
}

// Only used for an input-budget precheck; final usage comes from OpenCode SSE.
export function estimateTokens(text) { return Math.ceil(Buffer.byteLength(text, 'utf8') / 3); }

async function stopChild(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  const exited = new Promise(resolve => child.once('close', resolve));
  const kill = signal => {
    try { process.kill(-child.pid, signal); } catch { try { child.kill(signal); } catch {} }
  };
  kill('SIGTERM');
  const timer = setTimeout(() => kill('SIGKILL'), 1500);
  try { await exited; } finally { clearTimeout(timer); }
}

async function ready(child, signal) {
  return new Promise((resolve, reject) => {
    let url, password;
    const readers = [child.stdout, child.stderr].map(input => createInterface({ input }));
    const cleanup = () => {
      clearTimeout(timer);
      signal.removeEventListener('abort', aborted);
      child.removeListener('error', failed);
      child.removeListener('exit', exited);
      for (const reader of readers) reader.close();
    };
    const finish = (error) => { cleanup(); error ? reject(error) : resolve({ url, password }); };
    const aborted = () => finish(signal.reason ?? new Error('Aborted'));
    const failed = () => finish(new Error('Cannot start official OpenCode CLI; check PI_OPENCODE_BIN.'));
    const exited = code => finish(code === 71  // sandbox-exec could not exec the binary
      ? new Error('Cannot start official OpenCode CLI; check PI_OPENCODE_BIN.')
      : new Error('OpenCode server exited before readiness (logs withheld to protect credentials).'));
    const timer = setTimeout(() => finish(new Error('OpenCode server startup timed out.')), 20000);
    child.once('error', failed);
    child.once('exit', exited);
    signal.addEventListener('abort', aborted, { once: true });
    for (const reader of readers) reader.on('line', line => {
      // Never forward startup lines: OpenCode prints the local server password.
      url ??= line.match(/^server listening on (http:\/\/127\.0\.0\.1:\d+)\s*$/)?.[1];
      password ??= line.match(/^server password (\S+)\s*$/)?.[1];
      if (url && password) finish();
    });
    if (signal.aborted) aborted();
  });
}

async function readJson(response) {
  const chunks = [];
  let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > MAX_RESPONSE) throw new Error('OpenCode response exceeds 16 MiB.');
    chunks.push(chunk);
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw new Error('OpenCode returned malformed HTTP JSON.'); }
}

// SSE frames may split in the middle of UTF-8, CRLF, JSON escapes, or data lines.
export async function* parseSSE(body) {
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let buffer = '', data = [];
  for await (const chunk of body) {
    buffer += decoder.decode(chunk, { stream: true });
    if (Buffer.byteLength(buffer) > MAX_RESPONSE) throw new Error('Oversized SSE frame.');
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, end).replace(/\r$/, '');
      buffer = buffer.slice(end + 1);
      if (line === '') {
        if (data.length) {
          let event;
          try { event = JSON.parse(data.join('\n')); }
          catch { throw new Error('Malformed OpenCode SSE JSON.'); }
          data = [];
          yield event;
        }
      } else if (line.startsWith('data:')) {
        data.push(line.slice(5).replace(/^ /, ''));
        if (data.reduce((n, s) => n + s.length, 0) > MAX_RESPONSE) throw new Error('Oversized SSE event.');
      }
    }
  }
  buffer += decoder.decode();
  if (buffer.trim() || data.length) throw new Error('Truncated OpenCode SSE frame.');
}

// One sandboxed `opencode serve` per Pi process, reused by every request (each request gets its
// own transient session). Starting it costs 1-2 s, its stub MCP server ~1 s and the tool settle
// 2 s; now only the first request pays. Rebuilt when the launch inputs or Pi's tools change or
// the process died; stopped after IDLE_MS without requests, by stopOpenCode() and on exit.
// Kept clean: after each request leftover sessions are swept; after RECYCLE_AFTER requests or a
// failed one, a fresh server is warmed up in the background (MCP connected, tools settled) and
// takes over only when ready, so no call waits for the swap.
const IDLE_MS = 15 * 60 * 1000;
const RECYCLE_AFTER = 50;
let server, spare;

// Plain JSON call for the bridge's own housekeeping (no provider error mapping).
async function api(started, method, path, body, signal, fetchImpl = fetch) {
  const response = await fetchImpl(started.endpoint.url + path, { method, redirect: 'error', signal,
    headers: { 'content-type': 'application/json',
      authorization: 'Basic ' + Buffer.from('opencode:' + started.endpoint.password).toString('base64') },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) { await response.arrayBuffer(); throw new Error(`OpenCode HTTP ${response.status}`); }
  return response.status === 204 ? undefined : readJson(response);
}

// Without the stub connected the model would see no Pi tools: fail closed, no fallback.
// Needs a session (it boots the location); once per server.
async function waitForTools(started, options, signal) {
  for (const deadline = Date.now() + MCP_READY_MS; ;) {
    const state = (await api(started, 'GET', '/api/mcp', undefined, signal, options.fetch))?.data
      ?.find(entry => entry.name === MCP_SERVER)?.status?.status;
    if (state === 'connected') { await new Promise(resolve => setTimeout(resolve, options.mcpSettleMs ?? MCP_SETTLE_MS)); return; }
    if (state === 'failed' || state === 'needs_auth' || state === 'disabled' || Date.now() > deadline) {
      throw new Error(`Pi tools did not connect to OpenCode (MCP ${state ?? 'missing'}); no tools executed.`);
    }
    await new Promise(resolve => setTimeout(resolve, 250));
    signal.throwIfAborted();
  }
}

async function stopStarted(started) {
  clearTimeout(started.idle);
  process.removeListener('exit', started.reap);
  await stopChild(started.child);
  await rm(started.directory, { recursive: true, force: true });
}

function recycle(old, options, tools) {
  if (spare || old.retired) return;
  const signal = AbortSignal.timeout(MCP_READY_MS + 30000);
  const entry = { key: old.key, start: (async () => {
    const started = await startServer(old.key, options, tools, signal, await proxyUrl(options));
    try {
      if (tools.length) {
        const warm = await api(started, 'POST', '/api/session', { title: 'Pi Free bridge (warm-up)', model: MODEL_REF,
          agent: 'build', location: { directory: started.directory }, permissions: [] }, signal, options.fetch);
        await waitForTools(started, options, signal);
        await api(started, 'DELETE', `/api/session/${warm.data.id}`, undefined, signal, options.fetch);
        started.mcp = Promise.resolve();
      }
      return started;
    } catch (error) { await stopStarted(started); throw error; }
  })() };
  spare = entry;
  entry.start.then(async () => {
    if (spare !== entry) return;
    spare = undefined;
    if (server?.key !== old.key) { await stopOpenCode(entry); return; }
    const previous = server;
    server = entry;
    old.retired = true;
    if (!old.active) await previous.start.then(stopStarted, () => {});
  }, () => { if (spare === entry) spare = undefined; });
}

// OpenCode judges free-tier abuse per egress IP, so the bridge can send OpenCode through a fixed
// proxy (e.g. a dedicated Toronto ISP IP). The URL carries credentials: it lives in a user-only
// file (or PI_OPENCODE_PROXY), never in launcher env, logs or handoff relaunch scripts. Configured
// but unusable means requests fail; there is no silent fallback to the direct connection.
export const PROXY_FILE = join(homedir(), '.config', 'pi-opencode-free', 'proxy');

async function proxyUrl(options) {
  const configured = options.env?.PI_OPENCODE_PROXY ?? process.env.PI_OPENCODE_PROXY ??
    (await readFile(options.proxyFile ?? PROXY_FILE, 'utf8').catch(() => '')).trim();
  return configured || undefined;
}

async function startServer(key, options, tools, signal, proxy) {
  if (process.platform !== 'darwin') throw new Error('OpenCode bridge needs macOS sandbox-exec; refusing to run unsandboxed.');
  // Real path: the sandbox matches /private/var/..., not the /var symlink.
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'pi-opencode-free-')));
  let child;
  try {
    // Preserve the official built-in agent. Native permission requests are never approved.
    // `mcp.servers` (not the legacy `mcp.<name>`): only that form keeps codemode:false, i.e.
    // Pi's tools as direct native tools rather than inside OpenCode's codemode.
    if (tools.length) await writeFile(join(directory, 'tools.json'), JSON.stringify(tools), { mode: 0o600 });
    await writeFile(join(directory, 'opencode.json'), JSON.stringify({
      share: 'disabled', update: 'disable', snapshots: false, compaction: { auto: false },
      ...(tools.length ? { mcp: { servers: { [MCP_SERVER]: { type: 'local', codemode: false,
        command: [process.execPath, fileURLToPath(new URL('./mcp-stub.mjs', import.meta.url)), join(directory, 'tools.json')] } } } } : {}),
    }), { mode: 0o600 });
    const data = join(process.env.XDG_DATA_HOME || join(homedir(), '.local', 'share'), 'opencode');
    const profile = sandboxProfile([directory, join(data, 'log'), join(data, 'shell'), ...(options.writable ?? [])]);
    child = spawn('/usr/bin/sandbox-exec', ['-p', profile, options.bin ?? 'opencode', 'serve', '--hostname', '127.0.0.1', '--port', '0'], {
      // Own database and temp files: codemode cannot touch the user's OpenCode sessions.
      cwd: directory, env: { ...process.env, ...options.env, TMPDIR: directory + '/', OPENCODE_DB: join(directory, 'opencode.db'),
        ...(proxy ? { HTTPS_PROXY: proxy, HTTP_PROXY: proxy, NO_PROXY: '127.0.0.1,localhost' } : {}) },
      stdio: ['ignore', 'pipe', 'pipe'], detached: true,
    });
    const endpoint = await ready(child, signal);
    // Keep draining output without printing or retaining it.
    child.stdout.resume(); child.stderr.resume();
    const reap = () => { try { process.kill(-child.pid, 'SIGTERM'); } catch {} };
    process.once('exit', reap);  // a long-lived server must not outlive Pi
    return { key, child, endpoint, directory, reap, active: 0, served: 0 };
  } catch (error) {
    if (child?.pid) await stopChild(child);
    await rm(directory, { recursive: true, force: true });
    throw error;
  }
}

export async function stopOpenCode(only) {
  const entries = only ? [only] : [server, spare];
  if (!only) server = spare = undefined;
  for (const entry of entries) {
    const started = await entry?.start.catch(() => undefined);
    if (started) await stopStarted(started);
  }
}

async function acquire(options, tools, signal) {
  const proxy = await proxyUrl(options);
  const key = JSON.stringify([options.bin ?? 'opencode', options.env ?? {}, options.writable ?? [], tools, proxy ?? '']);
  const alive = await server?.start.then(s => s.child.exitCode === null && s.child.signalCode === null, () => false);
  if (server && (server.key !== key || !alive)) await stopOpenCode();
  if (!server) {
    const entry = { key, start: startServer(key, options, tools, signal, proxy) };
    server = entry;
    entry.start.catch(() => { if (server === entry) server = undefined; });
  }
  const started = await server.start;
  clearTimeout(started.idle);
  started.active++;
  return started;
}

function release(started, options, tools, failed) {
  started.active--;
  if (started.retired) { if (!started.active) stopStarted(started); return; }
  if (++started.served >= (options.recycleAfter ?? RECYCLE_AFTER) || failed) recycle(started, options, tools);
  if (started.active || server?.key !== started.key) return;
  started.idle = setTimeout(() => { if (server?.key === started.key) stopOpenCode(); }, options.idleMs ?? IDLE_MS);
  started.idle.unref();
}

export async function streamOpenCode(prompt, options = {}) {
  const timeout = AbortSignal.timeout(options.timeoutMs ?? 180000);
  const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
  signal.throwIfAborted();
  const tools = options.tools ?? [];
  const declared = new Set(tools.map(tool => tool.name));
  const current = await acquire(options, tools, signal);
  const { endpoint } = current;
  const local = new AbortController();  // closes this request's event stream on a reused server
  const streamSignal = AbortSignal.any([signal, local.signal]);
  let sessionID, eventResponse, completed = false, failed = false;
  const headers = () => ({ 'content-type': 'application/json',
    authorization: 'Basic ' + Buffer.from('opencode:' + endpoint.password).toString('base64') });
  const request = async (method, path, body, requestSignal = signal, observe = false) => {
    const response = await (options.fetch ?? fetch)(endpoint.url + path, {
      method, redirect: 'error', signal: requestSignal,
      headers: headers(),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (observe) await options.onResponse?.({ status: response.status, headers: Object.fromEntries(response.headers) });
    if (!response.ok) {
      // Preserve operational error classes, not provider payloads or possible credentials.
      const error = await readJson(response);
      const message = String(error?.message ?? '');
      const category = /free tier|FreeTier/i.test(message) ? 'free tier unavailable in this OpenCode API' :
        /context|token.*limit/i.test(message) ? 'context_length_exceeded' :
        /rate|quota|limit/i.test(message) ? 'rate limit or quota exceeded' :
        /auth|credential|login|sign in/i.test(message) ? 'authentication required; sign in using OpenCode' : 'request rejected';
      throw new Error(`OpenCode HTTP ${response.status}: ${category}. No model fallback.`);
    }
    if (response.status === 204) return;
    return readJson(response);
  };
  // Rejecting one permission can settle the other pending ones of the same step; a reply to a
  // request that no longer exists answers 404, which means it is already resolved.
  const reject = async (requestID, message) => {
    const response = await (options.fetch ?? fetch)(endpoint.url + `/api/session/${sessionID}/permission/${requestID}/reply`, {
      method: 'POST', redirect: 'error', signal, headers: headers(), body: JSON.stringify({ decision: 'reject', message }) });
    await response.arrayBuffer();
    if (!response.ok && response.status !== 404) throw new Error(`OpenCode HTTP ${response.status}: permission reply rejected.`);
  };
  try {
    const created = await request('POST', '/api/session', {
      title: 'Pi Free bridge (transient)', model: MODEL_REF, agent: 'build',
      location: { directory: current.directory }, permissions: [{ action: '*', resource: '*', effect: 'ask' }],
    });
    sessionID = created?.data?.id;
    if (typeof sessionID !== 'string' || !/^ses_[a-zA-Z0-9]+$/.test(sessionID)) {
      sessionID = undefined;
      throw new Error('OpenCode returned an invalid session ID.');
    }
    if (created.data.model?.id !== MODEL_ID || created.data.model?.providerID !== 'opencode') {
      throw new Error('OpenCode did not select the requested free model.');
    }
    if (tools.length) {  // once per server: later requests find the tools registered
      current.mcp ??= waitForTools(current, options, signal);
      await current.mcp.catch(error => { current.mcp = undefined; throw error; });
    }
    let payload = { text: prompt, resume: true };
    const replacement = await options.onPayload?.(payload);
    if (replacement !== undefined) payload = replacement;
    if (!payload || typeof payload.text !== 'string' || payload.resume !== true ||
        Object.keys(payload).sort().join(',') !== 'resume,text') {
      throw new Error('Bridge payload must contain only text and resume:true.');
    }
    // Subscribe BEFORE admitting the prompt, so no initial deltas are lost.
    eventResponse = await (options.fetch ?? fetch)(endpoint.url + '/api/event', {
      headers: headers(), redirect: 'error', signal: streamSignal,
    });
    await options.onResponse?.({ status: eventResponse.status, headers: Object.fromEntries(eventResponse.headers) });
    if (!eventResponse.ok || !eventResponse.headers.get('content-type')?.includes('text/event-stream')) {
      throw new Error('OpenCode SSE endpoint unavailable; no buffered fallback.');
    }
    let text = '', stepText = '', usage, finished = false, messageID, steps = 0, toolCalls;
    // Pi tool calls: captured from the events (OpenCode parsed the arguments), their permission
    // rejected so nothing runs; the step's end returns them to Pi.
    const piCalls = new Map();
    const piToolEvent = (type, d) => {
      if (type === 'session.tool.input.started' && d.name?.startsWith(TOOL_PREFIX) &&
          declared.has(d.name.slice(TOOL_PREFIX.length))) return !!piCalls.set(d.id, { name: d.name.slice(TOOL_PREFIX.length) });
      if (!piCalls.has(d.id)) return false;
      if (type === 'session.tool.input.ended') return true;
      if (type === 'session.tool.called' && d.executed === false && d.input !== null &&
          typeof d.input === 'object' && !Array.isArray(d.input)) return !!(piCalls.get(d.id).arguments = d.input);
      return type === 'session.tool.failed' && d.executed === false;  // after our rejection
    };
    // OpenCode's own tools (its injected instructions steer the model to shell / read / codemode).
    // Anything asking permission is rejected; what runs without asking (read-only tools, codemode)
    // runs inside the sandbox with a throwaway database, i.e. less than Pi's own bash may do.
    // Unknown names (bash, ...) just fail. OpenCode then reports the outcome to the model and
    // starts another step, where it usually calls the pi_* tools; text so far stays shown.
    const native = new Set(), replies = new Set();
    let nativeSeen = 0;
    const nativeToolEvent = (type, d) => {
      if (type === 'session.tool.input.started') return !!native.add(d.id) && !!++nativeSeen;
      if (!native.has(d.id)) return false;
      if (type === 'session.tool.success' || type === 'session.tool.failed') native.delete(d.id);
      return true;
    };
    const canContinue = () => nativeSeen && !native.size;
    // A Pi call OpenCode could not parse (malformed arguments) never gets valid arguments; OpenCode
    // reports that to the model, which usually resends it in the next step. Hand Pi the valid calls;
    // when none is valid, continue like after an OpenCode tool.
    const validCalls = () => [...piCalls.values()].filter(call => call.arguments);
    const dropInvalidStep = () => { if (piCalls.size && !validCalls().length) { piCalls.clear(); nativeSeen++; } };
    const consume = (async () => {
      for await (const event of parseSSE(eventResponse.body)) {
        if (event.data?.sessionID !== sessionID) continue;
        await options.onProviderStreamEvent?.(event);
        const d = event.data;
        // A failed provider call (TLS, network, rate limit) is retried by OpenCode as another step
        // on the same message; stop here with its real cause rather than at that step's start.
        if (event.type === 'session.retry.scheduled') {
          const type = /^[a-zA-Z0-9._-]+$/.test(d.error?.type ?? '') ? d.error.type : 'retry';
          const cause = String(d.error?.message ?? '').replace(/[\x00-\x1f\x7f]/g, ' ').slice(0, 300);
          throw new Error(`OpenCode ${type}: ${cause || 'provider call failed'}; no model fallback.`);
        }
        if (event.type === 'session.step.started') {
          if (messageID) dropInvalidStep();
          if (messageID && canContinue()) { messageID = undefined; nativeSeen = 0; }  // no step.ended after a rejection
          if (messageID || ++steps > MAX_STEPS || d.model?.id !== MODEL_ID || d.model?.providerID !== 'opencode') {
            throw new Error('Unexpected OpenCode model or extra native agent turn.');
          }
          messageID = d.assistantMessageID;
          stepText = '';
        }
        if (d.assistantMessageID && d.assistantMessageID !== messageID) {
          throw new Error('OpenCode stream message identity mismatch.');
        }
        if (event.type === 'permission.asked' && d.source?.type === 'tool' && piCalls.has(d.source.id)) {
          replies.add(d.id);
          await reject(d.id, 'Pi runs this tool and returns the result in the next turn.');
        } else if (event.type === 'permission.asked' && d.source?.type === 'tool' && native.has(d.source.id)) {
          replies.add(d.id);
          await reject(d.id, `Pi did not run this tool: OpenCode tools are unavailable here. Use only the ${TOOL_PREFIX}* tools.`);
        } else if (event.type === 'permission.replied' && replies.has(d.requestID) && d.reply === 'reject') {
          // our own rejection, echoed back
        } else if (event.type.includes('permission') || event.type.startsWith('session.tool.') &&
            !piToolEvent(event.type, d) && !nativeToolEvent(event.type, d)) {
          throw new Error('OpenCode attempted a native tool; request stopped. Only Pi tools may execute.');
        }
        if (event.type === 'session.text.delta') {
          if (typeof d.delta !== 'string') throw new Error('Invalid text delta.');
          text += d.delta; stepText += d.delta;
          if (Buffer.byteLength(text) > MAX_RESPONSE) throw new Error('OpenCode output exceeds 16 MiB.');
          await options.onText?.(d.delta);
        }
        if (event.type === 'session.reasoning.delta') {
          if (typeof d.delta !== 'string') throw new Error('Invalid reasoning delta.');
          await options.onThinking?.(d.delta);
        }
        if (event.type === 'session.text.ended' && d.text !== stepText) {
          throw new Error('OpenCode text delta mismatch; incomplete stream.');
        }
        if (event.type === 'session.step.ended' && validCalls().length) {
          // OpenCode's own tools in the same step (rejected or sandboxed) are dropped; Pi's calls go to Pi.
          toolCalls = validCalls();
          if (toolCalls.length > MAX_TOOL_CALLS) {
            throw new Error('OpenCode reported too many Pi tool calls; no tools executed.');
          }
          usage = d.tokens;
          return;  // OpenCode would start another step after the rejections; finally interrupts it
        }
        if (event.type === 'session.step.ended') dropInvalidStep();
        if (event.type === 'session.step.ended') {
          if (d.finish === 'tool-calls' && canContinue()) {
            messageID = undefined; nativeSeen = 0;  // wait for the next step
          } else if (d.finish !== 'stop') {
            throw new Error(`OpenCode did not finish normally (${d.finish}); no tools executed.`);
          } else {
            finished = true;
            usage = d.tokens;
          }
        }
        // Cumulative over steps; after a skipped step the final step's own tokens are the context size.
        if (event.type === 'session.usage.updated' && steps === 1) usage = d.tokens;
        // session.tool.failed got here only as an unknown native tool (checked above).
        if (event.type.endsWith('.failed') && !event.type.startsWith('session.tool.') || event.type === 'session.execution.interrupted') {
          const type = /^[a-zA-Z0-9._-]+$/.test(d.error?.type ?? '') ? d.error.type : 'failed';
          throw new Error(`OpenCode ${type}${d.error?.status ? ' HTTP ' + Number(d.error.status) : ''}; no model fallback.`);
        }
        if (event.type === 'session.execution.succeeded') {
          if (!finished || !usage) throw new Error('OpenCode stream lacks final step/usage.');
          completed = true;
          return;
        }
      }
      throw new Error('OpenCode SSE disconnected before completion; no tools executed.');
    })();
    // Attach a rejection handler immediately while POST is in flight.
    consume.catch(() => {});
    await request('POST', `/api/session/${sessionID}/prompt`, payload);
    await consume;
    signal.throwIfAborted();
    if (toolCalls && !usage) throw new Error('OpenCode stream lacks step usage.');
    return { text, usage, toolCalls: toolCalls ?? [] };
  } catch (error) {
    failed = !signal.aborted;  // the user cancelling is not a server problem
    throw error;
  } finally {
    local.abort();
    // Only our transient session: stop it if it is still running, then delete it. The server stays.
    if (sessionID) {
      if (!completed) await request('POST', `/api/session/${sessionID}/interrupt`, {}, AbortSignal.timeout(1500)).catch(() => {});
      await request('DELETE', `/api/session/${sessionID}`, undefined, AbortSignal.timeout(1500)).catch(() => {});
    }
    if (current.active === 1) {  // sweep whatever a session left behind (only when no other request runs)
      const listed = await api(current, 'GET', '/api/session', undefined, AbortSignal.timeout(1500), options.fetch).catch(() => undefined);
      for (const leftover of listed?.data ?? []) {
        await api(current, 'DELETE', `/api/session/${leftover.id}`, undefined, AbortSignal.timeout(1500), options.fetch).catch(() => {});
      }
    }
    release(current, options, tools, failed);
  }
}
