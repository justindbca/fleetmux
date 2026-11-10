// Opt-in M0 probe against the INSTALLED Pi SDK, not a mocked ExtensionAPI.
// node tests/acceptance_pi_sdk_contract.mjs /absolute/path/to/pi-coding-agent
// Uses an in-memory deterministic provider fixture. No model service, auth files,
// production Redis, personal resources, or cmux windows are accessed.
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const root = resolve(process.argv[2]);
const requirePi = createRequire(join(root, 'package.json'));
const sdk = await import(pathToFileURL(join(root, 'dist/index.js')).href);
const ai = await import(pathToFileURL(join(root, 'node_modules/@earendil-works/pi-ai/dist/index.js')).href);
const { Type } = await import(pathToFileURL(requirePi.resolve('typebox')).href);
const { Text, visibleWidth } = await import(pathToFileURL(requirePi.resolve('@earendil-works/pi-tui')).href);
const temp = await mkdtemp(join(tmpdir(), 'fleet-sdk-contract-'));
const home = join(temp, 'agent');
await mkdir(home);
let session;
let extensionApi;
let requests = 0;
let executions = 0;
const requestContexts = [];
const boundaries = [];
let settledResolve;
const errors = [];
const cost = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

try {
  const modelRuntime = await sdk.ModelRuntime.create({
    authPath: join(home, 'auth.json'), modelsPath: join(home, 'models.json'),
    modelsStorePath: join(home, 'model-store.json'),
    refreshOnCreate: false, allowModelNetwork: false,
  });
  modelRuntime.registerProvider('fleet-contract-fixture', {
    baseUrl: 'http://fixture.invalid', apiKey: 'SYNTHETIC_FIXTURE_NOT_A_CREDENTIAL',
    api: 'fleet-contract-api',
    models: [{ id: 'fixture', name: 'Deterministic fixture, not a real model',
      reasoning: false, input: ['text'], cost, contextWindow: 32000, maxTokens: 1000 }],
    streamSimple(model, context) {
      requests += 1;
      requestContexts.push(JSON.parse(JSON.stringify(context)));
      const stream = ai.createAssistantMessageEventStream();
      queueMicrotask(() => {
        const message = {
          role: 'assistant', content: [], api: model.api, provider: model.provider,
          model: model.id, timestamp: Date.now(), stopReason: 'pending',
          usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
            cost: { ...cost, total: 0 } },
        };
        stream.push({ type: 'start', partial: message });
        message.content.push({ type: 'toolCall', id: `fixture-call-${requests}`,
          name: 'fleet_contract_result', arguments: { reply: '业务 JSON: {"answer":42}; /tmp/report.md' } });
        stream.push({ type: 'toolcall_start', contentIndex: 0, partial: message });
        stream.push({ type: 'toolcall_end', contentIndex: 0,
          toolCall: message.content[0], partial: message });
        message.stopReason = 'toolUse';
        stream.push({ type: 'done', reason: 'toolUse', message });
        stream.end();
      });
      return stream;
    },
  });
  const settingsManager = sdk.SettingsManager.inMemory({
    compaction: { enabled: false }, retry: { enabled: false }, cacheWarming: 'off',
    defaultTools: [], enableInstallTelemetry: false, enableAnalytics: false,
  });
  const resourceLoader = new sdk.DefaultResourceLoader({
    cwd: temp, agentDir: home, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
    noContextFiles: true, systemPrompt: 'This is an offline deterministic contract test.',
    extensionFactories: [(pi) => {
      extensionApi = pi;
      pi.registerEntryRenderer('fleet-contract-ui', (entry) => new Text(entry.data.text, 0, 0));
      pi.registerTool({
        name: 'fleet_contract_result', label: 'Contract result',
        description: 'Finish this fixture turn without a follow-up model request.',
        parameters: Type.Object({ reply: Type.String() }),
        async execute(_id, args) {
          executions += 1;
          boundaries.push('candidate');
          return { content: [{ type: 'text', text: 'Candidate awaiting validation' }],
            details: args, terminate: true };
        },
        renderCall() { return new Text('已提交结果，等待验收', 0, 0); },
        renderResult() { return new Text('已提交结果，等待验收', 0, 0); },
      });
      pi.on('agent_settled', () => {
        boundaries.push('settled');
        settledResolve?.();
      });
    }],
  });
  await resourceLoader.reload();
  assert.deepEqual(resourceLoader.getExtensions().errors, []);
  ({ session } = await sdk.createAgentSession({
    cwd: temp, agentDir: home, resourceLoader, modelRuntime,
    model: modelRuntime.getModel('fleet-contract-fixture', 'fixture'), thinkingLevel: 'off',
    settingsManager, sessionManager: sdk.SessionManager.inMemory(temp),
    tools: ['fleet_contract_result'],
  }));
  await session.bindExtensions({ mode: 'json', onError: (error) => errors.push(error) });
  assert.ok(session.getActiveToolNames().includes('fleet_contract_result'));

  const uiText = 'worker-1 处理超时（约 10 分钟），未取得有效结果。原执行尚未确认停止。详情见 control。';
  extensionApi.appendEntry('fleet-contract-ui', { event_id: 'ui-synthetic-1', text: uiText });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(requests, 0, 'UI-only entry must not invoke the provider');
  assert.equal(session.sessionManager.getBranch().filter((e) => e.type === 'custom').length, 1);
  assert.ok(!JSON.stringify(session.sessionManager.buildSessionContext()).includes(uiText));

  const settled = new Promise((resolve) => { settledResolve = resolve; });
  extensionApi.sendMessage({
    customType: 'fleet-contract-task', content: 'BUSINESS_BODY_SENTINEL', display: false,
    details: { delivery_id: 'TRANSPORT_ONLY_SENTINEL', fence: 17 },
  }, { triggerTurn: true });
  await Promise.race([settled, new Promise((_, reject) => {
    const timer = setTimeout(() => reject(new Error('Pi failed to settle the fixture turn')), 10000);
    timer.unref();
  })]);
  await session.waitForIdle();
  assert.equal(requests, 1, 'terminate:true must suppress the mechanical follow-up request');
  assert.equal(executions, 1);
  assert.deepEqual(boundaries, ['candidate', 'settled']);
  const providerContext = JSON.stringify(requestContexts[0]);
  assert.ok(providerContext.includes('BUSINESS_BODY_SENTINEL'));
  assert.ok(!providerContext.includes('TRANSPORT_ONLY_SENTINEL'));
  assert.ok(!providerContext.includes(uiText));
  const entry = session.sessionManager.getBranch().find((e) => e.type === 'custom_message');
  assert.equal(entry.display, false);
  assert.equal(entry.details.delivery_id, 'TRANSPORT_ONLY_SENTINEL');
  assert.ok(!session.getLastAssistantText(), 'structured tool completion need not have assistant text');
  extensionApi.appendEntry('fleet-contract-ui', { event_id: 'ui-synthetic-2', text: 'worker-1：OK' });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.equal(requests, 1, 'result cards must not wake the provider');
  assert.deepEqual(errors, []);
  const lines = new Text(uiText, 0, 0).render(24);
  assert.ok(lines.every((line) => visibleWidth(line) <= 24));
  console.log(JSON.stringify({
    sdk: root, provider: 'in-memory deterministic fixture (not a real model)',
    passed: ['UI-only excluded from context', 'UI-only zero provider calls',
      'hidden task body included', 'transport details excluded',
      'terminate suppresses follow-up', 'candidate precedes settled',
      'empty assistant text after structured completion supported', 'Chinese narrow rendering'],
    provider_calls: requests, tool_executions: executions,
    real_tui_test: false, real_model_test: false,
  }, null, 2));
} finally {
  session?.dispose();
  await rm(temp, { recursive: true, force: true });
}
