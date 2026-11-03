import { defineFunction } from '@aws-amplify/backend';

/** Reads a Celonis tenant over MCP and turns process-mining data into a
 *  DiagramIQ discovery definition.
 *
 *  Long by design: one call is an OAuth handshake, an MCP handshake, and then
 *  up to a dozen rounds of (model thinks → Celonis answers). 300s is the
 *  Function URL's budget; the HTTP API in front still cuts off at 29s, which
 *  is why the frontend prefers the Function URL for this route.
 */
export const celonisApi = defineFunction({
  name: 'diagramiq-celonis',
  entry: './handler.ts',
  timeoutSeconds: 300,
  memoryMB: 1024,
  environment: {
    MODEL_ID: 'us.anthropic.claude-opus-4-5-20251101-v1:0',
  },
});
