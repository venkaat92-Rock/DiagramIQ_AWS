/**
 * A fake Celonis tenant, a fake Bedrock, and a fake DynamoDB.
 *
 * The point of this file is that the Celonis feature can be exercised
 * end-to-end with no Celonis tenant, no Bedrock access and no AWS account: the
 * handler under test is the real one, the MCP client is the real one, and what
 * is faked is only what sits on the far side of the network.
 *
 * The AWS SDK's service-specific endpoint environment variables
 * (AWS_ENDPOINT_URL_BEDROCK_RUNTIME, AWS_ENDPOINT_URL_DYNAMODB) point the real
 * clients at these servers, so no client is monkey-patched and no production
 * code knows it is under test.
 */
import { createServer } from 'node:http';
import { createServer as createHttp2Server } from 'node:http2';
import { AddressInfo } from 'node:net';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import {
  CallToolRequestSchema, ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';

/* ---------- the data the fake tenant has mined ----------------------------- */

export const ACTIVITIES = [
  { activity: 'Create Invoice', cases: 12480, resource: 'SAP interface' },
  { activity: 'Scan Invoice', cases: 12480, resource: 'AP Clerk' },
  { activity: 'Match to PO', cases: 11920, resource: 'AP Clerk' },
  { activity: 'Clarify with Supplier', cases: 2140, resource: 'AP Clerk' },
  { activity: 'Approve Invoice', cases: 11890, resource: 'Cost Centre Owner' },
  { activity: 'Post Invoice', cases: 11870, resource: 'SAP interface' },
  { activity: 'Pay Invoice', cases: 11840, resource: 'Treasury' },
];

export const DIRECTLY_FOLLOWS = [
  ['Create Invoice', 'Scan Invoice', 12480],
  ['Scan Invoice', 'Match to PO', 11920],
  ['Match to PO', 'Approve Invoice', 9780],
  ['Match to PO', 'Clarify with Supplier', 2140],
  ['Clarify with Supplier', 'Match to PO', 2110],
  ['Approve Invoice', 'Post Invoice', 11870],
  ['Post Invoice', 'Pay Invoice', 11840],
];

const TOOLS = [
  {
    name: 'list_data_models',
    description: 'List the data models published to this team.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'get_activities',
    description: 'Activity names with case counts and the resource that performs them.',
    inputSchema: {
      type: 'object',
      properties: { dataModel: { type: 'string' } },
      required: ['dataModel'],
    },
  },
  {
    name: 'get_directly_follows',
    description: 'Directly-follows relations between activities, with counts.',
    inputSchema: {
      type: 'object',
      properties: { dataModel: { type: 'string' } },
      required: ['dataModel'],
    },
  },
  // Published by the tenant, and never offered to the model: DiagramIQ reads a
  // process, it does not run one.
  {
    name: 'trigger_action_flow',
    description: 'Start an action-flow automation.',
    inputSchema: { type: 'object', properties: { flowId: { type: 'string' } } },
  },
] as const;

export const BLOCKED_TOOL = 'trigger_action_flow';
const DATA_MODEL = 'AP_Invoices_EU';

function runTool(name: string, args: Record<string, unknown>): string {
  if (name === 'list_data_models') {
    return JSON.stringify({ dataModels: [DATA_MODEL, 'O2C_Orders_NA'] });
  }
  if (name === 'get_activities' || name === 'get_directly_follows') {
    if (args.dataModel !== DATA_MODEL) {
      // The real thing errors on an unknown object too, and the model is meant
      // to read the message and try the name it was given by list_data_models.
      throw new Error(`No data model named '${String(args.dataModel)}'. `
                      + 'Call list_data_models first.');
    }
    return name === 'get_activities'
      ? JSON.stringify({ activities: ACTIVITIES })
      : JSON.stringify({
          directlyFollows: DIRECTLY_FOLLOWS.map(([from, to, count]) => ({ from, to, count })),
        });
  }
  throw new Error(`Unknown tool '${name}'.`);
}

/* ---------- the fake tenant: OAuth + MCP ----------------------------------- */

export type Tenant = {
  teamUrl: string;
  mcpUrl: string;
  tokenRequests: { auth: string; form: Record<string, string> }[];
  close: () => Promise<void>;
};

/** `rejectPost` makes the token endpoint refuse client_secret_post, which is
 *  how a tenant configured for client_secret_basic behaves — the fallback in
 *  getToken() exists for exactly this and is otherwise never exercised. */
export async function startTenant(opts: { rejectPost?: boolean } = {}): Promise<Tenant> {
  const tokenRequests: Tenant['tokenRequests'] = [];

  const server = createServer(async (req, res) => {
    const url = req.url ?? '/';
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const raw = Buffer.concat(chunks).toString();

    if (url.startsWith('/oauth2/token')) {
      const form = Object.fromEntries(new URLSearchParams(raw));
      const auth = String(req.headers.authorization ?? '');
      tokenRequests.push({ auth, form });
      const posted = !!form.client_secret;
      if (opts.rejectPost && posted) {
        res.writeHead(401, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid_client' }));
        return;
      }
      const ok = posted
        ? form.client_id === 'cid' && form.client_secret === 'shh'
        : auth === `Basic ${Buffer.from('cid:shh').toString('base64')}`;
      if (!ok) {
        res.writeHead(401, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'invalid_client' }));
        return;
      }
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ access_token: 'tenant-token', expires_in: 3600 }));
      return;
    }

    if (url.startsWith('/mcp')) {
      if (req.headers.authorization !== 'Bearer tenant-token') {
        res.writeHead(401, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ error: 'unauthorized' }));
        return;
      }
      // Stateless: a fresh server and transport per request, which is what the
      // MCP SDK documents for an HTTP server with no session store.
      const mcp = new Server({ name: 'celonis-stub', version: '0.0.1' },
                             { capabilities: { tools: {} } });
      mcp.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS as never }));
      mcp.setRequestHandler(CallToolRequestSchema, async (r) => {
        try {
          return { content: [{ type: 'text', text: runTool(r.params.name,
                                                           r.params.arguments ?? {}) }] };
        } catch (err) {
          return {
            isError: true,
            content: [{ type: 'text', text: err instanceof Error ? err.message : String(err) }],
          };
        }
      });
      const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
      res.on('close', () => { transport.close(); mcp.close(); });
      await mcp.connect(transport);
      await transport.handleRequest(req, res, raw ? JSON.parse(raw) : undefined);
      return;
    }

    res.writeHead(404);
    res.end();
  });

  const port = await listen(server);
  return {
    teamUrl: `http://127.0.0.1:${port}`,
    mcpUrl: `http://127.0.0.1:${port}/mcp`,
    tokenRequests,
    close: () => close(server),
  };
}

