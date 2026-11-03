/**
 * DiagramIQ AWS — Celonis process-mining Lambda.
 *
 * Routes:
 *   POST /celonis/status    { }                       -> { connected, tools[], blocked[] }
 *   POST /celonis/discover  { question?, processName?, modelId? }
 *                           -> { discovery, toolCalls[], notes[] }
 *
 * What this does, in one line: it asks a Celonis tenant what really happens in
 * a process, and turns the answer into the same `discovery` object every other
 * DiagramIQ input produces — so it lands in the existing review grid, gets
 * approved by a human, and is drawn by the existing BPMN builder.
 *
 * Why a tool-use loop rather than one prompt: nobody here knows what the
 * customer's Agent Tools asset publishes, or what their knowledge model calls
 * its activity column. The model is given the discovered tools and left to
 * work that out — list what is available, read the activities, read the
 * directly-follows relations — which is exactly the job an LLM is good at and
 * a hard-coded query is not.
 *
 * What it never does: write to Celonis. The tool list is filtered on the way
 * in and the call is refused on the way out (celonis.ts), so a model that
 * invents the name of an automation tool gets an error, not an action.
 */
import {
  BedrockRuntimeClient,
  ConverseCommand,
  type Message,
  type Tool,
  type ToolInputSchema,
} from '@aws-sdk/client-bedrock-runtime';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import {
  CelonisNotConfigured, type Connected, callTool, connect, readConfig,
} from './celonis.js';

const bedrock = new BedrockRuntimeClient({});

/* ---------- the model's brief ---------------------------------------------- */

const SYSTEM = `You are a process-mining analyst building a BPMN process definition from a Celonis tenant.

You have read-only tools onto a Celonis team. You do not know in advance what they are called or what data they expose — that is the first thing to find out. Explore before you conclude.

HOW TO WORK
1. Start by listing what is available: data pools, data models, knowledge models, published views or whatever the tools expose. Do not guess an object name; read it.
2. Find the process the user asked about. If more than one candidate matches, pick the closest and say which one you chose in "notes".
3. Read the real behaviour, not the documentation of it. What you want is:
   - the ACTIVITIES that occur, with how often each occurs,
   - the DIRECTLY-FOLLOWS relations between them (which activity leads to which), with frequencies,
   - who or what performs each activity (a resource, user, role or department column),
   - the systems involved if the data says so,
   - anything the data shows about rework, loops, long waits or deviations.
4. Prefer the main path. A mined graph has a long tail of rare variants; a BPMN diagram that tries to draw all of them is useless. Keep the activities that cover the bulk of the cases and name the dropped tail in "notes".

RULES ON EVIDENCE
- Every activity you output must have come from a tool result. If you could not read the data, say so in "notes" and return the steps you do have — an empty list is a better answer than an invented process.
- Never put a number in "frequency" that a tool did not give you. Copy the case or occurrence count you actually read, as a count ("12,480 cases") or leave it empty.
- If a tool errors, read the message and try a different tool or different arguments. Do not retry the identical call.

OUTPUT
When you have what you need, reply with ONE JSON object and nothing else — no prose, no markdown fences:
{
  "process_name": "<short name>",
  "trigger_event": "<the activity or event that starts a case>",
  "successful_outcome": "<how a case ends well>",
  "unsuccessful_outcome": "<how a case ends badly, or empty string>",
  "steps": [
    {
      "activity": "<Verb Object, max 5 words — the mined activity name, tidied>",
      "description": "<max 15 words, what happens here>",
      "participant": "<role/resource that performs it, max 4 words>",
      "it_systems": "<system name or empty>",
      "input_document": "", "output_document": "", "templates": "",
      "dependency": "<the condition under which this step happens, or empty>",
      "frequency": "<the count you read, or empty>",
      "pain_points": "<rework, loop or wait the data shows here, or empty>"
    }
  ],
  "notes": ["<one line per judgement call you made, tool you could not use, or variant tail you dropped>"]
}
Order "steps" along the dominant path, first activity first.`;

