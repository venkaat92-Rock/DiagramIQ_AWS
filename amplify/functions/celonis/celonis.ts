/**
 * Talking to Celonis: an OAuth token, and the two ways in.
 *
 * A tenant exposes its process knowledge through one of two doors, and which
 * one a customer has is not a choice DiagramIQ gets to make:
 *
 *   MCP   — the Agent Tools asset published from Celonis Studio. The native
 *           path, and the one with a tool list of the customer's choosing.
 *   KM    — the Knowledge Model REST API. Every tenant with a Knowledge Model
 *           has this, whether or not anybody published an Agent Tools asset.
 *
 * This file holds what both need: the configuration, the OAuth handshake, and
 * the read-only guard. The two doors themselves are in knowledge.ts, behind
 * one interface, so the rest of the function never asks which one it got.
 *
 * Two deliberate choices about the configuration.
 *
 * 1. The credentials live in SSM Parameter Store, not in the function's
 *    environment block and not in an Amplify secret. An Amplify secret is
 *    resolved at deploy time, so a stack that references one before anybody
 *    has set it fails the whole pipeline — the Celonis feature would take the
 *    live app down with it. SSM is read at runtime: absent parameters mean
 *    "Celonis is not connected yet", which is a message, not an outage. It
 *    also means rotating a client secret is one CLI call, with no redeploy.
 *
 * 2. Over MCP, tool names are discovered, never assumed. What an Agent Tools
 *    asset publishes is the customer's choice, so this asks the server what it
 *    has. A hard-coded tool name would work in exactly one tenant.
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js';
import { GetParametersByPathCommand, SSMClient } from '@aws-sdk/client-ssm';

/** Which door to use. 'auto' prefers MCP and falls back to the Knowledge
 *  Model API — an Agent Tools asset is curated for agents, so where somebody
 *  published one it is the better read. */
export type SourceKind = 'mcp' | 'km';

export type CelonisConfig = {
  teamUrl: string;
  source: SourceKind | 'auto';
  // MCP
  mcpUrl: string;
  scope: string;
  // Knowledge Model REST API
  kmBaseUrl: string;
  kmId: string;
  kmScope: string;
  // Both
  clientId: string;
  clientSecret: string;
  blockedWords: string[];
};

export class CelonisNotConfigured extends Error {}

/** Where the connection details live. One parameter per field, so the client
 *  secret can be a SecureString while the rest stay readable. */
export const SSM_PREFIX = process.env.CELONIS_SSM_PREFIX ?? '/diagramiq/celonis';

const FIELDS = ['TEAM_URL', 'MCP_URL', 'CLIENT_ID', 'CLIENT_SECRET', 'SCOPE',
                'BLOCKED_TOOL_WORDS', 'SOURCE', 'KM_BASE_URL', 'KM_ID',
                'KM_SCOPE'] as const;
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

/** The fields each door needs on top of the three every door needs. */
const COMMON: Field[] = ['TEAM_URL', 'CLIENT_ID', 'CLIENT_SECRET'];
const NEEDS: Record<SourceKind, Field[]> = {
  mcp: ['MCP_URL'],
  km: ['KM_ID'],
};

const label = (f: Field) => `${SSM_PREFIX}/${f.toLowerCase()}`;

/** Read the configuration. The environment wins over SSM so a test — or a
 *  local run against a stub — can override one field without touching the
 *  account's parameters. When the environment carries a complete source,
 *  SSM is not called at all: there is nothing left to learn from it, and the
 *  tests have no Parameter Store to answer them. */
export async function readConfig(): Promise<CelonisConfig> {
  const fromEnv = (f: Field) => (process.env[`CELONIS_${f}`] ?? '').trim();
  const envSource = (fromEnv('SOURCE') || 'auto').toLowerCase();
  const envComplete = COMMON.every((f) => fromEnv(f))
    && (['mcp', 'km'] as SourceKind[]).some(
      (k) => (envSource === 'auto' || envSource === k) && NEEDS[k].every((f) => fromEnv(f)));

  const stored = envComplete ? {} : await fromSsm();
  const get = (f: Field) => fromEnv(f) || (stored[f] ?? '');

  const asked = (get('SOURCE') || 'auto').toLowerCase();
  if (!['auto', 'mcp', 'km'].includes(asked)) {
    throw new CelonisNotConfigured(
      `${label('SOURCE')} is '${asked}'. It has to be mcp, km or auto.`);
  }

  const missingCommon = COMMON.filter((f) => !get(f));
  if (missingCommon.length) {
    throw new CelonisNotConfigured(
      `Celonis is not connected yet. Missing ${missingCommon.map(label).join(', ')}. `
      + 'See README, "Connecting Celonis".');
  }

  const ready = (['mcp', 'km'] as SourceKind[]).filter((k) => NEEDS[k].every((f) => get(f)));
  if (asked !== 'auto' && !ready.includes(asked as SourceKind)) {
    throw new CelonisNotConfigured(
      `${label('SOURCE')} asks for '${asked}', but `
      + `${NEEDS[asked as SourceKind].filter((f) => !get(f)).map(label).join(', ')} `
      + 'is not set.');
  }
  if (!ready.length) {
    throw new CelonisNotConfigured(
      'Celonis is not connected yet. Set either '
      + `${label('MCP_URL')} (the Agent Tools asset's MCP Server URL) or `
      + `${label('KM_ID')} (a Knowledge Model id). See README, "Connecting Celonis".`);
  }

  const teamUrl = get('TEAM_URL').replace(/\/+$/, '');
  return {
    teamUrl,
    // 'auto' prefers MCP where both are configured.
    source: asked === 'auto' ? (ready.includes('mcp') ? 'mcp' : 'km') : (asked as SourceKind),
    mcpUrl: get('MCP_URL'),
    scope: get('SCOPE') || 'mcp-asset.tools:execute',
    kmBaseUrl: (get('KM_BASE_URL') || `${teamUrl}/intelligence/api`).replace(/\/+$/, ''),
    kmId: get('KM_ID'),
    kmScope: get('KM_SCOPE') || 'intelligence.knowledge-models:read',
    clientId: get('CLIENT_ID'),
    clientSecret: get('CLIENT_SECRET'),
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
 *  visible from here, so try post and fall back to basic.
 *
 *  The scope is passed in rather than read from the config: the two doors want
 *  different ones (mcp-asset.tools:execute against an Agent Tools asset,
 *  intelligence.knowledge-models:read against the Knowledge Model API), and a
 *  token minted for one is refused by the other. */
export async function getToken(cfg: CelonisConfig, scope: string): Promise<string> {
  const url = `${cfg.teamUrl}/oauth2/token`;
  const base = { grant_type: 'client_credentials', scope };

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
    throw new Error(`Celonis rejected the credentials for scope '${scope}' `
                    + `(HTTP ${resp.status}): ${body}`);
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

/** Connect to the Agent Tools asset and return the read-only tools it
 *  publishes. */
export async function connect(cfg: CelonisConfig): Promise<Connected> {
  const token = await getToken(cfg, cfg.scope);
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

/** Call one tool on the Agent Tools asset and flatten the result to text.
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
