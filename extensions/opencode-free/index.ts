import { randomUUID } from 'node:crypto';
import {
  collapseSystemMessages, createAssistantMessageEventStream, getCurrentSystemPrompt, getCurrentTools,
  type AssistantMessage, type Model, type Api, type TranscriptContext, type SimpleStreamOptions,
} from '@earendil-works/pi-ai';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { MODEL_ID, PROVIDER_ID, encodePrompt, estimateTokens, streamOpenCode, stopOpenCode } from './backend.mjs';

// OpenCode's limit for this model: 200k context with 32k kept for output, i.e. a 168k input budget.
// Declared to Pi as the window: Pi compacts on its own at contextWindow - compaction.reserveTokens
// (default 16384), i.e. ~151.6k, leaving the reserve for the next round's growth (tool results,
// the new message) before the bridge's precheck would reject at 168k.
const INPUT_BUDGET = 200000 - 32000;

export function makeStream(active: Map<AbortController, Promise<void>>) {
  return (model: Model<Api>, context: TranscriptContext, options: SimpleStreamOptions = {}) => {
    const stream = createAssistantMessageEventStream();
    const controller = new AbortController();
    let settled!: () => void;
    active.set(controller, new Promise<void>(resolve => { settled = resolve; }));
    const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
    const output: AssistantMessage = {
      role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
      timestamp: Date.now(), stopReason: 'pending',
      usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    };
    let started = false, textIndex = -1, thinkingIndex = -1, closed = false;
    const start = () => { if (!started) { started = true; stream.push({ type: 'start', partial: output }); } };
    const textDelta = (delta: string) => {
      start();
      if (textIndex < 0) {
        textIndex = output.content.length;
        output.content.push({ type: 'text', text: '' });
        stream.push({ type: 'text_start', contentIndex: textIndex, partial: output });
      }
      const block = output.content[textIndex];
      if (block.type !== 'text') throw new Error('Invalid text block.');
      block.text += delta;
      if (delta) stream.push({ type: 'text_delta', contentIndex: textIndex, delta, partial: output });
    };
    const closeBlocks = () => {
      if (closed) return;
      closed = true;
      if (thinkingIndex >= 0) {
        const block = output.content[thinkingIndex];
        if (block.type === 'thinking') stream.push({ type: 'thinking_end', contentIndex: thinkingIndex, content: block.thinking, partial: output });
      }
      if (textIndex >= 0) {
        const block = output.content[textIndex];
        if (block.type === 'text') stream.push({ type: 'text_end', contentIndex: textIndex, content: block.text, partial: output });
      }
    };
    void (async () => {
      try {
        if (model.id !== MODEL_ID || model.provider !== PROVIDER_ID) throw new Error('Free bridge model mismatch.');
        signal.throwIfAborted();
        const transcript = collapseSystemMessages(context);
        const tools = options.toolChoice === 'none' ? [] : getCurrentTools(transcript.messages);
        const messages = transcript.messages.filter(message => message.role !== 'system').map(message => {
          if (typeof message.content === 'string') return { role: message.role, content: message.content };
          const content = message.content.flatMap(block => {
            if (block.type === 'image') throw new Error('Free bridge is text-only. Supply a text file instead of an image.');
            if (block.type === 'text') return [{ type: 'text', text: block.text }];
            if (block.type === 'toolCall') return [{ type: 'toolCall', id: block.id, name: block.name, arguments: block.arguments }];
            // Opaque thinking signatures and provider metadata are not instructions.
            return [];
          });
          return message.role === 'toolResult'
            ? { role: message.role, content, toolCallId: message.toolCallId, toolName: message.toolName, isError: message.isError }
            : { role: message.role, content };
        });
        const prompt = encodePrompt(getCurrentSystemPrompt(transcript.messages), messages);
        // Pi's tools reach the model as native tools (stub MCP server), so only these fields go out.
        const toolSpecs = tools.map(tool => ({ name: tool.name, description: tool.description, parameters: tool.parameters }));
        if (estimateTokens(prompt + JSON.stringify(toolSpecs)) > INPUT_BUDGET) {
          throw new Error('context_length_exceeded: bridge transcript estimate exceeds input budget; compact the Pi session.');
        }
        const result = await streamOpenCode(prompt, { tools: toolSpecs,
          signal, env: options.env, bin: options.env?.PI_OPENCODE_BIN ?? process.env.PI_OPENCODE_BIN,
          timeoutMs: options.timeoutMs, fetch: options.fetch,
          onPayload: options.onPayload ? (payload: unknown) => options.onPayload!(payload, model) : undefined,
          onResponse: options.onResponse ? (response: any) => options.onResponse!(response, model) : undefined,
          onProviderStreamEvent: options.onProviderStreamEvent ? (event: unknown) => options.onProviderStreamEvent!(event, model) : undefined,
          onText: (delta: string) => textDelta(delta),
          onThinking: (delta: string) => {
            start();
            if (thinkingIndex < 0) {
              thinkingIndex = output.content.length;
              output.content.push({ type: 'thinking', thinking: '' });
              stream.push({ type: 'thinking_start', contentIndex: thinkingIndex, partial: output });
            }
            const block = output.content[thinkingIndex];
            if (block.type !== 'thinking') throw new Error('Invalid thinking block.');
            block.thinking += delta;
            stream.push({ type: 'thinking_delta', contentIndex: thinkingIndex, delta, partial: output });
          },
        });
        signal.throwIfAborted();
        // OpenCode parsed the arguments; still check them at Pi's boundary before anything runs.
        const declared = new Set(tools.map(tool => tool.name));
        const object = (x: unknown) => x !== null && typeof x === 'object' && !Array.isArray(x);
        if (result.toolCalls.length > 32 || result.toolCalls.some((call: any) => !declared.has(call.name) || !object(call.arguments))) {
          throw new Error('OpenCode returned an undeclared tool or invalid arguments; no tools executed.');
        }
        const tokens = result.usage;
        const count = (n: unknown) => {
          if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) throw new Error('Invalid OpenCode usage.');
          return n;
        };
        output.usage.input = count(tokens.input);
        output.usage.reasoning = count(tokens.reasoning ?? 0);
        output.usage.output = count(tokens.output) + output.usage.reasoning;
        output.usage.cacheRead = count(tokens.cache?.read ?? 0);
        output.usage.cacheWrite = count(tokens.cache?.write ?? 0);
        output.usage.totalTokens = output.usage.input + output.usage.output + output.usage.cacheRead + output.usage.cacheWrite;
        start();
        if (textIndex < 0 && result.toolCalls.length === 0) textDelta('');
        closeBlocks();
        for (const call of result.toolCalls) {
          const index = output.content.length;
          const block = { type: 'toolCall' as const, id: randomUUID(), name: call.name, arguments: {} };
          output.content.push(block);
          stream.push({ type: 'toolcall_start', contentIndex: index, partial: output });
          block.arguments = call.arguments;
          stream.push({ type: 'toolcall_delta', contentIndex: index, delta: JSON.stringify(call.arguments), partial: output });
          stream.push({ type: 'toolcall_end', contentIndex: index, toolCall: block, partial: output });
        }
        output.stopReason = result.toolCalls.length ? 'toolUse' : 'stop';
        stream.push({ type: 'done', reason: output.stopReason, message: output });
      } catch (error) {
        closeBlocks();
        output.stopReason = signal.aborted ? 'aborted' : 'error';
        output.errorMessage = signal.aborted ? 'Request aborted; private OpenCode server stopped.' :
          error instanceof Error ? error.message : 'OpenCode bridge failed.';
        stream.push({ type: 'error', reason: output.stopReason, error: output });
      } finally {
        active.delete(controller);
        stream.end();
        settled();
      }
    })();
    return stream;
  };
}

export default function (pi: ExtensionAPI) {
  const active = new Map<AbortController, Promise<void>>();
  pi.registerProvider(PROVIDER_ID, {
    api: 'opencode-session-sse', baseUrl: 'http://127.0.0.1', apiKey: 'official-cli-local-session',
    models: [{ id: MODEL_ID, name: 'DeepSeek V4.1 Flash Free (OpenCode CLI · streaming)',
      reasoning: false, input: ['text'], contextWindow: INPUT_BUDGET, maxTokens: 32000,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple: makeStream(active),
  });
  pi.on('session_start', async (_event, ctx) => {
    if (ctx.hasUI) ctx.ui.setStatus('opencode-free', 'OpenCode official CLI · DeepSeek Free · live SSE');
  });
  pi.on('session_shutdown', async () => {
    const pending = [...active.values()];
    for (const controller of active.keys()) controller.abort();
    await Promise.allSettled(pending);
    await stopOpenCode();  // the shared OpenCode server lives as long as this Pi session
  });
}
