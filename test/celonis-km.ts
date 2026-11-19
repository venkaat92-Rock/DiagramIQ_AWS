/**
 * The second door: the Knowledge Model REST API, wrapped as the same four
 * tools an Agent Tools asset publishes.
 *
 * This is the path for a tenant that has a knowledge model but no published
 * MCP asset — which is most of them, since publishing one is a Studio task
 * needing permissions the person asking for a diagram may not have.
 *
 * Run: npm run test:celonis
 *
 * What is real: the handler, the adapter, the OAuth handshake, the tool-use
 * loop, the limits and the read-only guard. What is faked: the tenant, and
 * Bedrock, reached through the SDK's own endpoint environment variable.
 */
import assert from 'node:assert/strict';
import {
  ACTIVITIES, type Scripted, startBedrock, startDdb, startTenant,
} from './celonis-stub.js';

delete process.env.HTTP_PROXY;
delete process.env.HTTPS_PROXY;
delete process.env.http_proxy;
delete process.env.https_proxy;
process.env.NO_PROXY = '127.0.0.1,localhost';
process.env.AWS_REGION = 'us-west-2';
process.env.AWS_ACCESS_KEY_ID = 'test';
process.env.AWS_SECRET_ACCESS_KEY = 'test';
process.env.AUDIT_TABLE = 'diagramiq-audit-log';
process.env.CELONIS_SSM_PREFIX = '/diagramiq/celonis';

const ddb = await startDdb();
process.env.AWS_ENDPOINT_URL_DYNAMODB = ddb.url;
const ssm = await startDdb();            // a Parameter Store that holds nothing
process.env.AWS_ENDPOINT_URL_SSM = ssm.url;

let failures = 0;
const lines: string[] = [];
function check(name: string, fn: () => void) {
  try { fn(); lines.push(`  ok   ${name}`); } catch (err) {
    failures += 1;
    lines.push(`  FAIL ${name}\n       ${err instanceof Error ? err.message : String(err)}`);
  }
}

const tenant = await startTenant();

function envFor(source: 'km' | 'mcp' | 'auto', opts: { kmId?: string | null } = {}) {
  for (const k of ['SOURCE', 'TEAM_URL', 'MCP_URL', 'KM_BASE_URL', 'KM_ID',
                   'CLIENT_ID', 'CLIENT_SECRET']) {
    delete process.env[`CELONIS_${k}`];
  }
  process.env.CELONIS_SOURCE = source;
  process.env.CELONIS_TEAM_URL = tenant.teamUrl;
  process.env.CELONIS_CLIENT_ID = 'cid';
  process.env.CELONIS_CLIENT_SECRET = 'shh';
  if (source !== 'km') process.env.CELONIS_MCP_URL = tenant.mcpUrl;
  process.env.CELONIS_KM_BASE_URL = tenant.kmBaseUrl;
  const kmId = opts.kmId === undefined ? tenant.kmId : opts.kmId;
  if (kmId) process.env.CELONIS_KM_ID = kmId;
}

const FINAL_JSON = JSON.stringify({
  process_name: 'Accounts Payable',
  trigger_event: 'Create Invoice',
  successful_outcome: 'Pay Invoice',
  unsuccessful_outcome: '',
  steps: ACTIVITIES.slice(0, 4).map((a) => ({
    activity: a.activity, description: 'from the knowledge model',
    participant: a.resource, it_systems: '', input_document: '',
    output_document: '', templates: '', dependency: '',
    frequency: `${a.cases.toLocaleString('en-US')} cases`, pain_points: '',
  })),
  notes: ['Read the AP_INVOICE record of the knowledge model.'],
});

type Reply = { statusCode: number; body: string };
const parse = (r: Reply) => JSON.parse(r.body);
const event = (path: string, body: unknown) => ({
  rawPath: path,
  requestContext: { http: { method: 'POST' } },
  headers: {},
  body: JSON.stringify(body),
});

let importSeq = 0;
async function load(script: Scripted[]) {
  const fake = await startBedrock(script);
  process.env.AWS_ENDPOINT_URL_BEDROCK_RUNTIME = fake.url;
  importSeq += 1;
  const mod = await import(
    `../amplify/functions/celonis/handler.js?km=${importSeq}-${Date.now()}`);
  return { fake, handler: mod.handler as (e: unknown) => Promise<Reply> };
}

