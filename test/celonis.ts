/**
 * The Celonis path, end to end, with no Celonis tenant and no AWS account.
 *
 * Run: npm run test:celonis
 *
 * What is real here: the handler, the MCP client, the OAuth flow, the tool-use
 * loop, the read-only guard and the audit write. What is faked: the tenant on
 * the far side of the socket, and the two AWS services, reached through the
 * SDK's own endpoint environment variables so no client is patched.
 *
 * Every check below is one that has to hold for this feature to be safe or
 * useful — not one that merely passes.
 */
import assert from 'node:assert/strict';
import {
  ACTIVITIES, BLOCKED_TOOL, type Scripted,
  startBedrock, startDdb, startTenant,
} from './celonis-stub.js';

// Localhost only, and never through the session's outbound proxy.
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
// An SSM that holds nothing, for the "nobody has connected Celonis" case.
const ssm = await startDdb();
process.env.AWS_ENDPOINT_URL_SSM = ssm.url;

let failures = 0;
const checks: string[] = [];
function check(name: string, fn: () => void) {
  try {
    fn();
    checks.push(`  ok   ${name}`);
  } catch (err) {
    failures += 1;
    checks.push(`  FAIL ${name}\n       ${err instanceof Error ? err.message : String(err)}`);
  }
}

const FINAL_JSON = JSON.stringify({
  process_name: 'Accounts Payable',
  trigger_event: 'Invoice received',
  successful_outcome: 'Invoice paid',
  unsuccessful_outcome: '',
  steps: [
    { activity: 'Scan Invoice', description: 'Invoice is digitised',
      participant: 'AP Clerk', it_systems: 'SAP', input_document: '',
      output_document: '', templates: '', dependency: '',
      frequency: '12,480 cases', pain_points: '' },
    // Deliberately short of keys: the handler fills the rest rather than
    // handing the grid an object with holes in it.
    { activity: 'Match to PO', participant: 'AP Clerk', frequency: '11,920 cases' },
    { activity: 'Clarify with Supplier', participant: 'AP Clerk',
      frequency: '2,140 cases', dependency: 'PO mismatch',
      pain_points: '2,110 cases loop back to Match to PO' },
    { activity: 'Pay Invoice', participant: 'Treasury', frequency: '11,840 cases' },
    // No activity: not a step, and must not reach the grid as a blank row.
    { description: 'orphan', participant: 'nobody' },
  ],
  notes: ['Dropped the variant tail below 2% of cases.'],
});

type Reply = { statusCode: number; body: string };
const parse = (r: Reply) => JSON.parse(r.body);

function event(path: string, body: unknown) {
  return {
    rawPath: path,
    requestContext: { http: { method: 'POST' } },
    headers: {},
    body: JSON.stringify(body),
  };
}

/** Fresh handler state is not needed — the module caches nothing per request —
 *  but the Bedrock endpoint is read by the SDK at client construction, so the
 *  fake Bedrock has to exist before the first import. */
async function withBedrock(script: Scripted[]) {
  const fake = await startBedrock(script);
  process.env.AWS_ENDPOINT_URL_BEDROCK_RUNTIME = fake.url;
  return fake;
}

const tenant = await startTenant();
function connectEnv(t = tenant) {
  process.env.CELONIS_TEAM_URL = t.teamUrl;
  process.env.CELONIS_MCP_URL = t.mcpUrl;
  process.env.CELONIS_CLIENT_ID = 'cid';
  process.env.CELONIS_CLIENT_SECRET = 'shh';
}

// The fake Bedrock must be up before the handler module builds its client.
const bedrock = await withBedrock([
  { toolUse: { name: 'list_data_models', input: {} } },
  { toolUse: { name: 'get_activities', input: { dataModel: 'AP_Invoices_EU' } } },
  { toolUse: { name: 'get_directly_follows', input: { dataModel: 'AP_Invoices_EU' } } },
  { text: FINAL_JSON },
]);
const { handler } = await import('../amplify/functions/celonis/handler.js');

