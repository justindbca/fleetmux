/** Offline provider for acceptance_pi_v2_delivery.py, NOT a real model.
 * Loaded next to the real cmux_fleet/pi_extension_v2.ts: startup replies READY,
 * the fleet delivery run calls fleet_result once. */
import { createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';

export default function (pi: ExtensionAPI) {
  pi.registerProvider('fleet-v2-fixture', {
    baseUrl: 'http://fixture.invalid', api: 'fleet-v2-fixture-api',
    apiKey: 'SYNTHETIC_FIXTURE_NOT_A_CREDENTIAL',
    models: [{ id: 'fixture', name: 'Offline fleet v2 fixture', reasoning: false,
      input: ['text'], contextWindow: 32000, maxTokens: 1000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, context) {
      const raw = JSON.stringify(context);
      const delivery = raw.includes('DELIVERY_BODY_SENTINEL') && !raw.includes('fixture-call');
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
          message.content.push({ type: 'toolCall', id: 'fixture-call', name: 'fleet_result', arguments: { result: { reply: 'OK' } } });
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
}