/* ── 1. The four tools, over a tenant with no MCP asset ─────────────────────*/
envFor('km');
{
  const { fake, handler } = await load([{ text: FINAL_JSON }]);
  const out = parse(await handler(event('/celonis/status', {})));
  check('a knowledge model opens without any Agent Tools asset', () => {
    assert.equal(out.connected, true, JSON.stringify(out));
    assert.equal(out.source, 'km');
  });
  check('it publishes exactly the four tools an asset would', () => {
    assert.deepEqual(out.tools.map((t: { name: string }) => t.name).sort(),
      ['get_knowledge_model', 'load_data', 'search_data', 'search_metadata']);
  });
  check('and says which knowledge model it opened', () => {
    assert.match(out.sourceLabel, new RegExp(tenant.kmId));
  });
  check('the Knowledge Model API gets its own scope, not the MCP one', () => {
    const scopes = tenant.tokenRequests.map((t) => t.form.scope);
    assert.ok(scopes.some((s) => s?.includes('knowledge-models')), scopes.join('|'));
    assert.ok(!scopes.some((s) => s?.includes('mcp-asset')), scopes.join('|'));
  });
  await fake.close();
}

/* ── 2. What each tool actually does ────────────────────────────────────────*/
{
  const { fake, handler } = await load([
    { toolUse: { name: 'get_knowledge_model', input: {} } },
    { toolUse: { name: 'search_metadata', input: { query: 'activity' } } },
    { toolUse: { name: 'load_data',
                 input: { columns: ['AP_INVOICE.activity', 'ap_invoice_count'],
                          order_by: 'ap_invoice_count', ascending: false, limit: 20 } } },
    { toolUse: { name: 'search_data',
                 input: { query: 'invoice', attribute: 'AP_INVOICE.activity' } } },
    { text: FINAL_JSON },
  ]);
  tenant.kmRequests.length = 0;
  const out = parse(await handler(event('/celonis/discover',
    { processName: 'Accounts Payable' })));

  check('all four tool calls succeed against the REST API', () => {
    assert.equal(out.toolCalls.length, 4, JSON.stringify(out.toolCalls));
    const bad = out.toolCalls.filter((c: { ok: boolean }) => !c.ok);
    assert.equal(bad.length, 0, JSON.stringify(bad));
  });
  check('get_knowledge_model returns the records WITH their attributes', () => {
    // Without the attributes the model cannot form RECORD.field, so load_data
    // is unusable and the run dies three calls later for no visible reason.
    const sent = JSON.stringify(fake.calls[1].body);
    assert.match(sent, /AP_INVOICE/);
    assert.match(sent, /activity_index/);
    assert.match(sent, /ap_touchless_rate/);
  });
  check('search_metadata finds a column by name and gives its RECORD.field', () => {
    const sent = JSON.stringify(fake.calls[2].body);
    assert.match(sent, /AP_INVOICE\.activity/);
  });
  check('load_data splits KPIs into kpis= and attributes into fields=', () => {
    const call = tenant.kmRequests.find((u) => u.includes('/data?'));
    assert.ok(call, tenant.kmRequests.join('\n'));
    const q = new URL(call!, 'http://x').searchParams;
    assert.equal(q.get('kpis'), 'ap_invoice_count');
    assert.equal(q.get('fields'), 'AP_INVOICE.activity');
  });
  check('load_data passes the sort through', () => {
    const q = new URL(tenant.kmRequests.find((u) => u.includes('/data?'))!, 'http://x')
      .searchParams;
    assert.equal(q.get('sort'), 'ap_invoice_count');
    assert.equal(q.get('order'), 'desc');
  });
  check('the activities reach the model', () => {
    const sent = JSON.stringify(fake.calls[3].body);
    for (const a of ACTIVITIES.slice(0, 3)) assert.ok(sent.includes(a.activity), a.activity);
  });
  check('search_data returns the matching values, and only those', () => {
    const result = fake.calls[4].body.messages.at(-1)
      .content[0].toolResult.content[0].text;
    const { values } = JSON.parse(result);
    // Every activity with "invoice" in it, and none without — the query is a
    // filter, not decoration.
    assert.deepEqual(values, ACTIVITIES.map((a) => a.activity)
      .filter((a) => a.toLowerCase().includes('invoice')));
    assert.ok(!values.includes('Match to PO'), values.join('|'));
  });
  check('the run produces a process definition from the knowledge model', () => {
    assert.equal(out.discovery.steps.length, 4);
    assert.equal(out.source, 'km');
  });
  check("the tenant's own activity names are what come back", () => {
    assert.deepEqual(out.discovery.steps.map((s: { activity: string }) => s.activity),
      ACTIVITIES.slice(0, 4).map((a) => a.activity));
  });
  await fake.close();
}

/* ── 3. The API's published limits are respected, not discovered ────────────*/
{
  const { fake, handler } = await load([
    { toolUse: { name: 'load_data',
                 input: { columns: ['AP_INVOICE.activity'], limit: 500 } } },
    { toolUse: { name: 'load_data',
                 input: { columns: ['AP_INVOICE.activity'], limit: 99999 } } },
    { text: FINAL_JSON },
  ]);
  tenant.kmRequests.length = 0;
  const out = parse(await handler(event('/celonis/discover', { processName: 'AP' })));
  check('a page bigger than the API allows is clamped, not sent', () => {
    const q = new URL(tenant.kmRequests.find((u) => u.includes('/data?'))!, 'http://x')
      .searchParams;
    assert.equal(q.get('limit'), '50');
  });
  check('a request beyond the total row ceiling is refused with the reason', () => {
    const bad = out.toolCalls.find((c: { ok: boolean }) => !c.ok);
    assert.ok(bad, JSON.stringify(out.toolCalls));
    assert.match(bad.error, /at most 50 rows per call \(5000 in all\)/);
  });
  await fake.close();
}

