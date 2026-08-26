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

// allow-methods and max-age matter now that the function answers its own
// preflight: behind the gateway that was the gateway's job, but a Function URL
// without a CORS configuration forwards OPTIONS straight here, and a preflight
// without allow-methods is rejected by the browser.
const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers': 'content-type',
  'access-control-allow-methods': 'POST,OPTIONS',
  'access-control-max-age': '86400',
  'content-type': 'application/json',
};

const reply = (status: number, body: unknown) => ({
  statusCode: status,
  headers: CORS,
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
  const modelId: string = body.modelId || process.env.MODEL_ID || 'us.anthropic.claude-opus-4-8-v1:0';

  try {
    if (path.endsWith('/convert')) {
      const b64: string = body.imageBase64 || '';
      if (!b64) return reply(400, { error: 'imageBase64 is required.' });
      let format = String(body.mediaType || 'image/png').split('/').pop()!.toLowerCase();
      if (format === 'jpg') format = 'jpeg';
      if (!['png', 'jpeg', 'gif', 'webp'].includes(format)) format = 'png';
      const content: ContentBlock[] = [
        { image: { format: format as 'png' | 'jpeg' | 'gif' | 'webp', source: { bytes: Buffer.from(b64, 'base64') } } },
        { text: 'Extract this process. Return the JSON object only.' },
      ];
      const text = await converse(modelId, SYSTEM_EXTRACT, content);
      return reply(200, { model: extractJson(text) });
    }

    if (path.endsWith('/feedback')) {
      if (!body.model || !body.feedback) return reply(400, { error: 'model and feedback are required.' });
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
      return reply(200, {
        model,
        changes: lines(out?.changes),
        notApplied: lines(out?.not_applied),
      });
    }

    return reply(404, { error: `Unknown route: ${path}` });
  } catch (err: any) {
    const msg = err?.message || String(err);
    // Surface actionable Bedrock errors (model access / region) to the UI.
    return reply(502, { error: `Bedrock call failed for '${modelId}': ${msg}` });
  }
};
