// The two endpoints must both accept the browser's POST — and they say so in
// different words, because they answer the preflight in different places.
//
//   HTTP API      the gateway answers OPTIONS only for methods it was told to
//                 allow, so OPTIONS must be listed.
//   Function URL  the Lambda service answers the preflight itself, and
//                 Cors.AllowMethods describes the actual request. Its valid
//                 values are GET | PUT | HEAD | POST | PATCH | DELETE | *.
//                 OPTIONS is rejected by CreateFunctionUrlConfig, so listing
//                 it does not loosen or tighten CORS — it fails the deploy,
//                 and the site keeps serving the previous build.
//
// The first version of this test asserted the opposite — that both must list
// OPTIONS — and passed, because it only read the file it was checking against
// the belief it was written from. It is the deployability of the Function URL
// value that has to be asserted, not its symmetry with a different service.
import fs from 'node:fs';
const src = fs.readFileSync('amplify/backend.ts', 'utf8');

const gateway = /allowMethods:\s*\[([^\]]+)\]/.exec(src)?.[1] || '';
const fnUrl = /allowedMethods:\s*\[([^\]]+)\]/.exec(src)?.[1] || '';
const methods = (s) => [...s.matchAll(/\.(\w+)/g)].map((m) => m[1]).sort();

// What the Lambda API accepts in Cors.AllowMethods. ALL is CDK's '*'.
const FN_URL_VALID = new Set(['GET', 'PUT', 'HEAD', 'POST', 'PATCH', 'DELETE', 'ALL']);

const g = methods(gateway), f = methods(fnUrl);
console.log('gateway     :', g.join(', ') || '(none)');
console.log('function url:', f.join(', ') || '(none)');

let ok = true;
const check = (name, cond) => { ok &&= cond; console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}`); };

check('both endpoints accept the POST the app sends',
      g.includes('POST') && (f.includes('POST') || f.includes('ALL')));
check('the gateway allows OPTIONS — it answers the preflight itself',
      g.includes('OPTIONS'));
check('the function URL lists only values Cors.AllowMethods accepts — '
      + 'OPTIONS there fails the deploy',
      f.length > 0 && f.every((m) => FN_URL_VALID.has(m)));

console.log(ok ? '\nALL PASS' : '\nFAILURES');
process.exitCode = ok ? 0 : 1;
