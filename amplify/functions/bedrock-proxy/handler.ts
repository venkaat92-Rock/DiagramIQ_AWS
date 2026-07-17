/**
 * DiagramIQ AWS — Bedrock proxy Lambda.
 *
 * Routes (HTTP API v2):
 *   POST /convert   { imageBase64, mediaType, processName?, modelId? }
 *                   -> { model: <structured process JSON> }
 *   POST /feedback  { model, feedback, modelId? }
 *                   -> { model: <revised process JSON> }
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

const SYSTEM_FEEDBACK = `You are a senior BPMN process modeller. You receive (1) the current structured model of a business process as JSON (lanes, nodes with grid positions, flows), and (2) FEEDBACK from the human reviewer about mistakes to correct.
Apply the feedback conservatively: rename labels, add/remove/move nodes or flows, adjust lanes or grid positions — only what the feedback asks for. Keep every id stable where possible.
Return ONLY the FULL corrected JSON model in exactly the same schema, no prose, no markdown fences.`;

const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers': 'content-type',
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
        '\n\nReturn the full corrected JSON model only.';
      const text = await converse(modelId, SYSTEM_FEEDBACK, [{ text: prompt }]);
      return reply(200, { model: extractJson(text) });
    }

    return reply(404, { error: `Unknown route: ${path}` });
  } catch (err: any) {
    const msg = err?.message || String(err);
    // Surface actionable Bedrock errors (model access / region) to the UI.
    return reply(502, { error: `Bedrock call failed for '${modelId}': ${msg}` });
  }
};
