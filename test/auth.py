"""The login layer, exercised without AWS.

boto3 is stubbed, so what these assertions check is the policy: who is let in,
who is refused, what the refusal says, and what ends up in the audit row —
including the thing that must never end up there, which is the document.

Run:  python3 test/auth.py     (from the repository root)
"""
import base64
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'amplify/functions/python-analyzer'
sys.path[:0] = [str(ROOT), str(ROOT / 'vendor')]

fails = []


def ok(name, cond, detail=''):
    print(('PASS  ' if cond else 'FAIL  ') + name + (f'  — {detail}' if detail and not cond else ''))
    if not cond:
        fails.append(name)


# ── a fake Cognito and a fake DynamoDB ───────────────────────────────────────
class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {'Error': {'Code': code}}


calls = {'get_user': 0}
KNOWN = {
    'good-token': {
        'Username': 'anna@example.com',
        'UserAttributes': [{'Name': 'sub', 'Value': 'sub-anna'},
                           {'Name': 'email', 'Value': 'anna@example.com'}],
    },
}
written = []


class FakeTable:
    def put_item(self, Item):
        written.append(Item)


class FakeCognito:
    def get_user(self, AccessToken):
        calls['get_user'] += 1
        if AccessToken not in KNOWN:
            raise FakeClientError('NotAuthorizedException')
        return KNOWN[AccessToken]


fake_boto3 = types.ModuleType('boto3')
fake_boto3.client = lambda name, **kw: FakeCognito()
fake_boto3.resource = lambda name, **kw: types.SimpleNamespace(Table=lambda t: FakeTable())
sys.modules['boto3'] = fake_boto3
botocore = types.ModuleType('botocore')
exceptions = types.ModuleType('botocore.exceptions')
exceptions.ClientError = FakeClientError
botocore.exceptions = exceptions
sys.modules['botocore'] = botocore
sys.modules['botocore.exceptions'] = exceptions

import os                                                        # noqa: E402
os.environ['AUDIT_TABLE'] = 'test-audit'

from diagramiq import audit                                      # noqa: E402
from diagramiq import identity as ident                          # noqa: E402

audit.TABLE = 'test-audit'


def event(token=None, path='/validate', body=None):
    headers = {'authorization': f'Bearer {token}'} if token else {}
    return {'rawPath': path, 'headers': headers, 'body': json.dumps(body or {})}


# ── enforcement off: anonymous is allowed through ────────────────────────────
os.environ['REQUIRE_AUTH'] = 'false'
ident._cache.clear()
who = ident.identify(event())
ok('with enforcement off, a request with no token runs as anonymous', who.sub == 'anonymous')

# ── enforcement on: no token is refused ──────────────────────────────────────
os.environ['REQUIRE_AUTH'] = 'true'
ident._cache.clear()
try:
    ident.identify(event())
    ok('with enforcement on, a request with no token is refused', False, 'it was allowed')
except ident.NotAuthorised as err:
    ok('with enforcement on, a request with no token is refused', err.status == 401)
    ok('and the refusal tells the person what to do', 'Sign in' in str(err), str(err))

# ── a token that does not verify is refused either way ───────────────────────
for mode in ('true', 'false'):
    os.environ['REQUIRE_AUTH'] = mode
    ident._cache.clear()
    try:
        ident.identify(event('forged-token'))
        ok(f'a bad token is refused with enforcement {mode}', False, 'it was allowed')
    except ident.NotAuthorised as err:
        ok(f'a bad token is refused with enforcement {mode}', err.status == 401)

# ── a good token identifies the person ───────────────────────────────────────
os.environ['REQUIRE_AUTH'] = 'true'
ident._cache.clear()
calls['get_user'] = 0
who = ident.identify(event('good-token'))
ok('a valid token resolves to the person who owns it',
   who.sub == 'sub-anna' and who.email == 'anna@example.com', f'{who}')
ident.identify(event('good-token'))
ident.identify(event('good-token'))
ok('and the answer is cached, not re-asked on every request', calls['get_user'] == 1,
   f"{calls['get_user']} calls to Cognito")

# ── the audit row: enough to hold someone to account, and no more ────────────
written.clear()
audit._table = FakeTable()
body = {'filename': 'SOP-PR-014.docx', 'processName': 'Procurement',
        'fileBase64': base64.b64encode(b'x' * 5000).decode(),
        'text': 'the entire confidential document text'}
detail = audit.summarise(body)
audit.record(who, '/sop', 200, 0.0, detail=detail)
row = written[-1] if written else {}
ok('an audit row is written', bool(row))
ok('it names the person', row.get('actor') == 'anna@example.com' and row.get('actorSub') == 'sub-anna')
ok('it names the route and the outcome', row.get('action') == '/sop' and row.get('status') == 200)
ok('it keeps the filename', row.get('detail', {}).get('filename') == 'SOP-PR-014.docx')
ok('it keeps the size of what was uploaded', row.get('detail', {}).get('fileBase64Bytes', 0) > 5000)
blob = json.dumps(row)
ok('it does NOT keep the document itself',
   'confidential document text' not in blob and base64.b64encode(b'x' * 5000).decode() not in blob)
ok('it expires on its own', isinstance(row.get('expiresAt'), int) and row['expiresAt'] > 0)

# ── a failed audit write never breaks the request ────────────────────────────
class ExplodingTable:
    def put_item(self, Item):
        raise RuntimeError('DynamoDB is having a day')


audit._table = ExplodingTable()
try:
    audit.record(who, '/validate', 200, 0.0)
    ok('a failed audit write does not break the request it describes', True)
except Exception as err:                                          # noqa: BLE001
    ok('a failed audit write does not break the request it describes', False, str(err))

# ── the handler refuses before it does any work ──────────────────────────────
audit._table = FakeTable()
written.clear()
os.environ['REQUIRE_AUTH'] = 'true'
ident._cache.clear()
import index                                                      # noqa: E402
res = index.handler(event(path='/validate', body={'xml': '<x/>'}), None)
ok('the handler refuses an unauthenticated request', res['statusCode'] == 401, res)
ok('and logs the refusal', any(r.get('status') == 401 for r in written), written)
ok('the refusal carries no CORS headers of its own',
   not any(k.lower().startswith('access-control') for k in res['headers']))

print('\n' + ('ALL PASS' if not fails else f'{len(fails)} FAILURE(S)'))
sys.exit(1 if fails else 0)
