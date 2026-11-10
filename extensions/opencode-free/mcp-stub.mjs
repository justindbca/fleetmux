// Stub MCP server (stdio, newline-delimited JSON-RPC). It only lists Pi's tools so OpenCode offers
// them to the model as native tools; it never executes anything. The bridge captures each call
// from OpenCode's events and rejects its permission request, so tools/call is never reached.
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

const tools = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const send = message => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
createInterface({ input: process.stdin }).on('line', line => {
  let request;
  try { request = JSON.parse(line); } catch { return; }
  if (request.id === undefined) return;  // notifications
  if (request.method === 'initialize') {
    send({ id: request.id, result: { protocolVersion: request.params?.protocolVersion ?? '2025-06-18',
      capabilities: { tools: {} }, serverInfo: { name: 'pi', version: '1' } } });
  } else if (request.method === 'tools/list') {
    send({ id: request.id, result: { tools: tools.map(tool => ({ name: tool.name,
      description: tool.description ?? '', inputSchema: tool.parameters ?? { type: 'object' } })) } });
  } else if (request.method === 'tools/call') {
    send({ id: request.id, result: { isError: true, content: [{ type: 'text', text: 'Pi runs this tool, not OpenCode.' }] } });
  } else {
    send({ id: request.id, error: { code: -32601, message: 'Method not found' } });
  }
});
