// The two endpoints must declare the same CORS methods. They diverged once,
// silently, and the browser could only reach one of them.
import fs from 'node:fs';
const src = fs.readFileSync('amplify/backend.ts', 'utf8');

const gateway = /allowMethods:\s*\[([^\]]+)\]/.exec(src)?.[1] || '';
const fnUrl = /allowedMethods:\s*\[([^\]]+)\]/.exec(src)?.[1] || '';
const methods = (s) => [...s.matchAll(/\.(\w+)/g)].map((m) => m[1]).sort();

const g = methods(gateway), f = methods(fnUrl);
console.log('gateway     :', g.join(', '));
console.log('function url:', f.join(', '));

let ok = true;
const check = (name, cond) => { ok &&= cond; console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}`); };
check('both allow POST', g.includes('POST') && f.includes('POST'));
check('both allow OPTIONS — the preflight the browser always sends',
      g.includes('OPTIONS') && f.includes('OPTIONS'));
check('the two endpoints agree', JSON.stringify(g) === JSON.stringify(f));
console.log(ok ? '\nALL PASS' : '\nFAILURES');
process.exitCode = ok ? 0 : 1;
