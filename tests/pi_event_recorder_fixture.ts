/** Offline event-order recorder, NOT the fleet extension or a real model.
 * Records which Pi hooks fire (and with what shape) for a CLI startup prompt and for
 * a later pi.sendMessage(custom, { triggerTurn: true }) run, the way pi_extension_v2.ts
 * starts a fleet delivery. Output feeds tests/fixtures/pi_events_<version>.json. */
import { appendFileSync } from 'node:fs';
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { Type } from 'typebox';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

export default function (pi: ExtensionAPI) {
  const record = process.env.FLEET_EVENT_RECORD;
  if (!record) throw new Error('Fixture record path is required');
  let phase = 'startup';
  const log = (hook: string, data: Record<string, unknown> = {}) =>
    appendFileSync(record, JSON.stringify({ hook, phase, ...data }) + '\n');
  pi.registerProvider('fleet-event-fixture', {
    baseUrl: 'http://fixture.invalid', api: 'fleet-event-fixture-api',
    apiKey: 'SYNTHETIC_FIXTURE_NOT_A_CREDENTIAL',
    models: [{ id: 'fixture', name: 'Offline event recorder', reasoning: false,
      input: ['text'], contextWindow: 32000, maxTokens: 1000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, context) {
      const raw = JSON.stringify(context);
      const delivery = raw.includes('DELIVERY_BODY_SENTINEL') && !raw.includes('fixture-tool-done');
      const stream = createAssistantMessageEventStream();
      queueMicrotask(() => {
        const message: any = {
          role: 'assistant', api: model.api, provider: model.provider, model: model.id,
          timestamp: Date.now(), stopReason: 'pending', content: [],
          usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        };
        stream.push({ type: 'start', partial: message });
        if (delivery) {
          message.content.push({ type: 'toolCall', id: 'fixture-call', name: 'fleet_event_tool', arguments: { reply: 'OK' } });
          stream.push({ type: 'toolcall_start', contentIndex: 0, partial: message });
          stream.push({ type: 'toolcall_end', contentIndex: 0, toolCall: message.content[0], partial: message });
          message.stopReason = 'toolUse';
          stream.push({ type: 'done', reason: 'toolUse', message });
        } else {
          message.content.push({ type: 'text', text: 'READY' });
          message.stopReason = 'stop';
          stream.push({ type: 'done', reason: 'stop', message });
        }
        stream.end();
      });
      return stream;
    },
  });
  pi.registerTool({
    name: 'fleet_event_tool', label: 'Recorder tool', description: 'Finish this offline fixture turn.',
    parameters: Type.Object({ reply: Type.String() }),
    async execute(_id, params, _signal, _update, ctx) {
      log('tool_execute', { idle: ctx.isIdle() });
      return { content: [{ type: 'text', text: 'fixture-tool-done' }], details: params, terminate: true };
    },
  });
  pi.on('session_start', async (_event, ctx) => log('session_start', { mode: ctx.mode, idle: ctx.isIdle() }));
  pi.on('input', async (event, ctx) => { log('input', { source: event.source, idle: ctx.isIdle() }); return { action: 'continue' }; });
  pi.on('before_agent_start', async (event: any, ctx) => log('before_agent_start', {
    idle: ctx.isIdle(), messageType: event.messageType ?? null, hasPrompt: typeof event.prompt === 'string',
    customType: event.message?.customType ?? null, detailsMessageId: event.message?.details?.message_id ?? null }));
  pi.on('agent_start', async (_event, ctx) => log('agent_start', { idle: ctx.isIdle() }));
  pi.on('tool_execution_start', (event) => log('tool_execution_start', { tool: event.toolName }));
  pi.on('tool_execution_end', (event) => log('tool_execution_end', { tool: event.toolName, isError: event.isError }));
  pi.on('message_end', async (event) => log('message_end', { role: event.message.role,
    customType: (event.message as any).customType ?? null, stopReason: (event.message as any).stopReason ?? null }));
  pi.on('agent_settled', async (event, ctx) => {
    log('agent_settled', { aborted: event.aborted });
    if (phase === 'startup') {
      phase = 'pre-delivery';
      // Same as the fleet bridge: a later Node turn, not inside the notification.
      setTimeout(() => {
        phase = 'delivery';
        log('delivery_frame', { idle: ctx.isIdle(), pending: ctx.hasPendingMessages() });
        pi.sendMessage({ customType: 'fleet-delivery', content: 'DELIVERY_BODY_SENTINEL: return OK', display: true,
          details: { message_id: 'm-recorded' } }, { triggerTurn: true });
      }, 300);
    } else if (phase === 'delivery') {
      phase = 'done';
      log('done');
    }
  });
}
