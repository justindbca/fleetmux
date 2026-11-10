import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, copyFile, chmod, rm, readFile, realpath, access, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { streamOpenCode, stopOpenCode, parseSSE, encodePrompt } from '../extensions/opencode-free/backend.mjs';

async function fixture(mode, fn) {
  const dir = await realpath(await mkdtemp(join(tmpdir(), 'pi-free-test-')));
  const bin = join(dir, 'fake-opencode');
  const record = join(dir, 'record.jsonl');
  await copyFile(new URL('./fixtures/opencode-free-server.mjs', import.meta.url), bin);
  await chmod(bin, 0o700);
  try {
    await fn({ bin, writable: [record], mcpSettleMs: 0, proxyFile: join(dir, 'no-proxy'), env: { BRIDGE_FIXTURE_MODE: mode, BRIDGE_FIXTURE_RECORD: record,
      BRIDGE_FIXTURE_ESCAPE: join(dir, 'escaped.txt') } }, record);
    await stopOpenCode();  // the shared server outlives a request; Pi stops it on shutdown
    const pids = new Set((await readFile(record, 'utf8')).trim().split('\n').map(line => JSON.parse(line).pid));
    for (const pid of pids) assert.throws(() => process.kill(pid, 0), /ESRCH/, 'owned server must be reaped');
  } finally { await stopOpenCode(); await rm(dir, { recursive: true, force: true }); }
}

// The bridge starts OpenCode only inside macOS sandbox-exec and refuses everywhere else (backend.mjs startServer),
// so every sub-test that starts a server needs macOS. Elsewhere that refusal is the contract (last test).
const serverTest = process.platform === 'darwin' ? test
  : (name, fn) => test(name, { skip: 'OpenCode bridge needs macOS sandbox-exec' }, fn);

test('prompt keeps system text and tool results verbatim (multiline, quotes)', () => {
  const text = '引号"\n\t\\';
  const prompt = encodePrompt(text, [{ role: 'toolResult', content: [{ type: 'text', text }], toolCallId: 'c1', toolName: 'read', isError: true }]);
  assert.ok(prompt.includes('## System instructions\n' + text + '\n'));
  assert.ok(prompt.includes('### result of pi_read [c1] (error)\n' + text));
});

test('history is a readable record: past calls are "(called pi_x …)" lines, never message JSON', () => {
  const history = [
    { role: 'user', content: 'plain string' },
    { role: 'assistant', content: [{ type: 'text', text: 'use read' }, { type: 'toolCall', id: 'c1', name: 'read', arguments: { path: 'a' } }] },
    { role: 'toolResult', content: [{ type: 'text', text: 'ok' }], toolCallId: 'c1', toolName: 'read', isError: false },
  ];
  const prompt = encodePrompt('sys', history);
  const conversation = prompt.slice(prompt.indexOf('## Conversation'));
  assert.equal(conversation, '## Conversation\n### user\nplain string\n### assistant\nuse read\n' +
    '(called pi_read [c1] with {"path":"a"})\n### result of pi_read [c1]\nok');
  assert.ok(!/"type"\s*:\s*"toolCall"|"role"\s*:/.test(prompt), 'nothing shaped like an output to imitate');
  assert.match(prompt, /runs nothing/);
  assert.equal(history[1].content[1].name, 'read', 'caller transcript not mutated');
});

