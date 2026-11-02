/**
 * Talking to Celonis: an OAuth token, and an MCP session to the Agent Tools
 * asset a customer publishes in Celonis Studio.
 *
 * Two deliberate choices here.
 *
 * 1. The credentials live in SSM Parameter Store, not in the function's
 *    environment block and not in an Amplify secret. An Amplify secret is
 *    resolved at deploy time, so a stack that references one before anybody
 *    has set it fails the whole pipeline — the Celonis feature would take the
 *    live app down with it. SSM is read at runtime: absent parameters mean
 *    "Celonis is not connected yet", which is a message, not an outage. It
 *    also means rotating a client secret is one CLI call, with no redeploy.
 *
 * 2. Tool names are discovered, never assumed. What an Agent Tools asset
 *    publishes is the customer's choice — one tenant exposes a knowledge-model
 *    reader, another exposes something named differently — so this asks the
 *    server what it has and hands the list to the model. A hard-coded tool
 *    name would work in exactly one tenant.
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { GetParametersByPathCommand, SSMClient } from '@aws-sdk/client-ssm';

export type CelonisConfig = {
  teamUrl: string;
  mcpUrl: string;
  clientId: string;
  clientSecret: string;
  scope: string;
  blockedWords: string[];
};

export class CelonisNotConfigured extends Error {}

/** Where the connection details live. One parameter per field, so the client
 *  secret can be a SecureString while the rest stay readable. */
export const SSM_PREFIX = process.env.CELONIS_SSM_PREFIX ?? '/diagramiq/celonis';

const FIELDS = ['TEAM_URL', 'MCP_URL', 'CLIENT_ID', 'CLIENT_SECRET', 'SCOPE',
                'BLOCKED_TOOL_WORDS'] as const;
type Field = (typeof FIELDS)[number];

const ssm = new SSMClient({});

/** Parameters, cached for the life of the container. Short enough that a
 *  rotation is picked up by the next cold start, long enough that a tool-use
 *  loop making a dozen calls does not hammer Parameter Store. */
let cache: { at: number; values: Partial<Record<Field, string>> } | null = null;
const CACHE_MS = 5 * 60 * 1000;

async function fromSsm(): Promise<Partial<Record<Field, string>>> {
  if (cache && Date.now() - cache.at < CACHE_MS) return cache.values;
  const values: Partial<Record<Field, string>> = {};
  try {
    let token: string | undefined;
    do {
      const page = await ssm.send(new GetParametersByPathCommand({
        Path: SSM_PREFIX, WithDecryption: true, NextToken: token,
      }));
      for (const p of page.Parameters ?? []) {
        const name = (p.Name ?? '').split('/').pop()?.toUpperCase() as Field;
        if (FIELDS.includes(name) && p.Value) values[name] = p.Value.trim();
      }
      token = page.NextToken;
    } while (token);
  } catch (err) {
    // Not fatal on its own: the environment may carry the config instead (the
    // stub-server tests do exactly that). But it is never silent — a denied
    // GetParametersByPath is a misconfigured IAM policy, and swallowing it
    // would surface later as the far more confusing "Celonis is not connected".
    console.error(`SSM read failed under ${SSM_PREFIX}:`, err);
  }
  cache = { at: Date.now(), values };
  return values;
}

const REQUIRED: Field[] = ['TEAM_URL', 'MCP_URL', 'CLIENT_ID', 'CLIENT_SECRET'];

/** Read the configuration. The environment wins over SSM so a test — or a
 *  local run against the stub server — can override one field without
 *  touching the account's parameters. When the environment carries everything,
 *  SSM is not called at all: there is nothing left to learn from it, and the
 *  tests have no Parameter Store to answer them. */