/* ── 1. Not connected is a setup message, not a failure ───────────────────── */
{
  for (const k of ['TEAM_URL', 'MCP_URL', 'CLIENT_ID', 'CLIENT_SECRET']) {
    delete process.env[`CELONIS_${k}`];
  }
  const r = await handler(event('/celonis/discover', { processName: 'AP' })) as Reply;
  const out = parse(r);
  check('an unconnected tenant answers 409, not 502', () => {
    assert.equal(r.statusCode, 409);
    assert.equal(out.connected, false);
  });
  check('the message names the parameters an admin has to set', () => {
    assert.match(out.error, /\/diagramiq\/celonis\/client_secret/);
    assert.match(out.error, /\/diagramiq\/celonis\/team_url/);
  });
}

/* ── 2. The status route: what this tenant published, and what was withheld ── */
connectEnv();
{
  const r = await handler(event('/celonis/status', {})) as Reply;
  const out = parse(r);
  check('status connects and lists the read-only tools', () => {
    assert.equal(r.statusCode, 200, r.body);
    const names = out.tools.map((t: { name: string }) => t.name);
    assert.deepEqual(names.sort(),
      ['get_activities', 'get_directly_follows', 'list_data_models']);
  });
  check('a tool that could change the tenant is withheld from the model', () => {
    assert.deepEqual(out.blocked, [BLOCKED_TOOL]);
  });
  check('tool descriptions come through, so a reviewer can see what was offered', () => {
    const t = out.tools.find((x: { name: string }) => x.name === 'get_activities');
    assert.match(t.description, /case counts/);
  });
}

/* ── 3. The mining run ───────────────────────────────────────────────────────*/
let discovered: any;
{
  const r = await handler(event('/celonis/discover', {
    processName: 'Accounts Payable',
    question: 'main path only',
    modelId: 'us.anthropic.claude-opus-4-5-20251101-v1:0',
  })) as Reply;
  discovered = parse(r);
  check('a mining run returns 200 and a discovery object', () => {
    assert.equal(r.statusCode, 200, r.body);
    assert.equal(discovered.discovery.process_name, 'Accounts Payable');
    assert.equal(discovered.discovery.trigger_event, 'Invoice received');
  });
  check('the steps are the mined activities, in order', () => {
    assert.deepEqual(discovered.discovery.steps.map((s: any) => s.activity),
      ['Scan Invoice', 'Match to PO', 'Clarify with Supplier', 'Pay Invoice']);
  });
  check('a step with no activity is dropped, not passed on as a blank row', () => {
    assert.equal(discovered.discovery.steps.length, 4);
  });
  check('every step carries all ten fields the review grid renders', () => {
    const fields = ['activity', 'description', 'participant', 'it_systems',
                    'input_document', 'output_document', 'templates',
                    'dependency', 'frequency', 'pain_points'];
    for (const s of discovered.discovery.steps) {
      assert.deepEqual(Object.keys(s).sort(), [...fields].sort());
      for (const f of fields) assert.equal(typeof s[f], 'string');
    }
  });
  check('the mined counts survive into the grid', () => {
    const byName = Object.fromEntries(
      discovered.discovery.steps.map((s: any) => [s.activity, s.frequency]));
    assert.equal(byName['Scan Invoice'], '12,480 cases');
    assert.equal(byName['Pay Invoice'], '11,840 cases');
  });
  check("the model's own notes reach the reviewer", () => {
    assert.ok(discovered.notes.some((n: string) => /variant tail/.test(n)),
      JSON.stringify(discovered.notes));
  });
  check('every tool call is reported as evidence', () => {
    assert.deepEqual(discovered.toolCalls.map((c: any) => c.tool),
      ['list_data_models', 'get_activities', 'get_directly_follows']);
    assert.ok(discovered.toolCalls.every((c: any) => c.ok));
  });
  check('the tool results actually came from the tenant', () => {
    // The third Converse call must carry the activity names the stub holds —
    // otherwise the loop fed the model nothing and the JSON above was invented.
    const sent = JSON.stringify(bedrock.calls[2].body);
    for (const a of ACTIVITIES.slice(0, 3)) assert.ok(sent.includes(a.activity), a.activity);
  });
  check('the requested model is the one called', () => {
    // An `every` over an empty list is true, which would make this check pass
    // while nothing had been called at all.
    assert.equal(bedrock.calls.length, 4);
    assert.ok(bedrock.calls.every(
      (c) => c.modelId === 'us.anthropic.claude-opus-4-5-20251101-v1:0'),
      bedrock.calls.map((c) => c.modelId).join(','));
  });
  check('the tools are offered to the model, the withheld one is not', () => {
    const cfg = bedrock.calls[0].body.toolConfig;
    const names = cfg.tools.map((t: any) => t.toolSpec.name);
    assert.deepEqual(names.sort(),
      ['get_activities', 'get_directly_follows', 'list_data_models']);
    assert.ok(!JSON.stringify(cfg).includes(BLOCKED_TOOL));
  });
  check("the tool's own JSON Schema is passed through, not reinvented", () => {
    const spec = bedrock.calls[0].body.toolConfig.tools
      .find((t: any) => t.toolSpec.name === 'get_activities').toolSpec;
    assert.deepEqual(spec.inputSchema.json.required, ['dataModel']);
  });
}

