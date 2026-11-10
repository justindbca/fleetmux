#!/usr/bin/env node
// Local-only deterministic OpenCode fixture. Never contacts a model.
import http from 'node:http';
import { appendFileSync } from 'node:fs';
const mode = process.env.BRIDGE_FIXTURE_MODE ?? 'ok';
const record = value => { if (process.env.BRIDGE_FIXTURE_RECORD) appendFileSync(process.env.BRIDGE_FIXTURE_RECORD, JSON.stringify({ pid: process.pid, ...value }) + '\n'); };
import { readFileSync } from 'node:fs';
record({ pid: process.pid, argv: process.argv.slice(2), config: JSON.parse(readFileSync('opencode.json', 'utf8')),
  proxy: process.env.HTTPS_PROXY ?? null, httpProxy: process.env.HTTP_PROXY ?? null, noProxy: process.env.NO_PROXY ?? null });
if (mode === 'escape') {  // what codemode could do: write outside the transient directory
  let error = null;
  try { appendFileSync(process.env.BRIDGE_FIXTURE_ESCAPE, 'escaped\n'); } catch (exc) { error = exc.code; }
  record({ escape: error, cwd: process.cwd(), tmpdir: process.env.TMPDIR, db: process.env.OPENCODE_DB });
}
if (mode === 'startup-hang') { setInterval(() => {}, 1000); } else {
let events, answer, mcpPolls = 0, answers = 0;
const pending = new Set();  // Pi tool permissions not yet answered
const sessions = new Set();  // a reused server sees many transient sessions
const model = { id: mode === 'wrong-model' ? 'paid-model' : 'deepseek-v4.1-flash-free', providerID: 'opencode' };
const id = 'ses_fixture';
const emit = (type, data = {}) => events?.write('data: ' + JSON.stringify({ type, data: { sessionID: id, ...data } }) + '\n\n');
const tokens = { input: 10, output: 12, reasoning: 2, cache: { read: 3, write: 0 } };
const server = http.createServer(async (req, res) => {
  if (req.headers.authorization !== 'Basic ' + Buffer.from('opencode:local-test-password').toString('base64')) { res.writeHead(401); res.end('{}'); return; }
  let data = '';
  for await (const chunk of req) data += chunk;
  const body = data ? JSON.parse(data) : undefined;
  record({ method: req.method, path: req.url, body });
  res.setHeader('content-type', 'application/json');
  if (req.method === 'DELETE') { sessions.delete(req.url.split('/').pop()); res.writeHead(204); res.end(); return; }
  if (req.url === '/api/session' && req.method === 'GET') { res.end(JSON.stringify({ data: [...sessions].map(id => ({ id })) })); return; }
  if (req.url === '/api/session') { sessions.add(id); res.end(JSON.stringify({ data: { id, model } })); return; }
  if (req.url === '/api/session/' + id + '/interrupt') { res.writeHead(204); res.end(); return; }
  if (req.url === '/api/mcp') {  // the bridge waits for its stub MCP server before prompting
    const status = mode === 'mcp-failed' ? 'failed' : mode === 'mcp-slow' && ++mcpPolls < 4 ? 'pending' : 'connected';
    res.end(JSON.stringify({ data: mcpPolls === 1 && mode === 'mcp-slow' ? [] : [{ name: 'pi', status: { status } }] })); return;
  }
  if (req.url === '/api/event') {
    events = res; res.setHeader('content-type', 'text/event-stream'); res.flushHeaders();
    res.write(': heartbeat\n\ndata: {"type":"server.connected","data":{}}\n\n'); return;
  }
  if (req.url === '/api/session/' + id + '/prompt') {
    res.end(JSON.stringify({ data: { id: 'msg_user' } }));
    // Native tool steps before the real answer (event shapes recorded from OpenCode 2.0.30).
    const native = { 'unknown-tool': 1, 'unknown-tool-loop': 3, 'native-ran': 1, codemode: 1, 'text-then-tool': 1 }[mode] ?? 0;
    for (let n = 0; n < native; n++) {
      const m = 'msg_pre' + n, call = 'call_' + n, name = mode === 'codemode' ? 'execute' : 'bash';
      emit('session.step.started', { assistantMessageID: m, model });
      if (mode === 'text-then-tool') emit('session.text.delta', { assistantMessageID: m, delta: 'shown' });
      emit('session.tool.input.started', { assistantMessageID: m, id: call, name });
      emit('session.tool.input.ended', { assistantMessageID: m, id: call, text: '{"command":"ls"}' });
      emit('session.tool.called', { assistantMessageID: m, id: call, input: { command: 'ls' }, executed: false });
      if (mode === 'native-ran' || mode === 'codemode') {  // ran without a permission request
        emit('session.tool.progress', { assistantMessageID: m, id: call, metadata: {} });
        emit('session.tool.success', { assistantMessageID: m, id: call, content: [], executed: false });
      } else {
        emit('session.tool.failed', { assistantMessageID: m, id: call, executed: false,
          error: { type: 'tool.execution', message: 'No tool named "bash" is currently available. Please use a tool from the available tool list.' } });
      }
      emit('session.step.ended', { assistantMessageID: m, finish: 'tool-calls', tokens: { ...tokens, input: 999 } });
      emit('session.usage.updated', { tokens: { ...tokens, input: 999 } });
    }
    if (mode.startsWith('shell-')) {  // a real OpenCode tool: waits for permission (reply route below)
      emit('session.step.started', { assistantMessageID: 'msg_sh', model });
      if (mode === 'shell-text') emit('session.text.delta', { assistantMessageID: 'msg_sh', delta: 'shown' });
      emit('session.tool.input.started', { assistantMessageID: 'msg_sh', id: 'call_sh', name: 'shell' });
      emit('session.tool.called', { assistantMessageID: 'msg_sh', id: 'call_sh', input: { command: 'ls' }, executed: false });
      emit('permission.asked', { id: 'per_sh', action: 'shell', resources: ['ls'], source: { type: 'tool', messageID: 'msg_sh', id: 'call_sh' } });
      if (mode === 'shell-cascade') {  // a second pending permission in the same step
        emit('session.tool.input.started', { assistantMessageID: 'msg_sh', id: 'call_sh2', name: 'shell' });
        emit('session.tool.called', { assistantMessageID: 'msg_sh', id: 'call_sh2', input: { command: 'pwd' }, executed: false });
        emit('permission.asked', { id: 'per_sh2', action: 'shell', resources: ['pwd'], source: { type: 'tool', messageID: 'msg_sh', id: 'call_sh2' } });
      }
    }
    answer = () => setTimeout(() => {
      const round = answers++;
      emit('session.step.started', { assistantMessageID: 'msg_one', model });
      if (mode === 'hang') return;
      if (mode === 'permission') { emit('permission.requested'); return; }
      if (mode === 'error') { emit('session.execution.failed', { error: { type: 'provider.auth', status: 403 } }); return; }
      if (mode === 'retry') {  // as OpenCode 2.0.30 emitted it behind a TLS-intercepting proxy (2026-10-19)
        emit('session.retry.scheduled', { assistantMessageID: 'msg_one', attempt: 2,
          error: { type: 'provider.transport', message: 'UNABLE_TO_VERIFY_LEAF_SIGNATURE: unable to verify the first certificate' } });
        emit('session.step.started', { assistantMessageID: 'msg_one', model }); return;
      }
      emit('session.reasoning.delta', { assistantMessageID: 'msg_one', delta: 'Thinking…' });
      emit('session.text.delta', { sessionID: 'ses_unrelated', delta: 'DO NOT SHOW' });
      // Pi tool calls of this step, as OpenCode 2.0.30 emitted them for pi_bash (2026-10-19).
      // ASSUMPTION: several calls in one step are emitted together, then their permissions are asked
      // together; recorded for unknown names, not yet for Pi tools (needs one live check).
      const calls = { tool: [['read', { path: 'test.txt' }]], 'tools-multi': [['read', { path: 'a' }], ['read', { path: 'b' }]],
        mixed: [['read', { path: 'test.txt' }], ['shell', { command: 'ls' }]], 'pi-ran': [['read', { path: 'x' }]],
        // null: arguments OpenCode could not parse (the model nested them); the retry step answers
        'pi-bad-args': round ? [] : [['read', null]], 'pi-some-bad': [['read', { path: 'ok' }], ['read', null]] }[mode] ?? [];
      const text = calls.length ? 'Read it' : '你好🌊\nstreamed';
      const middle = Math.floor(text.length / 2);
      emit('session.text.delta', { assistantMessageID: 'msg_one', delta: text.slice(0, middle) });
      setTimeout(() => {
        if (mode === 'disconnect') { events.end(); return; }
        emit('session.text.delta', { assistantMessageID: 'msg_one', delta: text.slice(middle) });
        emit('session.text.ended', { assistantMessageID: 'msg_one', text });
        if (calls.length) {
          calls.forEach(([name, input], n) => {
            const tool = name === 'shell' ? name : 'pi_' + name;
            emit('session.tool.input.started', { assistantMessageID: 'msg_one', id: 'call_' + n, name: tool });
            emit('session.tool.input.ended', { assistantMessageID: 'msg_one', id: 'call_' + n, text: JSON.stringify(input) });
            if (input === null) emit('session.tool.failed', { assistantMessageID: 'msg_one', id: 'call_' + n, executed: false,
              error: { type: 'tool.execution', message: 'Invalid arguments for tool pi_read' } });
            else emit('session.tool.called', { assistantMessageID: 'msg_one', id: 'call_' + n, input, executed: false });
          });
          emit('session.step.streamed', { assistantMessageID: 'msg_one' });
          if (mode === 'pi-ran') { emit('session.tool.success', { assistantMessageID: 'msg_one', id: 'call_0', content: [], executed: false }); return; }
          if (!calls.some(([, input]) => input)) {  // nothing valid: OpenCode ends the step, the model retries
            emit('session.step.ended', { assistantMessageID: 'msg_one', finish: 'tool-calls', tokens });
            answer(); return;
          }
          calls.forEach(([name, input], n) => { if (!input) return; pending.add('per_' + n);
            emit('permission.asked', { id: 'per_' + n, action: name === 'shell' ? name : 'pi_' + name, resources: ['*'],
              source: { type: 'tool', messageID: 'msg_one', id: 'call_' + n } }); });
          return;  // step.ended follows the replies (route below)
        }
        emit('session.step.ended', { assistantMessageID: 'msg_one', finish: 'stop', tokens });
        emit('session.usage.updated', { tokens, cost: 0 });
        emit('session.execution.succeeded');
      }, 180);
    }, 20);
    if (!mode.startsWith('shell-')) answer();
    return;
  }
  if (req.url === '/api/session/' + id + '/permission/per_sh/reply') {
    res.writeHead(204); res.end();
    emit('permission.replied', { requestID: 'per_sh', reply: body.decision });
    for (const [call, request] of mode === 'shell-cascade' ? [['call_sh', 'per_sh'], ['call_sh2', 'per_sh2']] : [['call_sh', 'per_sh']]) {
      if (request !== 'per_sh') emit('permission.replied', { requestID: request, reply: 'reject' });  // settled along with the first
      emit('session.tool.failed', { assistantMessageID: 'msg_sh', id: call, executed: false,
        error: { type: 'permission.rejected', message: body.message } });
    }
    answer();  // OpenCode starts the next step without session.step.ended
    return;
  }
  const reply = req.url.match(/^\/api\/session\/ses_fixture\/permission\/(per_\d+)\/reply$/);
  if (reply && pending.delete(reply[1])) {
    res.writeHead(204); res.end();
    emit('permission.replied', { requestID: reply[1], reply: body.decision });
    const native = mode === 'mixed' && reply[1] === 'per_1';  // shell: OpenCode's own tool
    emit('session.tool.failed', { assistantMessageID: 'msg_one', id: 'call_' + reply[1].slice(4), executed: false,
      error: native ? { type: 'permission.rejected', message: body.message } : { type: 'tool.execution', message: 'Unable to execute pi_read' } });
    if (!pending.size) emit('session.step.ended', { assistantMessageID: 'msg_one', finish: 'tool-calls', tokens });
    return;
  }
  res.writeHead(404); res.end('{}');
});
server.listen(0, '127.0.0.1', () => {
  console.log('server listening on http://127.0.0.1:' + server.address().port);
  console.log('server password local-test-password');
});
}