const MAX_TURNS = 12;          // tool round trips before we stop and report
const MAX_TOOL_CHARS = 24000;  // per result, fed back to the model

/* ---------- who is calling, and what they did ------------------------------
 * Same shape as the Bedrock proxy's: verified locally against the pool's
 * public keys, and REQUIRE_AUTH gates enforcement rather than wiring.
 */
const REQUIRE_AUTH = ['1', 'true', 'yes'].includes(
  String(process.env.REQUIRE_AUTH ?? 'false').trim().toLowerCase(),
);
const AUDIT_TABLE = process.env.AUDIT_TABLE ?? '';
const RETENTION_DAYS = Number(process.env.AUDIT_RETENTION_DAYS ?? 400);

const verifier = process.env.USER_POOL_ID
  ? CognitoJwtVerifier.create({
      userPoolId: process.env.USER_POOL_ID,
      tokenUse: 'access',
      clientId: process.env.USER_POOL_CLIENT_ID ?? null,
    })
  : null;

const ddb = AUDIT_TABLE ? DynamoDBDocumentClient.from(new DynamoDBClient({})) : null;

type Who = { sub: string; label: string };
const ANONYMOUS: Who = { sub: 'anonymous', label: 'anonymous' };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
async function identify(event: any): Promise<Who> {
  const header: string =
    event?.headers?.authorization ?? event?.headers?.Authorization ?? '';
  const token = header.replace(/^Bearer\s+/i, '').trim();
  if (!token) {
    if (REQUIRE_AUTH) {
      throw Object.assign(new Error('Sign in to use DiagramIQ.'), { status: 401 });
    }
    return ANONYMOUS;
  }
  if (!verifier) return ANONYMOUS;
  try {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const payload: any = await verifier.verify(token);
    return { sub: payload.sub, label: payload.username ?? payload.sub };
  } catch {
    throw Object.assign(new Error('Your session has expired. Sign in again.'),
                        { status: 401 });
  }
}

async function audit(who: Who, action: string, status: number, startedAt: number,
                     detail: Record<string, unknown>, error = '') {
  if (!ddb) return;
  const now = new Date();
  try {
    await ddb.send(new PutCommand({
      TableName: AUDIT_TABLE,
      Item: {
        pk: `USER#${who.sub}`,
        sk: `${now.toISOString()}#${Math.random().toString(36).slice(2, 8)}`,
        day: now.toISOString().slice(0, 10),
        ts: now.toISOString(),
        actor: who.label,
        actorSub: who.sub,
        action,
        surface: 'celonis',
        status,
        ms: Date.now() - startedAt,
        detail,
        ...(error ? { error: error.slice(0, 160) } : {}),
        expiresAt: Math.floor(now.getTime() / 1000) + RETENTION_DAYS * 86400,
      },
    }));
  } catch (err) {
    console.error('audit write failed', action, err);   // never fatal
  }
}

/* ---------- plumbing ------------------------------------------------------- */