/* ── 4. The audit row, and what must never be in it ─────────────────────────*/
{
  const rows = ddb.writes
    .map((w) => w.Item)
    .filter((i) => i && i.action?.S === '/celonis/discover');
  check('the mining run is audited', () => {
    assert.ok(rows.length >= 1, `no audit row, saw ${ddb.writes.length} write(s)`);
  });
  check('the audit row never carries the mined process', () => {
    const text = JSON.stringify(rows);
    for (const a of ACTIVITIES) assert.ok(!text.includes(a.activity), a.activity);
    assert.ok(!text.includes('main path only'), 'the question text was stored');
  });
  check('the audit row records the request without its content', () => {
    const detail = JSON.stringify(rows.at(-1).detail);
    assert.match(detail, /questionChars/);
    assert.match(detail, /Accounts Payable/);     // the process name is metadata
    assert.match(JSON.stringify(rows.at(-1)), /"surface":\{"S":"celonis"\}/);
  });
}

/* ── 5. A tool error is information, not a dead end ─────────────────────────*/
{
  const fake = await withBedrock([
    { toolUse: { name: 'get_activities', input: { dataModel: 'Wrong_Model' } } },
    { toolUse: { name: 'get_activities', input: { dataModel: 'AP_Invoices_EU' } } },
    { text: FINAL_JSON },
  ]);
  const { handler: h } = await import(
    `../amplify/functions/celonis/handler.js?t=${Date.now()}`);
  const out = parse(await h(event('/celonis/discover', { processName: 'AP' })) as Reply);
  check('a failed tool call is recorded with the reason', () => {
    const bad = out.toolCalls.find((c: any) => !c.ok);
    assert.ok(bad, JSON.stringify(out.toolCalls));
    assert.match(bad.error, /No data model named 'Wrong_Model'/);
  });
  check("the tenant's error is fed back so the model can correct itself", () => {
    const sent = JSON.stringify(fake.calls[1].body);
    assert.match(sent, /No data model named/);
    assert.match(sent, /"status":"error"/);
  });
  check('the run still produces a usable process after the error', () => {
    assert.equal(out.discovery.steps.length, 4);
  });
  await fake.close();
}