/* ── 4. A wrong column is the tenant's error, handed back verbatim ──────────*/
{
  const { fake, handler } = await load([
    { toolUse: { name: 'load_data', input: { columns: ['AP_INVOICE.not_a_field'] } } },
    { text: FINAL_JSON },
  ]);
  const out = parse(await handler(event('/celonis/discover', { processName: 'AP' })));
  check('an unknown column comes back with the name the tenant rejected', () => {
    const bad = out.toolCalls.find((c: { ok: boolean }) => !c.ok);
    assert.ok(bad, JSON.stringify(out.toolCalls));
    assert.match(bad.error, /not_a_field/);
  });
  check('and the model gets it, so it can correct itself', () => {
    assert.match(JSON.stringify(fake.calls[1].body), /not_a_field/);
  });
  await fake.close();
}

/* ── 5. Misconfiguration says what to set, not "something went wrong" ───────*/
{
  envFor('km', { kmId: 'no-such-model' });
  const { fake, handler } = await load([{ text: FINAL_JSON }]);
  const r = await handler(event('/celonis/status', {}));
  check('a knowledge model id that does not exist names the id', () => {
    assert.equal(r.statusCode, 502, r.body);
    assert.match(parse(r).error, /no-such-model/);
  });
  await fake.close();
}
{
  envFor('km', { kmId: null });
  const { fake, handler } = await load([{ text: FINAL_JSON }]);
  const r = await handler(event('/celonis/status', {}));
  check('asking for the knowledge model door without an id names the parameter', () => {
    assert.equal(r.statusCode, 409, r.body);
    assert.match(parse(r).error, /\/diagramiq\/celonis\/km_id/);
  });
  await fake.close();
}
{
  envFor('auto', { kmId: null });
  delete process.env.CELONIS_MCP_URL;
  const { fake, handler } = await load([{ text: FINAL_JSON }]);
  const r = await handler(event('/celonis/status', {}));
  check('with neither door configured, both parameters are offered', () => {
    assert.equal(r.statusCode, 409, r.body);
    assert.match(parse(r).error, /\/diagramiq\/celonis\/mcp_url/);
    assert.match(parse(r).error, /\/diagramiq\/celonis\/km_id/);
  });
  await fake.close();
}

/* ── 6. Choosing a door ─────────────────────────────────────────────────────*/
{
  envFor('auto');
  const { fake, handler } = await load([{ text: FINAL_JSON }]);
  const out = parse(await handler(event('/celonis/status', {})));
  check('with both configured, auto takes the Agent Tools asset', () => {
    // An asset is a curated set of tools, chosen by someone who knows the
    // tenant. Where one exists it is the better read.
    assert.equal(out.source, 'mcp', JSON.stringify(out));
  });
  await fake.close();
}
{
  envFor('mcp');
  process.env.CELONIS_MCP_URL = `${tenant.teamUrl}/no-asset-published-here`;
  process.env.CELONIS_KM_ID = tenant.kmId;
  const { fake, handler } = await load([{ text: FINAL_JSON }]);
  const r = await handler(event('/celonis/status', {}));
  check('a failing MCP asset does NOT silently fall through to the REST API', () => {
    // The two read different things. Swapping them without saying so would
    // change the answer and hide the reason the asset failed.
    assert.equal(r.statusCode, 502, r.body);
    assert.ok(!/knowledge model/i.test(parse(r).sourceLabel ?? ''), r.body);
  });
  await fake.close();
}

/* ── 7. The audit row ───────────────────────────────────────────────────────*/
{
  const rows = ddb.writes.map((w) => w.Item)
    .filter((i) => i && i.action?.S === '/celonis/discover');
  check('the audit row records which door answered', () => {
    assert.ok(rows.length, 'no audit rows');
    const detail = JSON.stringify(rows.map((r) => r.detail));
    assert.match(detail, /"km"/);
  });
  check('and still never the data that was read', () => {
    const text = JSON.stringify(rows);
    for (const a of ACTIVITIES) assert.ok(!text.includes(a.activity), a.activity);
  });
}

await tenant.close();
await ddb.close();
await ssm.close();

console.log(lines.join('\n'));
console.log(failures ? `\n${failures} of ${lines.length} checks FAILED`
                     : `\n${lines.length} checks passed`);
process.exit(failures ? 1 : 0);