test('stub MCP server lists the tools and never runs them', async () => {
  const dir = await realpath(await mkdtemp(join(tmpdir(), 'pi-free-stub-')));
  try {
    const tools = [{ name: 'read', description: 'Read', parameters: { type: 'object', properties: { path: { type: 'string' } } } }];
    await writeFile(join(dir, 'tools.json'), JSON.stringify(tools));
    const child = spawn(process.execPath, [fileURLToPath(new URL('../extensions/opencode-free/mcp-stub.mjs', import.meta.url)), join(dir, 'tools.json')]);
    const lines = createInterface({ input: child.stdout })[Symbol.asyncIterator]();
    const call = async message => { child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n'); return JSON.parse((await lines.next()).value); };
    assert.equal((await call({ id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25' } })).result.protocolVersion, '2025-11-25');
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
    assert.deepEqual((await call({ id: 2, method: 'tools/list' })).result.tools,
      [{ name: 'read', description: 'Read', inputSchema: tools[0].parameters }]);
    assert.equal((await call({ id: 3, method: 'tools/call', params: { name: 'read', arguments: {} } })).result.isError, true);
    assert.equal((await call({ id: 4, method: 'other' })).error.code, -32601);
    child.kill();
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('SSE handles every UTF-8 byte boundary, CRLF, comments and multiline data', async () => {
  const input = Buffer.from(': keepalive\r\ndata: {"type":"中文",\r\ndata: "data":{}}\r\n\r\n');
  const events = [];
  for await (const event of parseSSE(Readable.from([...input].map(b => Buffer.from([b]))))) events.push(event);
  assert.deepEqual(events, [{ type: '中文', data: {} }]);
  await assert.rejects(async () => { for await (const _ of parseSSE(Readable.from([Buffer.from('data: {}')]))) {} }, /Truncated/);
  await assert.rejects(async () => { for await (const _ of parseSSE(Readable.from([Buffer.from('data: nope\n\n')]))) {} }, /Malformed/);
});

serverTest('real SSE delivery arrives BEFORE completion; official model, isolated session, actual usage', async () => {
  await fixture('ok', async (options, record) => {
    const deltas = [], times = [], hooks = [];
    const result = await streamOpenCode('prompt', { ...options,
      onText: delta => { deltas.push(delta); times.push(Date.now()); },
      onPayload: payload => { assert.equal(payload.resume, true); return { ...payload, text: 'replaced' }; },
      onResponse: response => hooks.push(response.status),
      onProviderStreamEvent: event => hooks.push(event.type),
    });
    assert.equal(deltas.length, 2);
    assert.ok(times[1] - times[0] >= 100, 'must not buffer until completion');
    assert.equal(result.usage.input, 10);
    assert.equal(result.text, deltas.join(''));
    assert.ok(hooks.includes(200) && hooks.includes('session.text.delta'));
    const rows = (await readFile(record, 'utf8')).trim().split('\n').map(JSON.parse);
    const create = rows.find(r => r.path === '/api/session');
    assert.equal(create.body.model.id, 'deepseek-v4.1-flash-free');
    assert.deepEqual(create.body.permissions, [{ action: '*', resource: '*', effect: 'ask' }]);
    assert.ok(rows.findIndex(r => r.path === '/api/event') < rows.findIndex(r => r.path?.endsWith('/prompt')));
    assert.equal(rows.find(r => r.path?.endsWith('/prompt')).body.text, 'replaced');
    assert.ok(rows.some(r => r.method === 'DELETE'));
  });
});

serverTest('server is sandboxed: no writes outside its transient directory, own database', async () => {
  await fixture('escape', async (options, record) => {
    await streamOpenCode('prompt', options);
    const row = (await readFile(record, 'utf8')).trim().split('\n').map(JSON.parse).find(r => 'escape' in r);
    assert.equal(row.escape, 'EPERM');
    await assert.rejects(access(options.env.BRIDGE_FIXTURE_ESCAPE));
    assert.equal(row.tmpdir, row.cwd + '/');
    assert.equal(row.db, join(row.cwd, 'opencode.db'));
    assert.match(row.cwd, /pi-opencode-free-/);
  });
});

serverTest('a step whose only tools are names OpenCode lacks is skipped; final step answers', async () => {
  await fixture('unknown-tool', async options => {
    const deltas = [];
    const result = await streamOpenCode('prompt', { ...options, onText: delta => deltas.push(delta) });
    assert.equal(result.text, '你好🌊\nstreamed');
    assert.equal(deltas.join(''), result.text);
    assert.equal(result.usage.input, 10, 'final step usage, not the skipped step or the cumulative total');
  });
});

serverTest('a real OpenCode tool waiting for permission is rejected; the next step answers', async () => {
  await fixture('shell-reject', async (options, record) => {
    const result = await streamOpenCode('prompt', options);
    assert.equal(result.text, '你好🌊\nstreamed');
    const rows = (await readFile(record, 'utf8')).trim().split('\n').map(JSON.parse);
    const reply = rows.find(r => r.path?.endsWith('/permission/per_sh/reply'));
    assert.equal(reply.body.decision, 'reject');
    assert.match(reply.body.message, /pi_\*/);
  });
});

serverTest('a permission already settled by an earlier rejection (404 on reply) is not an error', async () => {
  await fixture('shell-cascade', async (options, record) => {
    assert.equal((await streamOpenCode('prompt', options)).text, '你好🌊\nstreamed');
    assert.ok((await readFile(record, 'utf8')).includes('/permission/per_sh2/reply'), 'second reply was attempted');
  });
});

const readTool = { name: 'read', description: 'Read', parameters: { type: 'object', properties: { path: { type: 'string' } } } };
const rowsOf = async record => (await readFile(record, 'utf8')).trim().split('\n').map(JSON.parse);

serverTest('Pi tools are native: OpenCode-parsed calls are captured, their permission rejected, nothing runs', async () => {
  await fixture('tool', async (options, record) => {
    const deltas = [];
    const result = await streamOpenCode('prompt', { ...options, tools: [readTool], onText: delta => deltas.push(delta) });
    assert.equal(result.text, 'Read it');
    assert.equal(deltas.join(''), 'Read it');
    assert.deepEqual(result.toolCalls, [{ name: 'read', arguments: { path: 'test.txt' } }]);
    assert.equal(result.usage.input, 10);
    const rows = await rowsOf(record);
    const server = rows[0].config.mcp.servers.pi;  // new-style key: the only one that keeps codemode:false
    assert.equal(server.codemode, false);
    assert.match(server.command[1], /mcp-stub\.mjs$/);
    assert.equal(rows.find(r => r.path?.endsWith('/permission/per_0/reply')).body.decision, 'reject');
    assert.ok(rows.findIndex(r => r.path === '/api/mcp') < rows.findIndex(r => r.path?.endsWith('/prompt')), 'MCP ready before prompting');
  });
});

serverTest('several Pi calls in one step all come back; a rejected OpenCode tool in the same step is dropped', async () => {
  await fixture('tools-multi', async options => {
    const result = await streamOpenCode('prompt', { ...options, tools: [readTool] });
    assert.deepEqual(result.toolCalls.map(c => c.arguments.path), ['a', 'b']);
  });
  await fixture('mixed', async (options, record) => {
    const result = await streamOpenCode('prompt', { ...options, tools: [readTool] });
    assert.deepEqual(result.toolCalls, [{ name: 'read', arguments: { path: 'test.txt' } }]);
    assert.equal((await rowsOf(record)).find(r => r.path?.endsWith('/permission/per_1/reply')).body.decision, 'reject');
  });
});

serverTest('malformed Pi calls are dropped; with none valid the next step answers', async () => {
  await fixture('pi-some-bad', async options => {
    assert.deepEqual((await streamOpenCode('prompt', { ...options, tools: [readTool] })).toolCalls, [{ name: 'read', arguments: { path: 'ok' } }]);
  });
  await fixture('pi-bad-args', async options => {
    const result = await streamOpenCode('prompt', { ...options, tools: [readTool] });
    assert.deepEqual(result.toolCalls, []);
    assert.equal(result.text, 'Read it你好🌊\nstreamed');
  });
});

serverTest('a configured proxy (env or user file) is handed to OpenCode only, local traffic excluded', async () => {
  await fixture('ok', async (options, record) => {
    await streamOpenCode('prompt', { ...options, proxyFile: '/nonexistent' });
    assert.equal((await rowsOf(record))[0].proxy, null, 'no proxy configured: direct');
  });
  await fixture('ok', async (options, record) => {
    const url = 'http://user:secret@proxy.example:8003';
    await streamOpenCode('prompt', { ...options, env: { ...options.env, PI_OPENCODE_PROXY: url } });
    const [first] = await rowsOf(record);
    assert.equal(first.proxy, url);
    assert.equal(first.httpProxy, url);
    assert.equal(first.noProxy, '127.0.0.1,localhost');
  });
  const dir = await realpath(await mkdtemp(join(tmpdir(), 'pi-free-proxy-')));
  try {
    await writeFile(join(dir, 'proxy'), 'http://u:p@from-file:8001\n', { mode: 0o600 });
    await fixture('ok', async (options, record) => {
      await streamOpenCode('prompt', { ...options, proxyFile: join(dir, 'proxy') });
      assert.equal((await rowsOf(record))[0].proxy, 'http://u:p@from-file:8001');
    });
  } finally { await rm(dir, { recursive: true, force: true }); }
});

serverTest('waits for the stub MCP server; fails closed when it does not connect; no MCP without tools', async () => {
  await fixture('mcp-slow', async (options, record) => {
    assert.deepEqual((await streamOpenCode('prompt', { ...options, tools: [readTool] })).toolCalls, []);
    assert.ok((await rowsOf(record)).filter(r => r.path === '/api/mcp').length >= 4);
  });
  await fixture('mcp-failed', async options => {
    await assert.rejects(streamOpenCode('prompt', { ...options, tools: [readTool] }), /did not connect.*MCP failed/);
  });
  await fixture('ok', async (options, record) => {
    await streamOpenCode('prompt', options);
    const rows = await rowsOf(record);
    assert.equal(rows[0].config.mcp, undefined);
    assert.ok(!rows.some(r => r.path === '/api/mcp'));
  });
});

serverTest('OpenCode tools run sandboxed or are rejected; the next step answers; text so far stays', async () => {
  for (const [mode, shown] of [['native-ran', ''], ['codemode', ''], ['text-then-tool', 'shown'], ['shell-text', 'shown']]) {
    await fixture(mode, async options => {
      const result = await streamOpenCode('prompt', { ...options, tools: [readTool] });
      assert.equal(result.text, shown + '你好🌊\nstreamed', mode);
      assert.deepEqual(result.toolCalls, []);
    });
  }
});

for (const [mode, pattern] of [['pi-ran', /native tool/], ['unknown-tool-loop', /extra native agent turn/],
  ['wrong-model', /requested free model/], ['error', /403/], ['retry', /provider\.transport: UNABLE_TO_VERIFY_LEAF_SIGNATURE/], ['permission', /native tool/], ['disconnect', /disconnected/]]) {
  serverTest('fails closed and reaps child: ' + mode, async () => {
    await fixture(mode, async options => { await assert.rejects(streamOpenCode('prompt', { ...options, tools: [readTool] }), pattern); });
  });
}

const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
const pidsOf = async record => [...new Set((await rowsOf(record)).map(r => r.pid))];
const until = async (check, ms = 5000) => { for (const end = Date.now() + ms; Date.now() < end; await new Promise(r => setTimeout(r, 50))) if (await check()) return; assert.fail('timed out'); };

serverTest('one server serves many requests; each request gets its own session, swept afterwards', async () => {
  await fixture('ok', async (options, record) => {
    await streamOpenCode('prompt', options);
    await streamOpenCode('prompt', options);
    const [pid] = await pidsOf(record);
    assert.deepEqual(await pidsOf(record), [pid]);
    assert.ok(alive(pid), 'kept for the next request');
    const rows = await rowsOf(record);
    assert.equal(rows.filter(r => r.path === '/api/session' && r.method === 'POST').length, 2);
    assert.equal(rows.filter(r => r.path === '/api/session' && r.method === 'GET').length, 2, 'leftover sweep after each');
  });
});

serverTest('a changed tool list or a dead server gets a fresh server', async () => {
  await fixture('ok', async (options, record) => {
    await streamOpenCode('prompt', { ...options, tools: [readTool] });
    const [first] = await pidsOf(record);
    await streamOpenCode('prompt', { ...options, tools: [readTool, { ...readTool, name: 'write' }] });
    const [, second] = await pidsOf(record);
    assert.ok(second && !alive(first), 'tools changed: old server replaced');
    process.kill(second, 'SIGKILL');
    await until(() => !alive(second));
    await streamOpenCode('prompt', { ...options, tools: [readTool, { ...readTool, name: 'write' }] });
    assert.equal((await pidsOf(record)).length, 3, 'dead server restarted');
  });
});

serverTest('cancelling interrupts and deletes the session but keeps the server', async () => {
  await fixture('hang', async (options, record) => {
    const controller = new AbortController();
    await assert.rejects(streamOpenCode('prompt', { ...options, signal: controller.signal,
      onResponse: () => setTimeout(() => controller.abort(), 50) }), /abort/i);
    const rows = await rowsOf(record);
    assert.ok(rows.some(r => r.path?.endsWith('/interrupt')) && rows.some(r => r.method === 'DELETE'));
    assert.ok(alive(rows[0].pid));
  });
});

serverTest('an idle server stops by itself', async () => {
  await fixture('ok', async (options, record) => {
    await streamOpenCode('prompt', { ...options, idleMs: 100 });
    const [pid] = await pidsOf(record);
    await until(() => !alive(pid));
  });
});

serverTest('after N requests a fresh server is warmed in the background and takes over', async () => {
  await fixture('ok', async (options, record) => {
    await streamOpenCode('prompt', { ...options, tools: [readTool], recycleAfter: 1 });
    const [old] = await pidsOf(record);
    await until(async () => (await pidsOf(record)).length === 2 && !alive(old));  // swapped once warm
    const [, fresh] = await pidsOf(record);
    const warmed = (await rowsOf(record)).filter(r => r.pid === fresh);
    assert.ok(warmed.some(r => r.path === '/api/mcp'), 'tools ready before taking over');
    await streamOpenCode('prompt', { ...options, tools: [readTool], recycleAfter: 1 });
    const prompts = (await rowsOf(record)).filter(r => r.path?.endsWith('/prompt')).map(r => r.pid);
    assert.deepEqual(prompts, [old, fresh]);
  });
});

serverTest('a failed request (not a cancel) also swaps in a fresh server', async () => {
  await fixture('error', async (options, record) => {
    await assert.rejects(streamOpenCode('prompt', { ...options, tools: [readTool] }), /403/);
    await until(async () => (await pidsOf(record)).length === 2);
  });
});

serverTest('cancellation and timeout reap only the owned server', async () => {
  await fixture('hang', async options => {
    const controller = new AbortController();
    // Abort once the server answered (a fixed delay raced the sandboxed fixture's startup).
    const promise = streamOpenCode('prompt', { ...options, signal: controller.signal,
      onResponse: () => setTimeout(() => controller.abort(), 50) });
    await assert.rejects(promise, /abort/i);
  });
  await fixture('startup-hang', async options => {
    await assert.rejects(streamOpenCode('prompt', { ...options, timeoutMs: 1500 }), /timeout|aborted/i);
  });
});

serverTest('missing binary produces a sanitized actionable error', async () => {
  await assert.rejects(streamOpenCode('prompt', { bin: '/no/such/opencode' }), /Cannot start official/);
});

test('without macOS sandbox-exec the bridge refuses to start OpenCode', { skip: process.platform === 'darwin' && 'macOS has sandbox-exec' }, async () => {
  await assert.rejects(streamOpenCode('prompt', { bin: '/no/such/opencode' }), /needs macOS sandbox-exec; refusing to run unsandboxed/);
});
