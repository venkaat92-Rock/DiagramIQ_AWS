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

## What it does not cover

Every `/…` call is answered by a mock in this file, so it proves the frontend's
half of each contract and nothing about Bedrock, the Python engine, or IAM.
A route whose shape changes server-side will keep passing here until the mock
is updated with it.
