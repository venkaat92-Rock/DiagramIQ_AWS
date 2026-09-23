/**
 * DiagramIQ AWS — Bedrock proxy Lambda.
 *
 * Routes (HTTP API v2):
 *   POST /convert   { imageBase64, mediaType, processName?, modelId? }
 *                   -> { model: <structured process JSON> }
 *   POST /feedback  { model, feedback, modelId? }
 *                   -> { model, changes[], notApplied[] }
 *
 * The Lambda's IAM role carries bedrock:InvokeModel — no API keys anywhere.
 * The BPMN XML itself is built in the browser (frontend/bpmnBuilder.js) from
 * the structured model, so this function stays a thin, auditable AI proxy.
 */
import {
  BedrockRuntimeClient,
  ConverseCommand,
  type ContentBlock,
} from '@aws-sdk/client-bedrock-runtime';

const client = new BedrockRuntimeClient({});

const SYSTEM_EXTRACT = `You are a meticulous BPMN 2.0 analyst. You are given an IMAGE of a business process diagram (often a cross-functional / swimlane flowchart). Read it COMPLETELY and return its exact structure.
Rules:
- Identify every swimlane top-to-bottom (use the exact lane titles).
- Identify every shape: its TYPE (start | end | task | gateway | intermediate), its EXACT text label INCLUDING any system/sub-label shown inside the box (e.g. 'Validate request (SharePoint)', 'Setup material (SAP)'), and which lane it is in.
- Give each shape a GRID POSITION so the layout can be rebuilt faithfully: 'col' = its left-to-right order as an integer (0 = leftmost; shapes stacked vertically share the same col), and 'row' = 0-based vertical position WITHIN its lane (0 = top row of that lane; use 1,2 only when a lane stacks shapes).
- Capture EVERY connector as a flow with its source id, target id, and any edge label (e.g. 'Yes','No'). Include loop-backs.
- A diamond is a gateway; keep its question concise.
Return ONLY a JSON object, no prose, no markdown fences:
{"process_name":"<title>","lanes":["<top lane>","<next>"],"nodes":[{"id":"n1","type":"start|end|task|gateway|intermediate","name":"<label>","lane":"<lane title>","col":0,"row":0}],"flows":[{"from":"n1","to":"n2","label":""}]}`;

const SYSTEM_FEEDBACK = `You are a senior BPMN process modeller correcting a structured process model on a reviewer's instruction.

You receive (1) the current model as JSON — lanes, nodes with grid positions, flows — and (2) the reviewer's feedback.

WHAT YOU CONTROL
- A node's "name", "type" and "lane".
- A node's "col" (left-to-right order) and "row" (position within its own lane). These are GRID INDICES, not pixels.
- Adding or deleting nodes and flows; flow labels; the lane list and its order; "process_name".

WHAT YOU DO NOT CONTROL
The picture is drawn from this model by a separate layout engine. You cannot change connector routing, line spacing, arrow paths, box size, colour, font, or the size of the canvas. If the feedback is about how the diagram LOOKS rather than what the process CONTAINS — lines crossing or overlapping, connectors running outside the frame or the pool, boxes too close together, the diagram not fitting on screen — then change NOTHING and say so in "not_applied". Editing the model to chase a drawing problem is the worst possible answer: it corrupts the process and does not fix the picture.

HOW TO APPLY FEEDBACK
- Change only what the feedback asks for. Every other node, flow, label, lane and grid position must come back byte-identical to what you received.
- Keep every existing "id" unchanged. A new node gets a new id that collides with nothing.
- Read the reviewer literally. If they name an element, act on that element and nothing near it. Do not tidy, rename, re-order or "improve" anything you were not asked about.
- If the feedback is ambiguous, or names something that is not in the model, do not guess. Leave the model unchanged and explain in "not_applied".

OUTPUT — one JSON object, no prose, no markdown fences:
{"model": {<the full model, same schema as the input>},
 "changes": ["<one short line per edit you made, naming the element>"],
 "not_applied": ["<one line per part of the feedback you did not act on, and why>"]}

Every edit you made must appear in "changes". If you changed nothing, "changes" is [] and "not_applied" says why.`;

// No CORS headers here. Both endpoints in front of this function declare their
// own, and a Function URL *adds* its configured headers to whatever the
// function returns — so returning them too produced
// `access-control-allow-origin: *, *`, which the browser rejects outright while
// the function logs a clean 200. See the note in python-analyzer/index.py.
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';
import { CognitoJwtVerifier } from 'aws-jwt-verify';

