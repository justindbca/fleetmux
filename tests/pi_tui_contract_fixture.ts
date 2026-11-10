/** Offline capability fixture, NOT the fleet extension or a real model. */
import { writeFileSync } from 'node:fs';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { Text } from '@earendil-works/pi-tui';
import { Type } from 'typebox';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

export default function (pi: ExtensionAPI) {
  const record = process.env.FLEET_CONTRACT_RECORD;
  if (!record) throw new Error('Fixture record path is required');
  let requests = 0;
  let started = false;
  let mode = '';
  let bodySeen = false;
  let transportSeen = false;
  let uiSeen = false;
  const boundaries: string[] = [];
  const save = (phase: string) => writeFileSync(record, JSON.stringify({
    phase, mode, requests, bodySeen, transportSeen, uiSeen, boundaries,
  }));
  pi.registerProvider('fleet-tui-fixture', {
    baseUrl: 'http://fixture.invalid', api: 'fleet-tui-fixture-api',
    apiKey: 'SYNTHETIC_FIXTURE_NOT_A_CREDENTIAL',
    models: [{ id: 'fixture', name: 'Offline deterministic TUI fixture', reasoning: false,
      input: ['text'], contextWindow: 32000, maxTokens: 1000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, context) {
      requests += 1;
      const raw = JSON.stringify(context);
      bodySeen = raw.includes('BUSINESS_BODY_SENTINEL');
      transportSeen = raw.includes('TRANSPORT_ONLY_SENTINEL');
      uiSeen = raw.includes('UI_ONLY_TIMEOUT_SENTINEL');
      const stream = createAssistantMessageEventStream();
      queueMicrotask(() => {
        const message: any = {
          role: 'assistant', api: model.api, provider: model.provider, model: model.id,
          timestamp: Date.now(), stopReason: 'pending', content: [],
          usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        };
        stream.push({ type: 'start', partial: message });
        message.content.push({ type: 'toolCall', id: `fixture-${requests}`,
          name: 'fleet_contract_result', arguments: { reply: 'OK', internal: 'PROTOCOL_ARGUMENT_SENTINEL' } });
        stream.push({ type: 'toolcall_start', contentIndex: 0, partial: message });
        stream.push({ type: 'toolcall_end', contentIndex: 0, toolCall: message.content[0], partial: message });
        message.stopReason = 'toolUse';
        stream.push({ type: 'done', reason: 'toolUse', message });
        stream.end();
      });
      return stream;
    },
  });
  pi.registerEntryRenderer('fleet-tui-fixture-ui', (entry) => new Text((entry.data as any).text, 0, 0));
  pi.registerTool({
    name: 'fleet_contract_result', label: 'Contract result', description: 'Finish this offline fixture turn.',
    parameters: Type.Object({ reply: Type.String(), internal: Type.String() }),
    async execute(_id, params) {
      boundaries.push('candidate');
      return { content: [{ type: 'text', text: 'Candidate awaiting validation' }], details: params, terminate: true };
    },
    renderCall() { return new Text('结果已提交，等待验收', 0, 0); },
    renderResult() { return new Text('结果已提交，等待验收', 0, 0); },
  });
  pi.on('session_start', async (_event, ctx) => {
    mode = ctx.mode;
    pi.appendEntry('fleet-tui-fixture-ui', {
      text: 'worker-1 处理超时（约 10 分钟），未取得有效结果。原执行尚未确认停止。详情见 control。',
      event_id: 'UI_ONLY_TIMEOUT_SENTINEL',
    });
    save('ready');
  });
  pi.on('input', async (event, ctx) => {
    if (ctx.mode !== 'tui' || event.source !== 'interactive' || event.text.startsWith('/')) {
      return { action: 'continue' };
    }
    if (started) return { action: 'handled' };
    started = true;
    pi.appendEntry('fleet-tui-fixture-ui', { text: event.text });
    pi.sendMessage({ customType: 'fleet-tui-fixture-task', display: false,
      content: 'BUSINESS_BODY_SENTINEL: return OK',
      details: { delivery_id: 'TRANSPORT_ONLY_SENTINEL' },
    }, { triggerTurn: true });
    return { action: 'handled' };
  });
  pi.on('agent_settled', () => {
    boundaries.push('settled');
    pi.appendEntry('fleet-tui-fixture-ui', { text: 'worker-1：OK' });
    save('settled');
  });
}