/* ---------- a scripted Bedrock ---------------------------------------------- */

export type Scripted =
  | { toolUse: { name: string; input: Record<string, unknown> } }
  | { text: string };

export type FakeBedrock = {
  url: string;
  calls: { modelId: string; body: any }[];
  close: () => Promise<void>;
};

/** Answers Converse from a queue. The last entry repeats, so a script can end
 *  with a tool call and let the loop run out of turns.
 *
 *  HTTP/2, in the clear: the Bedrock Runtime client selects an http2 handler
 *  because Converse has a streaming sibling, and an HTTP/1.1 stub makes it
 *  fail with a bare "Protocol error" before the request is ever sent. */
export async function startBedrock(script: Scripted[]): Promise<FakeBedrock> {
  const calls: FakeBedrock['calls'] = [];
  let i = 0;

  const server = createHttp2Server(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    const body = JSON.parse(Buffer.concat(chunks).toString() || '{}');
    const modelId = decodeURIComponent((req.url ?? '').split('/model/')[1]?.split('/')[0] ?? '');
    calls.push({ modelId, body });

    const step = script[Math.min(i, script.length - 1)];
    i += 1;
    const content = 'toolUse' in step
      ? [{ toolUse: { toolUseId: `tu-${i}`, name: step.toolUse.name, input: step.toolUse.input } }]
      : [{ text: step.text }];
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({
      output: { message: { role: 'assistant', content } },
      stopReason: 'toolUse' in step ? 'tool_use' : 'end_turn',
      usage: { inputTokens: 1, outputTokens: 1, totalTokens: 2 },
    }));
  });

  const port = await listen(server);
  return { url: `http://127.0.0.1:${port}`, calls, close: () => close(server) };
}

/* ---------- a DynamoDB that only remembers what it was asked to write ------- */

export type FakeDdb = {
  url: string;
  writes: any[];
  close: () => Promise<void>;
};

export async function startDdb(): Promise<FakeDdb> {
  const writes: any[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const c of req) chunks.push(c as Buffer);
    writes.push(JSON.parse(Buffer.concat(chunks).toString() || '{}'));
    res.writeHead(200, { 'content-type': 'application/x-amz-json-1.0' });
    res.end('{}');
  });
  const port = await listen(server);
  return { url: `http://127.0.0.1:${port}`, writes, close: () => close(server) };
}

/* ---------- plumbing -------------------------------------------------------- */

type Listenable = {
  listen: (port: number, host: string, cb: () => void) => unknown;
  address: () => AddressInfo | string | null;
  close: (cb: () => void) => unknown;
};

const listen = (s: Listenable) => new Promise<number>((resolve) => {
  s.listen(0, '127.0.0.1', () => resolve((s.address() as AddressInfo).port));
});

const close = (s: Listenable) => new Promise<void>((resolve) => {
  s.close(() => resolve());
});
