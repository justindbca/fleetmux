// Imports a temporary copy with installed Pi dependencies; no real model request.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { mkdtemp, copyFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
const extension = await import(pathToFileURL(process.argv[2]).href);
let config, provider;
const handlers = new Map();
extension.default({ registerProvider: (p, c) => { provider = p; config = c; }, on: (event, handler) => handlers.set(event, handler) });
const model = { ...config.models[0], provider, api: config.api, baseUrl: config.baseUrl };
const dir = await mkdtemp(join(tmpdir(), 'pi-free-provider-'));
const bin = join(dir, 'fake-opencode');
await copyFile(new URL('./fixtures/opencode-free-server.mjs', import.meta.url), bin); await chmod(bin, 0o700);
const context = { messages: [
  { role: 'system', content: 'Be concise.', timestamp: Date.now(), toolsAdded: [{ name: 'read', description: 'Read', parameters: { type: 'object', properties: { path: { type: 'string' } } } }] },
  { role: 'user', content: [{ type: 'text', text: 'Hello' }], timestamp: Date.now() },
] };
try {
  for (const mode of ['ok', 'tool', 'tools-multi', 'disconnect', 'error']) {
    const events = [];
    const stream = config.streamSimple(model, context, { env: { PI_OPENCODE_BIN: bin, BRIDGE_FIXTURE_MODE: mode } });
    for await (const event of stream) events.push(structuredClone(event));
    const result = await stream.result();
    const terminal = events.filter(e => ['done', 'error'].includes(e.type));
    assert.equal(terminal.length, 1);
    assert.ok(events.filter(e => e.type === 'start').length <= 1);
    if (mode === 'tools-multi') {
      assert.equal(result.stopReason, 'toolUse', result.errorMessage);
      assert.deepEqual(events.filter(e => e.type === 'toolcall_end').map(e => e.toolCall.arguments), [{ path: 'a' }, { path: 'b' }]);
    } else if (['ok', 'tool'].includes(mode)) {
      assert.equal(result.stopReason, mode === 'tool' ? 'toolUse' : 'stop', result.errorMessage);
      assert.equal(result.usage.totalTokens, 27);
      const text = events.filter(e => e.type === 'text_delta').map(e => e.delta).join('');
      assert.equal(text, mode === 'tool' ? 'Read it' : '你好🌊\nstreamed');
      assert.ok(events.some(e => e.type === 'thinking_delta'));
      if (mode === 'tool') assert.equal(events.find(e => e.type === 'toolcall_end').toolCall.name, 'read');
    } else {
      assert.equal(result.stopReason, 'error');
      assert.ok(!events.some(e => e.type.startsWith('toolcall')));
    }
  }
  // Pi compacts on its own at contextWindow - reserveTokens (default 16384). The window must be the
  // bridge's input budget (OpenCode: 200k context, 32k kept for output) so Pi compacts a full reserve
  // below it; otherwise one round's growth crosses the budget and the bridge rejects first
  // (10-16: three such errors; 10-19: equal lines left no room for growth).
  assert.equal(config.models[0].contextWindow, 200000 - 32000);
  for (const [tokens, ok] of [[167000, true], [169000, false]]) {  // the precheck stays at the budget
    const big = { messages: [{ ...context.messages[0], toolsAdded: [] },
      { role: 'user', content: [{ type: 'text', text: 'a'.repeat(tokens * 3) }], timestamp: Date.now() }] };
    const result = await config.streamSimple(model, big, { env: { PI_OPENCODE_BIN: bin, BRIDGE_FIXTURE_MODE: 'ok' } }).result();
    assert.equal(result.stopReason, ok ? 'stop' : 'error', tokens + ': ' + result.errorMessage);
    if (!ok) assert.match(result.errorMessage, /context_length_exceeded/);
  }
  const imageContext = { messages: [{ role: 'user', content: [{ type: 'image', mimeType: 'image/png', data: 'AA==' }], timestamp: Date.now() }] };
  const imageStream = config.streamSimple(model, imageContext, {});
  const imageResult = await imageStream.result();
  assert.equal(imageResult.stopReason, 'error');
  assert.match(imageResult.errorMessage, /text-only/);
  const stream = config.streamSimple(model, context, { env: { PI_OPENCODE_BIN: bin, BRIDGE_FIXTURE_MODE: 'hang' } });
  await new Promise(r => setTimeout(r, 250));
  await handlers.get('session_shutdown')();
  assert.equal((await stream.result()).stopReason, 'aborted');
  console.log('streaming provider contracts passed');
} finally { await rm(dir, { recursive: true, force: true }); }
