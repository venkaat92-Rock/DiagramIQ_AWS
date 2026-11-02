/**
 * The Celonis endpoint's front door, with enforcement switched on.
 *
 * Its own file, and its own process, because the handler reads REQUIRE_AUTH,
 * USER_POOL_ID and AUDIT_TABLE once at module load — the same reason the
 * deployed function has to be redeployed to change them, and the reason a
 * single test process cannot cover both states.
 *
 * This function is reachable at a Lambda Function URL with authType NONE: the
 * gate in the handler is the only thing in front of it. These checks are about
 * that gate, so they run before any Celonis configuration exists — nothing
 * here should get far enough to need it.
 */
import assert from 'node:assert/strict';
import { startTenant } from './celonis-stub.js';

delete process.env.HTTP_PROXY;
delete process.env.HTTPS_PROXY;
process.env.NO_PROXY = '127.0.0.1,localhost';
process.env.AWS_REGION = 'us-west-2';
process.env.AWS_ACCESS_KEY_ID = 'test';
process.env.AWS_SECRET_ACCESS_KEY = 'test';

// Enforcement on, and a pool to verify against.
process.env.REQUIRE_AUTH = 'true';
process.env.USER_POOL_ID = 'us-west-2_test';
process.env.USER_POOL_CLIENT_ID = 'testclient';

// A reachable tenant, so a 200 would be possible if the gate let anyone past.
const tenant = await startTenant();
process.env.CELONIS_TEAM_URL = tenant.teamUrl;
process.env.CELONIS_MCP_URL = tenant.mcpUrl;
process.env.CELONIS_CLIENT_ID = 'cid';
process.env.CELONIS_CLIENT_SECRET = 'shh';

const { handler } = await import('../amplify/functions/celonis/handler.js');

let failures = 0;
const lines: string[] = [];
function check(name: string, fn: () => void) {
  try { fn(); lines.push(`  ok   ${name}`); } catch (err) {
    failures += 1;
    lines.push(`  FAIL ${name}\n       ${err instanceof Error ? err.message : String(err)}`);
  }
}

const call = (path: string, headers: Record<string, string> = {}) => handler({
  rawPath: path,
  requestContext: { http: { method: 'POST' } },
  headers,
  body: JSON.stringify({ processName: 'Accounts Payable' }),
}) as Promise<{ statusCode: number; body: string }>;

/* ── no token, enforcement on ───────────────────────────────────────────────*/
{
  const r = await call('/celonis/discover');
  check('an unauthenticated mining run is refused', () => {
    assert.equal(r.statusCode, 401);
    assert.match(JSON.parse(r.body).error, /Sign in/);
  });
  check('and it is refused before Celonis is touched at all', () => {
    assert.equal(tenant.tokenRequests.length, 0,
      `the tenant was called ${tenant.tokenRequests.length} time(s)`);
  });
}

/* ── the status probe is not a way round the gate ───────────────────────────*/
{
  const r = await call('/celonis/status');
  check('the connection probe is behind the same gate', () => {
    assert.equal(r.statusCode, 401);
    assert.equal(tenant.tokenRequests.length, 0);
  });
}

/* ── a token that is not one of ours ────────────────────────────────────────*/
{
  const r = await call('/celonis/discover', { authorization: 'Bearer not.a.jwt' });
  check('a junk bearer token is refused, not ignored', () => {
    assert.equal(r.statusCode, 401);
    assert.match(JSON.parse(r.body).error, /session has expired|Sign in/);
  });
  check('a junk token does not reach Celonis either', () => {
    assert.equal(tenant.tokenRequests.length, 0);
  });
}

/* ── an unsigned token claiming to be an admin ──────────────────────────────*/
{
  const forged = `${Buffer.from(JSON.stringify({ alg: 'none' })).toString('base64url')}.`
    + `${Buffer.from(JSON.stringify({
        sub: 'sub-1', username: 'anna@example.com',
        'cognito:groups': ['superadmin'],
        token_use: 'access', client_id: 'testclient',
        exp: Math.floor(Date.now() / 1000) + 3600,
      })).toString('base64url')}.`;
  const r = await call('/celonis/discover', { authorization: `Bearer ${forged}` });
  check('an unsigned token is refused however well-formed its claims are', () => {
    assert.equal(r.statusCode, 401);
    assert.equal(tenant.tokenRequests.length, 0);
  });
}

await tenant.close();
console.log(lines.join('\n'));
console.log(failures ? `\n${failures} of ${lines.length} checks FAILED`
                     : `\n${lines.length} checks passed`);
process.exit(failures ? 1 : 0);
