// Contract test using the actual TypeScript extension, Node's type stripping and a fake Pi API.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const { default: register } = await import(pathToFileURL(process.argv[2]).href);
const events = new Map();
register({ on(name, handler) { events.set(name, handler); } });
const titles = [];
const notices = [];
let label = 'supervisor-1.1';
const ctx = {
  mode: 'tui',
  cwd: '/tmp',
  ui: { setTitle: (title) => titles.push(title), notify: (text, level) => notices.push({text, level}) },
  getContextUsage: () => ({ tokens: 4321, contextWindow: 100000 }),
  model: { contextWindow: 100000 },
  sessionManager: { getSessionId: () => 'session-id', getSessionFile: () => '/sessions/g0/session.jsonl',
                    getSessionName: () => label, getBranch: () => [] },
};
const records = () => readFileSync(process.argv[3], 'utf8').trim().split('\n').map(JSON.parse);
const emit = async (name, payload) => await events.get(name)?.(payload, ctx);
const assistant = (text, stopReason = 'stop') => ({ message: {
  role: 'assistant', stopReason, content: [{ type: 'text', text }],
} });

assert.ok(!events.has('agent_end') && !events.has('turn_end'));
await emit('session_start', {});
assert.equal(records()[0].kind, 'pi_session');
assert.equal(titles.at(-1), 'supervisor-1.1');
assert.equal((await emit('input', {text:'Initial CLI prompt',source:'interactive'})).action, 'continue');
await emit('before_agent_start', { prompt: 'Do the task -- the full message is in /tmp/.inbox/msg-20261010-090000-abcdef.md . Read that whole file first and follow it.' });
await emit('message_end', assistant('temporary error', 'error'));
await emit('agent_end', {}); // retry is still possible; no terminal callback
assert.equal(records().length, 2);
await emit('message_end', assistant('intermediate tool call', 'toolUse'));
await emit('message_end', { message: { role: 'toolResult', content: [] } });
await emit('message_end', assistant('{"answer":2}'));
await emit('agent_settled', { aborted: false });
let last = records().at(-1);
assert.equal(last.kind, 'turn');
assert.equal(last.text, '{"answer":2}');
assert.equal(last.msg_id, 'msg-20261010-090000-abcdef');
assert.equal(last.failed, false);
assert.equal(last.ctx_used, 4321);
assert.equal(titles.at(-1), 'supervisor-1.1');
await emit('agent_settled', { aborted: false });
assert.equal(records().length, 3); // duplicate boundary suppressed

await emit('before_agent_start', { prompt: 'Manual input without delivery id' });
await emit('message_end', assistant('partial result'));
await emit('agent_settled', { aborted: true });
last = records().at(-1);
assert.equal(last.msg_id, ''); // never reuse the previous id
assert.equal(last.aborted, true);

await emit('before_agent_start', { prompt: 'New generation startup' });
await emit('agent_settled', { aborted: false });
last = records().at(-1);
assert.equal(last.text, ''); // no old assistant text reused
assert.equal(last.failed, true);

ctx.getContextUsage = () => ({ tokens: null, contextWindow: 100000 });
await emit('before_agent_start', { prompt: 'one more' });
await emit('message_end', assistant('backend error', 'error'));
await emit('agent_settled', { aborted: false });
last = records().at(-1);
assert.equal(last.failed, true);
assert.equal(last.ctx_used, null);
assert.ok(titles.every((title) => title === 'supervisor-1.1'));
label = 'worker-1.2';
await emit('session_start', {});
assert.equal(titles.at(-1), 'worker-1.2');
// Regression: a supervisor may quote its own inbox in the instruction preview.
// The worker must bind to the final daemon pointer, not the first inbox mentioned.
const quoted = '/supervisor/.inbox/msg-20261010-090000-111111.md';
await emit('before_agent_start', { prompt: `Read ${quoted} -- the full message is in /tmp/.inbox/msg-20261010-090100-222222.md . Read that whole file first and follow it.` });
await emit('message_end', assistant('{"sum":42}'));
await emit('agent_settled', { aborted: false });
assert.equal(records().at(-1).msg_id, 'msg-20261010-090100-222222');
// A pointer belonging to another agent must never acknowledge this agent's delivery.
await emit('before_agent_start', { prompt: `Read it -- the full message is in ${quoted} . Read that whole file first and follow it.` });
await emit('message_end', assistant('done'));
await emit('agent_settled', { aborted: false });
assert.equal(records().at(-1).msg_id, '');
const titleCount = titles.length;
ctx.mode = 'json';
await emit('session_start', {});
assert.equal(titles.length, titleCount); // no terminal side effects in headless mode
// Human input must enter the daemon queue, but daemon pointers and slash commands must not loop.
ctx.mode = 'tui';
label = 'supervisor-1.1';
await emit('before_agent_start', {prompt:'Startup completed'});
const beforeInput = records().length;
assert.equal((await emit('input', {text:'Ask worker-1.1 to reply OK',source:'interactive'})).action, 'handled');
assert.equal(records().length, beforeInput + 1);
assert.equal(records().at(-1).kind, 'operator_input');
assert.equal(records().at(-1).text, 'Ask worker-1.1 to reply OK');
assert.equal(notices.at(-1).level, 'info');
assert.ok(notices.at(-1).text.includes('ui-test'));
const beforeDaemon = records().length;
const daemonPrompt = 'Task -- the full message is in /tmp/.inbox/msg-20261010-090100-222222.md . Read that whole file first and follow it.';
for (const event of [{text:daemonPrompt,source:'interactive'}, {text:'/model',source:'interactive'}, {text:'RPC input',source:'rpc'}]) {
  assert.equal((await emit('input', event)).action, 'continue');
}
assert.equal(records().length, beforeDaemon);
assert.equal((await emit('input', {text:'Image',source:'interactive',images:[{}]})).action, 'handled');
assert.equal(notices.at(-1).level, 'warning');
assert.equal(records().length, beforeDaemon);
// Workers remain ordinary interactive Pi sessions; only the supervisor bridges operator commands.
const workerEvents = new Map();
const workerCallback = JSON.parse(process.env.CMUX_FLEET_PI_CALLBACK);
workerCallback[workerCallback.length - 2] = 'worker-1';
process.env.CMUX_FLEET_PI_CALLBACK = JSON.stringify(workerCallback);
register({ on(name, handler) { workerEvents.set(name, handler); } });
await workerEvents.get('before_agent_start')({prompt:'Worker startup'}, ctx);
assert.equal((await workerEvents.get('input')({text:'Reply OK',source:'interactive'}, ctx)).action, 'continue');
console.log('Pi extension lifecycle contract passed');