/* ── 6. The read-only guard holds against the model, not just the tool list ─*/
{
  const fake = await withBedrock([
    { toolUse: { name: BLOCKED_TOOL, input: { flowId: 'f1' } } },
    { text: FINAL_JSON },
  ]);
  const { handler: h } = await import(
    `../amplify/functions/celonis/handler.js?t=${Date.now()}`);
  const out = parse(await h(event('/celonis/discover', { processName: 'AP' })) as Reply);
  check('a model asking for a write tool is refused by name', () => {
    const bad = out.toolCalls.find((c: any) => c.tool === BLOCKED_TOOL);
    assert.ok(bad && !bad.ok, JSON.stringify(out.toolCalls));
    assert.match(bad.error, /not a read-only tool/);
  });
  await fake.close();
}

/* ── 7. An unpublished tool is refused too ──────────────────────────────────*/
{
  const fake = await withBedrock([
    { toolUse: { name: 'read_everything', input: {} } },
    { text: FINAL_JSON },
  ]);
  const { handler: h } = await import(
    `../amplify/functions/celonis/handler.js?t=${Date.now()}`);
  const out = parse(await h(event('/celonis/discover', { processName: 'AP' })) as Reply);
  check('a tool the tenant never published is refused', () => {
    const bad = out.toolCalls.find((c: any) => c.tool === 'read_everything');
    assert.ok(bad && !bad.ok, JSON.stringify(out.toolCalls));
    assert.match(bad.error, /not one of the published tools/);
  });
  await fake.close();
}

/* ── 8. A loop that will not settle stops, and says so ──────────────────────*/
{
  const spin: Scripted[] = Array.from({ length: 12 },
    () => ({ toolUse: { name: 'list_data_models', input: {} } }));
  const fake = await withBedrock([...spin, { text: FINAL_JSON }]);
  const { handler: h } = await import(
    `../amplify/functions/celonis/handler.js?t=${Date.now()}`);
  const out = parse(await h(event('/celonis/discover', { processName: 'AP' })) as Reply);
  check('an endless exploration is cut off after a fixed number of rounds', () => {
    assert.equal(out.toolCalls.length, 12);
  });
  check('and the reviewer is told the answer may be incomplete', () => {
    assert.ok(out.notes.some((n: string) => /Stopped after 12 rounds/.test(n)),
      JSON.stringify(out.notes));
  });
  await fake.close();
}

/* ── 9. A tenant configured for client_secret_basic still works ─────────────*/
{
  const basicTenant = await startTenant({ rejectPost: true });
  connectEnv(basicTenant);
  const fake = await withBedrock([{ text: FINAL_JSON }]);
  const { handler: h } = await import(
    `../amplify/functions/celonis/handler.js?t=${Date.now()}`);
  const r = await h(event('/celonis/status', {})) as Reply;
  check('the OAuth fallback to client_secret_basic is exercised and works', () => {
    assert.equal(r.statusCode, 200, r.body);
    assert.equal(basicTenant.tokenRequests.length, 2);
    assert.ok(basicTenant.tokenRequests[0].form.client_secret, 'first try was not a post');
    assert.match(basicTenant.tokenRequests[1].auth, /^Basic /);
  });
  check('the client secret is never put in a URL or a query string', () => {
    assert.ok(basicTenant.tokenRequests.every((t) => !t.form.scope?.includes('shh')));
  });
  await fake.close();
  await basicTenant.close();
  connectEnv();
}

/* ── 10. Nothing asked, nothing mined ───────────────────────────────────────*/
{
  const r = await handler(event('/celonis/discover', {})) as Reply;
  check('a run with no process and no question is refused before any model call', () => {
    assert.equal(r.statusCode, 400);
    assert.match(parse(r).error, /Name the process/);
  });
}

/* ── 11. Unknown route ──────────────────────────────────────────────────────*/
{
  const r = await handler(event('/celonis/elsewhere', {})) as Reply;
  check('an unknown route is a 404', () => assert.equal(r.statusCode, 404));
}

await tenant.close();
await bedrock.close();
await ddb.close();
await ssm.close();

console.log(checks.join('\n'));
console.log(failures
  ? `\n${failures} of ${checks.length} checks FAILED`
  : `\n${checks.length} checks passed`);
process.exit(failures ? 1 : 0);