export async function readConfig(): Promise<CelonisConfig> {
  const fromEnv = (f: Field) => (process.env[`CELONIS_${f}`] ?? '').trim();
  const complete = REQUIRED.every((f) => fromEnv(f));
  const stored = complete ? {} : await fromSsm();
  const get = (f: Field) => fromEnv(f) || (stored[f] ?? '');

  const missing = REQUIRED.filter((f) => !get(f));
  if (missing.length) {
    throw new CelonisNotConfigured(
      'Celonis is not connected yet. Missing ' +
      missing.map((f) => `${SSM_PREFIX}/${f.toLowerCase()}`).join(', ') +
      '. See README, "Connecting Celonis".');
  }

  return {
    teamUrl: get('TEAM_URL').replace(/\/+$/, ''),
    mcpUrl: get('MCP_URL'),
    clientId: get('CLIENT_ID'),
    clientSecret: get('CLIENT_SECRET'),
    scope: get('SCOPE') || 'mcp-asset.tools:execute',
    // Read-only guard. A tool whose name suggests it changes something is
    // never offered to the model, whatever the asset happens to publish.
    // DiagramIQ reads a process; it has no business triggering an automation
    // in somebody's Celonis tenant.
    blockedWords: (get('BLOCKED_TOOL_WORDS') ||
      'trigger,action,orchestration,resume,execute,write,create,update,delete,publish')
      .split(',').map((w) => w.trim().toLowerCase()).filter(Boolean),
  };
}

/** OAuth2 client credentials. Celonis application keys are configured for
 *  either client_secret_post or client_secret_basic, and which one is not
 *  visible from here, so try post and fall back to basic. */
export async function getToken(cfg: CelonisConfig): Promise<string> {
  const url = `${cfg.teamUrl}/oauth2/token`;
  const base = { grant_type: 'client_credentials', scope: cfg.scope };

  let resp = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ ...base, client_id: cfg.clientId,
                                client_secret: cfg.clientSecret }),
  });
  if (resp.status === 400 || resp.status === 401) {
    const basic = Buffer.from(`${cfg.clientId}:${cfg.clientSecret}`).toString('base64');
    resp = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded',
                 authorization: `Basic ${basic}` },
      body: new URLSearchParams(base),
    });
  }
  if (!resp.ok) {
    const body = (await resp.text()).slice(0, 300);
    throw new Error(`Celonis rejected the credentials (HTTP ${resp.status}): ${body}`);
  }
  const json = (await resp.json()) as { access_token?: string };
  if (!json.access_token) throw new Error('Celonis returned no access_token.');
  return json.access_token;
}

export const isBlocked = (name: string, cfg: CelonisConfig) =>
  cfg.blockedWords.some((w) => name.toLowerCase().includes(w));

export type DiscoveredTool = {
  name: string;
  description: string;
  schema: Record<string, unknown>;
};

export type Connected = {
  client: Client;
  tools: DiscoveredTool[];
  blocked: string[];
  close: () => Promise<void>;
};

/** Connect, initialise, and return the read-only tools the asset publishes. */
export async function connect(cfg: CelonisConfig): Promise<Connected> {
  const token = await getToken(cfg);
  const transport = new StreamableHTTPClientTransport(new URL(cfg.mcpUrl), {
    requestInit: { headers: { authorization: `Bearer ${token}` } },
  });
  const client = new Client({ name: 'diagramiq', version: '1.0.0' },
                            { capabilities: {} });
  await client.connect(transport);

  const listed = await client.listTools();
  const blocked = listed.tools.filter((t) => isBlocked(t.name, cfg)).map((t) => t.name);
  const tools: DiscoveredTool[] = listed.tools
    .filter((t) => !isBlocked(t.name, cfg))
    .map((t) => ({
      name: t.name,
      description: t.description ?? '',
      schema: (t.inputSchema as Record<string, unknown>) ??
              { type: 'object', properties: {} },
    }));

  return { client, tools, blocked, close: () => client.close() };
}

/** Call one discovered tool and flatten the result to text.
 *  Refuses anything the guard blocks, even if the caller asks by name — the
 *  filter in connect() narrows what the model is offered, and this refuses
 *  what it tries. Two layers because the model picks the name, and a model
 *  can be talked into picking a name it was never shown. */
export async function callTool(
  conn: Connected, cfg: CelonisConfig, name: string, args: Record<string, unknown>,
): Promise<string> {
  if (isBlocked(name, cfg)) {
    throw new Error(`Refused: '${name}' is not a read-only tool.`);
  }
  if (!conn.tools.some((t) => t.name === name)) {
    throw new Error(`Refused: '${name}' is not one of the published tools.`);
  }
  const res = await conn.client.callTool({ name, arguments: args });
  const parts = (res.content as { type: string; text?: string }[] | undefined) ?? [];
  const text = parts.map((p) => (p.type === 'text' ? (p.text ?? '') : `[${p.type}]`))
                    .join('\n').trim();
  if (res.isError) throw new Error(text || `Tool '${name}' returned an error.`);
  return text;
}