/* ---------- who is calling, and what they did ------------------------------
 *
 * The engine verifies tokens by asking Cognito (no crypto library fits its
 * pure-Python zip); here the library is available, so the token is verified
 * locally against the pool's public keys — no network call per request.
 *
 * REQUIRE_AUTH gates enforcement, and ships false: deploying the login layer
 * must not lock anyone out before someone has proved they can sign in.
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

async function identify(event: any): Promise<Who> {
  const header: string = event?.headers?.authorization ?? event?.headers?.Authorization ?? '';
  const token = header.replace(/^Bearer\s+/i, '').trim();
  if (!token) {
    if (REQUIRE_AUTH) throw Object.assign(new Error('Sign in to use DiagramIQ.'), { status: 401 });
    return ANONYMOUS;
  }
  if (!verifier) return ANONYMOUS;          // pool not wired yet
  try {
    const payload: any = await verifier.verify(token);
    return { sub: payload.sub, label: payload.username ?? payload.sub };
  } catch {
    throw Object.assign(new Error('Your session has expired. Sign in again.'), { status: 401 });
  }
}

/** A shallow summary of the request — never the image, never the model. */
function summarise(body: any) {
  const out: Record<string, unknown> = {};
  if (!body || typeof body !== 'object') return out;
  for (const key of ['modelId', 'mediaType', 'processName']) {
    if (body[key]) out[key] = String(body[key]).slice(0, 160);
  }
  if (body.imageBase64) out.imageBytes = String(body.imageBase64).length;
  if (body.feedback) out.feedbackChars = String(body.feedback).length;
  if (body.model?.nodes) out.nodes = body.model.nodes.length;
  return out;
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
        surface: 'ai',
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

const RESPONSE_HEADERS = { 'content-type': 'application/json' };

const reply = (status: number, body: unknown) => ({
  statusCode: status,
  headers: RESPONSE_HEADERS,
  body: JSON.stringify(body),
});

function extractJson(text: string): unknown {
  const cleaned = text.replace(/^```[a-zA-Z]*\s*/m, '').replace(/\s*```\s*$/m, '');
  const start = cleaned.indexOf('{');
  const end = cleaned.lastIndexOf('}');
  if (start < 0 || end <= start) throw new Error('The model did not return a JSON object.');
  return JSON.parse(cleaned.slice(start, end + 1));
}

async function converse(modelId: string, system: string, content: ContentBlock[]) {
  const resp = await client.send(
    new ConverseCommand({
      modelId,
      system: [{ text: system }],
      messages: [{ role: 'user', content }],
      inferenceConfig: { maxTokens: 8000, temperature: 0 },
    }),
  );
  const blocks = resp.output?.message?.content ?? [];
  return blocks.map((b) => ('text' in b ? b.text : '')).join('');
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const handler = async (event: any) => {
  const path: string = event?.rawPath ?? event?.path ?? '';
  if ((event?.requestContext?.http?.method ?? '') === 'OPTIONS') return reply(200, {});
  let body: any = {};
  try {
    body = event?.body ? JSON.parse(event.isBase64Encoded ? Buffer.from(event.body, 'base64').toString() : event.body) : {};
  } catch {
    return reply(400, { error: 'Invalid JSON body.' });
  }

  const startedAt = Date.now();
  const action = path.endsWith('/feedback') ? '/feedback' : '/convert';
  const detail = summarise(body);
  let who: Who;
  try {
    who = await identify(event);
  } catch (err: any) {
    await audit(ANONYMOUS, action, err.status ?? 401, startedAt, detail, err.message);
    return reply(err.status ?? 401, { error: err.message });
  }

  const done = async (status: number, payload: unknown, error = '') => {
    await audit(who, action, status, startedAt, detail, error);
    return reply(status, payload);
  };
  const modelId: string = body.modelId || process.env.MODEL_ID || 'us.anthropic.claude-opus-4-8-v1:0';

  try {
    if (path.endsWith('/convert')) {
      const b64: string = body.imageBase64 || '';
      if (!b64) return done(400, { error: 'imageBase64 is required.' }, 'imageBase64 is required');
      let format = String(body.mediaType || 'image/png').split('/').pop()!.toLowerCase();
      if (format === 'jpg') format = 'jpeg';
      if (!['png', 'jpeg', 'gif', 'webp'].includes(format)) format = 'png';
      const content: ContentBlock[] = [
        { image: { format: format as 'png' | 'jpeg' | 'gif' | 'webp', source: { bytes: Buffer.from(b64, 'base64') } } },
        { text: 'Extract this process. Return the JSON object only.' },
      ];
      const text = await converse(modelId, SYSTEM_EXTRACT, content);
      return done(200, { model: extractJson(text) });
    }

    if (path.endsWith('/feedback')) {
      if (!body.model || !body.feedback) return done(400, { error: 'model and feedback are required.' }, 'model and feedback are required');
      const prompt =
        'Current model JSON:\n' + JSON.stringify(body.model) +
        '\n\nReviewer feedback to apply:\n' + String(body.feedback) +
        '\n\nReturn the JSON object with "model", "changes" and "not_applied".';
      const text = await converse(modelId, SYSTEM_FEEDBACK, [{ text: prompt }]);
      const out = extractJson(text) as Record<string, unknown>;
      // Tolerate a bare model: older prompts returned one, and a model that
      // ignores the envelope should still produce a usable edit rather than a
      // 502. `nodes` is the tell — the envelope never carries it at top level.
      const model = (out && typeof out === 'object' && 'nodes' in out) ? out : out?.model;
      if (!model) throw new Error('The model returned no "model" object.');
      const lines = (v: unknown) =>
        (Array.isArray(v) ? v : []).map((x) => String(x)).filter(Boolean).slice(0, 20);
      return done(200, {
        model,
        changes: lines(out?.changes),
        notApplied: lines(out?.not_applied),
      });
    }

    return done(404, { error: `Unknown route: ${path}` }, 'unknown route');
  } catch (err: any) {
    const msg = err?.message || String(err);
    // Surface actionable Bedrock errors (model access / region) to the UI.
    return done(502, { error: `Bedrock call failed for '${modelId}': ${msg}` }, msg);
  }
};
