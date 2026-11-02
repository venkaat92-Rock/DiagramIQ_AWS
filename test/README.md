# Browser test

`e2e.mjs` drives the real frontend in Chromium against a mock of the engine.
It asserts behaviour, not markup: file uploads reaching the right route, the
review grid round-tripping edits, reports rendering and downloading, zoom
arithmetic, rollback restoring earlier versions, and Re-do reporting what it
actually changed.

## Running it

Playwright is deliberately **not** a dependency of this project — adding it
would make every Amplify build download a browser it never uses. Install it
where you run the test:

```bash
npm i --no-save playwright
npx playwright install chromium     # skip if PLAYWRIGHT_BROWSERS_PATH is set
node test/e2e.mjs
```

It prints one line per assertion and ends with `ALL PASS` or `FAILURES`, and
exits after reporting either way — read the output, don't rely on the exit
code.

## The other two

`cors-parity.mjs` reads `amplify/backend.ts` and the two handlers. It asserts
what has to hold for a browser to reach either endpoint: the gateway allows
OPTIONS, the Function URL lists only methods `CreateFunctionUrlConfig` accepts,
and neither handler returns CORS headers of its own — a Function URL adds its
configured headers to whatever the function returns, and two of them is a
response the browser discards.

`route_split.py` runs the Python engine's handler in-process with Bedrock
stubbed, and asserts which prompt each upload route sends: `/notes` the
transcription pass at its 32000-token ceiling, `/sop` the document pass with
the Word file's tables and figures, each with the model chosen in the UI. It
needs the vendored dependencies (`amplify/functions/python-analyzer/vendor`)
and the sample SOP, both of which are in the repository:

```bash
python3 test/route_split.py
```

`auth.py` runs the engine handler with boto3 stubbed and asserts the policy of
the login layer: who is refused, what the refusal says, that a valid token is
resolved once and then cached, and — the assertion that matters most — that the
audit row keeps the filename and the size of an upload but never the document
text itself. It was confirmed to fail when enforcement is bypassed.

```bash
python3 test/auth.py
```

## Celonis

`celonis.ts` and `celonis-auth.ts` (run with `npm run test:celonis`) exercise
the Celonis path end-to-end with no Celonis tenant and no AWS account. The
handler, the MCP client, the OAuth flow, the tool-use loop and the audit write
are the real ones; what is faked is the tenant on the far side of the socket
and the two AWS services, reached through the SDK's own endpoint environment
variables (`AWS_ENDPOINT_URL_BEDROCK_RUNTIME`, `AWS_ENDPOINT_URL_DYNAMODB`) so
that no client is patched and no production code knows it is under test.
`celonis-stub.ts` holds the fakes — note the Bedrock one speaks HTTP/2, because
the Bedrock Runtime client does and an HTTP/1.1 stub fails with a bare
"Protocol error" before the request is even sent.

The checks that matter most:

- a tool whose name suggests it writes is withheld from the model, **and**
  refused if the model names it anyway;
- a tool the tenant never published is refused;
- the tenant's error text is fed back so the model can correct itself;
- an exploration that will not settle is cut off, and the reviewer is told;
- the audit row carries who asked and for which process, and never the mined
  activities or the reviewer's question;
- with `REQUIRE_AUTH=true`, both routes refuse before Celonis is touched at all
  — including an unsigned token with well-formed admin claims.

Each one was confirmed to fail when the thing it guards was deliberately
broken. The browser half is in `e2e.mjs`, against mocks mounted under their own
path prefix so the test can prove `/celonis/*` goes to the Celonis Function URL
and not the engine's.

What none of this covers: the tool names and result shapes of a **real** Agent
Tools asset. No live tenant was available, which is why nothing in the code
depends on them.

## What it does not cover

Every `/…` call is answered by a mock in this file, so it proves the frontend's
half of each contract and nothing about Bedrock, the Python engine, or IAM.
A route whose shape changes server-side will keep passing here until the mock
is updated with it.