const reply = (status: number, body: unknown) => ({
  statusCode: status,
  headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

function extractJson(text: string): Record<string, unknown> {
  const cleaned = text.replace(/^```[a-zA-Z]*\s*/m, '').replace(/\s*```\s*$/m, '');
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) {
    throw new Error('The model did not return a JSON object.');
  }
  return JSON.parse(cleaned.slice(start, end + 1));
}

const STEP_FIELDS = ['activity', 'description', 'participant', 'it_systems',
                     'input_document', 'output_document', 'templates',
                     'dependency', 'frequency', 'pain_points'] as const;

/** Coerce whatever came back into the discovery shape the review grid and
 *  /discovery-to-bpmn expect. Missing keys become empty strings rather than
 *  undefined: the grid renders them, and the engine writes them to a sheet. */
function toDiscovery(raw: Record<string, unknown>, fallbackName: string) {
  const str = (v: unknown) => (v === null || v === undefined ? '' : String(v)).trim();
  const steps = (Array.isArray(raw.steps) ? raw.steps : [])
    .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
    .map((s) => {
      const out: Record<string, string> = {};
      for (const f of STEP_FIELDS) out[f] = str(s[f]);
      return out;
    })
    .filter((s) => s.activity);
  return {
    discovery: {
      process_name: str(raw.process_name) || fallbackName || 'Mined Process',
      trigger_event: str(raw.trigger_event),
      successful_outcome: str(raw.successful_outcome),
      unsuccessful_outcome: str(raw.unsuccessful_outcome),
      steps,
    },
    notes: (Array.isArray(raw.notes) ? raw.notes : [])
      .map((n) => str(n)).filter(Boolean).slice(0, 25),
  };
}

/** The discovered MCP tools, in Bedrock Converse's toolConfig shape. */
function toolConfig(conn: Connected): { tools: Tool[] } {
  return {
    tools: conn.tools.map((t): Tool.ToolSpecMember => ({
      toolSpec: {
        name: t.name,
        description: (t.description || t.name).slice(0, 900),
        // The MCP server's own JSON Schema, passed through untouched: it is
        // the only description of the tool's arguments anyone has.
        inputSchema: { json: t.schema as ToolInputSchema.JsonMember['json'] },
      },
    })),
  };
}

type ToolCall = { tool: string; args: unknown; ok: boolean; chars: number; error?: string };

/** Converse, with the Celonis tools, until the model stops asking for them. */
async function runLoop(conn: Connected, cfg: Awaited<ReturnType<typeof readConfig>>,
                       modelId: string, ask: string) {
  const messages: Message[] = [{ role: 'user', content: [{ text: ask }] }];
  const calls: ToolCall[] = [];
  let lastText = '';

  for (let turn = 0; turn < MAX_TURNS; turn += 1) {
    const resp = await bedrock.send(new ConverseCommand({
      modelId,
      system: [{ text: SYSTEM }],
      messages,
      toolConfig: toolConfig(conn),
      inferenceConfig: { maxTokens: 8000, temperature: 0 },
    }));

    const out = resp.output?.message;
    if (!out) throw new Error('Bedrock returned no message.');
    messages.push(out);

    const blocks = out.content ?? [];
    lastText = blocks.map((b) => ('text' in b && b.text ? b.text : '')).join('').trim()
               || lastText;

    const uses = blocks.flatMap((b) => ('toolUse' in b && b.toolUse ? [b.toolUse] : []));
    if (!uses.length) {
      return { text: lastText, calls, exhausted: false };
    }

    // Run every tool the model asked for this turn, and answer each one —
    // Converse rejects the next request if a toolUse has no matching result.
    const results = [];
    for (const use of uses) {
      const name = use.name ?? '';
      const args = (use.input ?? {}) as Record<string, unknown>;
      try {
        const text = await callTool(conn, cfg, name, args);
        const clipped = text.length > MAX_TOOL_CHARS
          ? `${text.slice(0, MAX_TOOL_CHARS)}\n…[truncated, ${text.length} chars total]`
          : text;
        calls.push({ tool: name, args, ok: true, chars: text.length });
        results.push({
          toolResult: {
            toolUseId: use.toolUseId!,
            content: [{ text: clipped || '(the tool returned nothing)' }],
          },
        });
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        calls.push({ tool: name, args, ok: false, chars: 0, error: msg.slice(0, 300) });
        // The error goes back to the model on purpose: "that object does not
        // exist" is how it learns the tenant's real names.
        results.push({
          toolResult: {
            toolUseId: use.toolUseId!,
            content: [{ text: `ERROR: ${msg}` }],
            status: 'error' as const,
          },
        });
      }
    }
    messages.push({ role: 'user', content: results });
  }

  // Out of turns. Ask once for whatever it has, rather than returning nothing.
  messages.push({
    role: 'user',
    content: [{ text: 'Stop exploring and answer now with the JSON object, using '
                      + 'what you have. Record in "notes" what you could not finish.' }],
  });
  const final = await bedrock.send(new ConverseCommand({
    modelId,
    system: [{ text: SYSTEM }],
    messages,
    inferenceConfig: { maxTokens: 8000, temperature: 0 },
  }));
  const text = (final.output?.message?.content ?? [])
    .map((b) => ('text' in b && b.text ? b.text : '')).join('').trim();
  return { text: text || lastText, calls, exhausted: true };
}

/* ---------- the routes ----------------------------------------------------- */

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const handler = async (event: any) => {
  const path: string = event?.rawPath ?? event?.path ?? '';
  if ((event?.requestContext?.http?.method ?? '') === 'OPTIONS') return reply(200, {});

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let body: any = {};
  try {
    body = event?.body
      ? JSON.parse(event.isBase64Encoded
          ? Buffer.from(event.body, 'base64').toString() : event.body)
      : {};
  } catch {
    return reply(400, { error: 'Invalid JSON body.' });
  }

  const startedAt = Date.now();
  const action = path.endsWith('/status') ? '/celonis/status' : '/celonis/discover';
  // Never the tool results, never the mined data — only what was asked.
  const detail: Record<string, unknown> = {};
  if (body.processName) detail.processName = String(body.processName).slice(0, 160);
  if (body.question) detail.questionChars = String(body.question).length;
  if (body.modelId) detail.modelId = String(body.modelId).slice(0, 160);

  let who: Who;
  try {
    who = await identify(event);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  } catch (err: any) {
    await audit(ANONYMOUS, action, err.status ?? 401, startedAt, detail, err.message);
    return reply(err.status ?? 401, { error: err.message });
  }
  const done = async (status: number, payload: unknown, error = '') => {
    await audit(who, action, status, startedAt, detail, error);
    return reply(status, payload);
  };

  let conn: Connected | null = null;
  try {
    const cfg = await readConfig();

    if (path.endsWith('/celonis/status')) {
      conn = await connect(cfg);
      return done(200, {
        connected: true,
        team: cfg.teamUrl,
        tools: conn.tools.map((t) => ({ name: t.name, description: t.description })),
        blocked: conn.blocked,
      });
    }

    if (path.endsWith('/celonis/discover')) {
      const processName = String(body.processName ?? '').trim();
      const question = String(body.question ?? '').trim();
      if (!processName && !question) {
        return done(400, { error: 'Name the process, or describe what to look for.' },
                    'nothing asked');
      }
      const modelId: string = body.modelId || process.env.MODEL_ID
        || 'us.anthropic.claude-opus-4-5-20251101-v1:0';

      conn = await connect(cfg);
      if (!conn.tools.length) {
        return done(
          502,
          { error: 'The Celonis MCP server published no read-only tools'
                   + (conn.blocked.length
                      ? ` (${conn.blocked.length} were withheld as not read-only: `
                        + `${conn.blocked.join(', ')}).`
                      : '.')
                   + ' Check which tools the Agent Tools asset exposes in Celonis Studio.' },
          'no usable tools',
        );
      }

      const ask = [
        processName ? `Process to model: ${processName}` : '',
        question ? `What the reviewer wants to see: ${question}` : '',
        'Use the Celonis tools to read this process as it actually ran, then '
        + 'return the JSON object.',
      ].filter(Boolean).join('\n');

      const { text, calls, exhausted } = await runLoop(conn, cfg, modelId, ask);
      if (!text) throw new Error('The model returned nothing after reading Celonis.');

      const { discovery, notes } = toDiscovery(extractJson(text), processName);
      if (exhausted) {
        notes.push(`Stopped after ${MAX_TURNS} rounds of tool calls — the answer may be `
                   + 'incomplete.');
      }
      return done(200, {
        discovery,
        notes,
        toolCalls: calls,
        tools: conn.tools.map((t) => t.name),
      });
    }

    return done(404, { error: `Unknown route: ${path}` }, 'unknown route');
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    // "Not connected yet" is a configuration answer, not a failure of the
    // call: 409 so the UI can tell the person what to set rather than
    // reporting that Celonis is broken.
    if (err instanceof CelonisNotConfigured) {
      return done(409, { error: msg, connected: false }, 'not configured');
    }
    return done(502, { error: msg }, msg);
  } finally {
    if (conn) {
      try { await conn.close(); } catch { /* the request is already answered */ }
    }
  }
};
