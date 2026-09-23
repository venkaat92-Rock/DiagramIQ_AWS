/* Rebuild docs/DiagramIQ-Overview.pdf from overview-deck.html.
 *
 *   cd docs && node render-deck.mjs
 *
 * Playwright is not a dependency of this project (see test/README.md); install
 * it where you run this: npm i --no-save playwright. The executablePath points
 * at the Chromium this environment already provides — drop it, or point it at
 * your own browser, if you have run `npx playwright install chromium`.
 *
 * The numbers in the deck all derive from four assumptions stated on its last
 * page. Change them there and re-run this; nothing else needs touching.
 */
import { chromium } from 'playwright';
const b = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const p = await b.newPage();
await p.goto('file://' + process.cwd() + '/overview-deck.html', { waitUntil: 'load' });
await p.pdf({ path: 'DiagramIQ-Overview.pdf', format: 'A4', printBackground: true,
              margin: { top: 0, bottom: 0, left: 0, right: 0 } });
await b.close();
console.log("wrote DiagramIQ-Overview.pdf");
