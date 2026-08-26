// Drives the real frontend in Chromium against a mock of the engine, to prove
// the four requested changes actually work in the browser.
import { chromium } from 'playwright';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.join(path.dirname(new URL(import.meta.url).pathname), '..', 'frontend');
const PORT = 5205;
const ORIGIN = `http://localhost:${PORT}`;
const BPMN = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" targetNamespace="http://e.com">
<bpmn:process id="P1"><bpmn:task id="t1" name="Validate request"/></bpmn:process></bpmn:definitions>`;

const MODEL = {
  process_name: 'Procurement intake',
  lanes: ['Business', 'Procurement'],
  nodes: [
    { id: 's1', type: 'start', name: 'Request raised', lane: 'Business', col: 0, row: 0 },
    { id: 't1', type: 'task', name: 'Submit purchase request', lane: 'Business', col: 1, row: 0 },
    { id: 'g1', type: 'gateway', name: 'Approved?', lane: 'Procurement', col: 2, row: 0 },
    { id: 't2', type: 'task', name: 'Raise purchase order', lane: 'Procurement', col: 3, row: 0 },
    { id: 'e1', type: 'end', name: 'PO issued', lane: 'Procurement', col: 4, row: 0 },
  ],
  flows: [
    { from: 's1', to: 't1', label: '' }, { from: 't1', to: 'g1', label: '' },
    { from: 'g1', to: 't2', label: 'Yes' }, { from: 't2', to: 'e1', label: '' },
  ],
};

const UPLIFTED = BPMN.replace('Validate request', 'UPLIFT-MARKER task')
  .replace('</bpmn:definitions>', '<!--engine-only detail--></bpmn:definitions>');

const MODEL2 = { ...MODEL, process_name: 'Procurement intake (uplifted)' };

// The feedback pass renames one task and touches nothing else.
const MODEL3 = {
  ...MODEL,
  nodes: MODEL.nodes.map((n) => (n.id === 't1' ? { ...n, name: 'Raise purchase requisition' } : n)),
};

const DISCOVERY = {
  process_name: 'Procurement intake',
  trigger_event: 'Requester needs goods',
  successful_outcome: 'PO issued',
  unsuccessful_outcome: 'Request rejected',
  steps: [
    { activity: 'Submit purchase request', description: 'Raise in SAP', participant: 'Requester',
      it_systems: 'SAP', dependency: '', input_document: 'Req form', frequency: 'Daily' },
    { activity: 'Review request', description: 'Check budget', participant: 'Procurement',
      it_systems: 'SAP', dependency: 'Budget approval', templates: 'Checklist' },
  ],
};

// 45 rules: enough that a RULE_BATCH of 20 genuinely fans out (offsets 0, 20,
// 40) rather than fitting in the first slice, which would let a broken split
// pass unnoticed. The first three carry the verdicts the display asserts on.
const NAMED = [
  { id: 'AP-CONV-01', name: 'Pool carries the process name', category: 'structure', severity: 'must', kind: 'Modeling Convention' },
  { id: 'AP-CONV-02', name: 'Every task sits in a named lane', category: 'structure', severity: 'must', kind: 'Modeling Convention' },
  { id: 'AP-CONV-03', name: 'Message flows cross pools only', category: 'flow', severity: 'should', kind: 'Modeling Convention' },
];
const RULES = [
  ...NAMED,
  ...Array.from({ length: 42 }, (_, i) => ({
    id: `AP-CHK-${String(i + 1).padStart(2, '0')}`,
    name: `Checklist item ${i + 1}`, category: 'general', severity: '',
    kind: 'Self-Review Checklist',
  })),
];
const VERDICTS = Object.fromEntries(RULES.map((r) => [r.id, { status: 'Verified', notes: 'ok' }]));
VERDICTS['AP-CONV-02'] = { status: 'Not Verified', notes: 'lane missing' };
VERDICTS['AP-CONV-03'] = { status: 'Not Applicable', notes: 'no message flows' };
VERDICTS['AP-CONV-01'] = { status: 'Verified', notes: 'pool named' };

const XLSX_B64 = Buffer.from('fake-xlsx').toString('base64');
const seen = [];
const bodies = {};
const calls = {};
const requestedUrls = [];

const MOCK = {
  '/notes': { discovery: DISCOVERY, fileBase64: XLSX_B64, filename: 'Procurement.discovery.xlsx' },
  '/excel-to-discovery': { discovery: DISCOVERY },
  '/discovery-xlsx': { fileBase64: XLSX_B64, filename: 'Procurement.discovery.xlsx' },
  '/discovery-to-bpmn': { xml: BPMN, model: MODEL, fileBase64: XLSX_B64, filename: 'x.xlsx' },
  '/model': { model: MODEL },
  '/bpmn-to-excel': { fileBase64: XLSX_B64, filename: 'review.xlsx' },
  '/visio': { xml: BPMN, model: MODEL },
  '/uplift': { xml: UPLIFTED, model: MODEL2, log: 'ok' },
  '/convert': { model: MODEL },
  '/feedback': { model: MODEL },
  '/ai-modeller-inputs': { inputs: [
    { category: 'Missing decision criteria', element_id: 'g1', element_name: 'Approved?',
      what_missing: 'No threshold for who approves what.', why_it_matters: 'Routing is guesswork.',
      suggested_question: 'What spend limit sends a request to the category manager?' },
    { category: 'Missing system', element_id: 't2', element_name: 'Raise purchase order',
      what_missing: 'No IT system named.', why_it_matters: 'Cannot be automated as drawn.',
      suggested_question: 'Is the PO raised in SAP or Coupa?' },
  ] },
  '/ai-uplift': { xml: BPMN, model: MODEL, log: 'ok' },
  '/ai-layout': { xml: BPMN, model: MODEL },
  '/ai-naming': { fixes: {} },
  '/ai-gateways': { suggestions: [] },
  '/normalize': { xml: BPMN, model: MODEL },
  '/patch': { xml: BPMN, model: MODEL, changes: [{ type: 'rename' }] },
  '/uplift-report': { fileBase64: XLSX_B64, filename: 'uplift_report.xlsx' },
  '/validate': {
    issues: [
      { severity: 'ERROR', source: 'bpmn', rule_id: 3, element_id: 't1', element_name: 'Submit', message: 'No start event.' },
      { severity: 'WARNING', source: 'signavio', rule_id: 12, element_id: 't1', element_name: 'Submit', message: 'Verb-object naming.' },
    ],
    summary: { total: 2, errors: 1, warnings: 1, info: 0 },
  },
  // '/ai-compliance' is answered per slice, below.
};

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://x');
  requestedUrls.push(req.url);
  if (req.method === 'POST') {
    let raw = '';
    req.on('data', (c) => { raw += c; });
    req.on('end', () => {
      seen.push(url.pathname);
      bodies[url.pathname] = raw ? JSON.parse(raw) : {};
      (calls[url.pathname] = calls[url.pathname] || []).push(bodies[url.pathname]);
      let body = MOCK[url.pathname] ?? { error: `no mock for ${url.pathname}` };
      if (url.pathname === '/feedback') {
        const fb = String(bodies['/feedback']?.feedback || '');
        body = /line|frame|overlap/i.test(fb)
          // A drawing complaint: the model must come back untouched.
          ? { model: bodies['/feedback'].model, changes: [],
              notApplied: ['Connector routing is drawn by the layout engine, not held in the model.'] }
          : { model: MODEL3, changes: ['renamed t1'], notApplied: [] };
      }
      if (url.pathname === '/ai-compliance') {
        const { ruleOffset = 0, ruleLimit = 0 } = bodies['/ai-compliance'] || {};
        const slice = RULES.slice(ruleOffset, ruleLimit ? ruleOffset + ruleLimit : undefined);
        body = {
          results: Object.fromEntries(slice.map((r) => [r.id, VERDICTS[r.id]])),
          rules: slice,
          ruleTotal: RULES.length,
        };
      }
      if (url.pathname === '/patch') {
        body = { xml: UPLIFTED, model: MODEL2, changes: [{ type: 'rename' }, { type: 'lane' }] };
      }
      res.writeHead(body.error ? 404 : 200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(body));
    });
    return;
  }
  if (url.pathname === '/amplify_outputs.json') {
    res.writeHead(200, { 'content-type': 'application/json' });
    // Trailing slashes on purpose: Lambda Function URLs carry one, and the
    // frontend has to trim it or every path doubles its separator.
    return res.end(JSON.stringify({ custom: {
      diagramiqApiUrl: ORIGIN,
      diagramiqEngineUrl: `${ORIGIN}/`,
      diagramiqAiUrl: `${ORIGIN}/`,
    } }));
  }
  if (url.pathname === '/favicon.ico') { res.writeHead(204); return res.end(); }
  const file = path.join(ROOT, url.pathname === '/' ? 'index.html' : url.pathname);
  if (!fs.existsSync(file)) { res.writeHead(404); return res.end('nope'); }
  const ext = path.extname(file);
  res.writeHead(200, { 'content-type': { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' }[ext] || 'text/plain' });
  res.end(fs.readFileSync(file));
});
await new Promise((r) => server.listen(PORT, r));

const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' });
const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
const errors = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
const downloads = [];
page.on('download', (d) => downloads.push(d.suggestedFilename()));
await page.goto(`${ORIGIN}/`);
await page.waitForTimeout(400);

const R = [];
const ok = (name, cond, extra = '') => R.push([name, !!cond, extra]);
const status = () => page.textContent('#status');

ok('rollback disabled on a blank page', await page.isDisabled('#btnRollback'),
   await page.textContent('#btnRollback'));
ok('no checks strip before a diagram exists', !(await page.isVisible('#health')));

// ---- 1. Notes accepts Word ------------------------------------------------
ok('notes accepts .docx/.pdf',
   (await page.getAttribute('#notesInput', 'accept')).includes('.docx'),
   await page.getAttribute('#notesInput', 'accept'));

await page.setInputFiles('#notesInput', {
  name: 'meeting.docx',
  mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  buffer: Buffer.from('PK-fake-docx'),
});
await page.waitForTimeout(400);
ok('Word file sent to /notes as base64',
   bodies['/notes']?.filename === 'meeting.docx' && !!bodies['/notes']?.fileBase64);
ok('review popup opened', await page.isVisible('#reviewModal'));
ok('grid populated', (await page.locator('#revTable tbody tr').count()) === 2,
   `${await page.locator('#revTable tbody tr').count()} rows`);
ok('metadata populated', (await page.inputValue('#revName')) === 'Procurement intake');
ok('Export as Excel is top-right', await page.isVisible('#btnExportXlsx'));

// ---- 2. Edit in the popup, export, approve --------------------------------
await page.locator('#revTable tbody tr').first().locator('textarea').first().fill('Submit PR (edited)');
await page.click('#btnAddRow');
await page.waitForTimeout(150);
ok('add step works', (await page.locator('#revTable tbody tr').count()) === 3);

await page.click('#btnExportXlsx');
await page.waitForTimeout(350);
ok('export posts edited grid',
   bodies['/discovery-xlsx']?.discovery?.steps?.[0]?.activity === 'Submit PR (edited)',
   bodies['/discovery-xlsx']?.discovery?.steps?.[0]?.activity);
ok('untouched fields survive the round-trip',
   bodies['/discovery-xlsx']?.discovery?.steps?.[0]?.input_document === 'Req form');

await page.click('#btnApproveBuild');
await page.waitForTimeout(500);
ok('approve closes the popup', !(await page.isVisible('#reviewModal')));
ok('approve built BPMN', /BPMN built from/.test(await status()), (await status()).slice(0, 60));

// ---- 3. Preview renders for non-image inputs ------------------------------
const svgCount = await page.locator('#svgPane svg').count();
ok('preview drawn in "what the AI understood"', svgCount === 1, `${svgCount} svg`);
ok('stats line filled', /Understood:/.test(await page.textContent('#stats')));

await page.setInputFiles('#excelInput', { name: 'disc.xlsx', mimeType: 'application/vnd.ms-excel', buffer: Buffer.from('x') });
await page.waitForTimeout(400);
ok('Excel upload opens the grid', await page.isVisible('#reviewModal'));
ok('Excel upload hit /excel-to-discovery', seen.includes('/excel-to-discovery'));
await page.click('#btnReviewCancel');

await page.setInputFiles('#visioInput', { name: 'd.vsdx', mimeType: 'application/octet-stream', buffer: Buffer.from('x') });
await page.waitForTimeout(400);
ok('Visio also renders a preview', (await page.locator('#svgPane svg').count()) === 1);

// ---- 4. Reports in a popup with download ----------------------------------
await page.click('#btnValidate');
await page.waitForTimeout(400);
ok('validation opens a popup', await page.isVisible('#reportModal'));
ok('validation rows shown', (await page.locator('#reportTable tbody tr').count()) === 2);
ok('download button present', await page.isVisible('#btnReportDownload'));
await page.click('#btnReportDownload');
await page.waitForTimeout(350);
ok('validation downloads', downloads.some((f) => /validation/i.test(f)), downloads.join(','));
await page.click('#btnReportOk');

await page.click('#btnCompliance');
await page.waitForTimeout(900);
ok('review report opens as a popup', await page.isVisible('#insightModal'));
ok('checklist rows shown', (await page.locator('#checklistTable tbody tr').count()) === RULES.length);
await page.click('#btnInsightClose');

// ---- 5. Zoom -------------------------------------------------------------
const svgW = async () => Number((await page.getAttribute('#svgPane svg', 'style') || '')
  .match(/width:\s*([\d.]+)px/)?.[1] || 0);
const zlabel = () => page.textContent('#zoomLabel');

ok('zoom bar rendered', await page.isVisible('.zoombar'));
ok('starts fitted', /^Fit \d+%$/.test(await zlabel()), await zlabel());
const wFit = await svgW();
ok('svg sized in px, not stretched', wFit > 0, `${wFit}px`);

await page.click('#btnZoomIn');
const wIn = await svgW();
ok('zoom in enlarges', wIn > wFit, `${wFit} -> ${wIn}`);
ok('label leaves fit mode', !/Fit/.test(await zlabel()), await zlabel());

await page.click('#btnZoomOut');
ok('zoom out shrinks again', Math.abs((await svgW()) - wFit) < 2, `${await svgW()} vs ${wFit}`);

await page.click('#btnZoom100');
ok('1:1 is 100%', (await zlabel()) === '100%', await zlabel());
const w100 = await svgW();
ok('1:1 matches the viewBox width', Math.abs(w100 - 1090) < 400, `${w100}px`);

await page.click('#btnZoomFit');
ok('fit returns', /^Fit/.test(await zlabel()), await zlabel());
ok('overflow is pannable when zoomed', await page.evaluate(async () => {
  document.getElementById('btnZoomIn').click();
  document.getElementById('btnZoomIn').click();
  document.getElementById('btnZoomIn').click();
  return document.getElementById('svgPane').classList.contains('pannable');
}));
await page.click('#btnZoomFit');

// ---- 6. Checks run after the diagram is built -----------------------------
await page.waitForTimeout(300);
ok('checks strip visible', await page.isVisible('#health'));
const rules = await page.textContent('#btnHealthRules');
ok('BPMN rules ran unprompted', /1 error, 1 warning/.test(rules), rules);
ok('rules chip flags errors', (await page.getAttribute('#btnHealthRules', 'class')).includes('err'));
await page.click('#btnHealthRules');
await page.waitForTimeout(250);
ok('rules chip opens the report', await page.isVisible('#reportModal'));
await page.click('#btnReportOk');

// One audit run is one slice at offset 0, however many slices follow it.
const auditRuns = () => (calls['/ai-compliance'] || []).filter((c) => !c.ruleOffset).length;
ok('the audit never runs by itself — only the click so far', auditRuns() === 1,
   `${auditRuns()} runs`);
await page.click('#btnHealthQuality');
await page.waitForTimeout(900);
ok('the chip runs the review report', await page.isVisible('#insightModal'));
await page.click('#btnInsightOk');
const q1 = await page.textContent('#btnHealthQuality');
ok('the chip keeps the verdict', /43\/45 verified · 2 gaps/.test(q1), q1);
await page.click('#btnHealthQuality');
await page.waitForTimeout(300);
ok('the chip reopens it', await page.isVisible('#insightModal'));
await page.click('#btnInsightOk');

// ---- 7. Rollback ----------------------------------------------------------
const xmlText = () => page.inputValue('#xmlOut');
const beforeUplift = await xmlText();
const rbCount = async () =>
  Number((await page.textContent('#btnRollback')).match(/\((\d+)\)/)?.[1] || 0);
const n0 = await rbCount();

await page.click('#btnUplift');
await page.waitForTimeout(450);
const afterUplift = await xmlText();
ok("engine's own XML is kept, not regenerated",
   afterUplift.includes('UPLIFT-MARKER') && afterUplift.includes('engine-only detail'),
   afterUplift.slice(0, 60));
ok('rollback now offered', !(await page.isDisabled('#btnRollback')));
ok('rollback counts the step', (await rbCount()) === n0 + 1, `${n0} -> ${await rbCount()}`);
const qAfter = await page.textContent('#btnHealthQuality');
ok('a new diagram drops the old report', /checklist \+ what is still needed/.test(qAfter), qAfter);
ok('rollback names what it undoes',
   /Rule-based uplift/.test(await page.getAttribute('#btnRollback', 'title')),
   await page.getAttribute('#btnRollback', 'title'));

await page.click('#btnRollback');
await page.waitForTimeout(350);
ok('rollback restores the previous document', (await xmlText()) === beforeUplift);
ok('rollback pops one step', (await rbCount()) === n0, `${await rbCount()} vs ${n0}`);
ok('rollback says what it undid', /undid "Rule-based uplift"/.test(await status()),
   (await status()).slice(0, 70));
ok('preview still drawn after rollback', (await page.locator('#svgPane svg').count()) === 1);

// two steps back, in order
await page.click('#btnUplift');
await page.waitForTimeout(400);
await page.click('#btnAiLayout');
await page.waitForTimeout(400);
ok('history stacks', (await rbCount()) === n0 + 2, `${n0} -> ${await rbCount()}`);
await page.click('#btnRollback');
await page.waitForTimeout(300);
ok('first rollback undoes the layout pass', /undid "Layout cleanup"/.test(await status()));
await page.click('#btnRollback');
await page.waitForTimeout(300);
ok('second rollback undoes the uplift', /undid "Rule-based uplift"/.test(await status()));
ok('back to the approved version', (await xmlText()) === beforeUplift);

// feedback text rides along with the version
await page.fill('#feedback', 'lane is wrong');
await page.click('#btnUplift');
await page.waitForTimeout(400);
await page.fill('#feedback', 'something else');
await page.click('#btnRollback');
await page.waitForTimeout(300);
ok('rollback restores the feedback text too',
   (await page.inputValue('#feedback')) === 'lane is wrong',
   await page.inputValue('#feedback'));

// ---- 8. Image upload goes through the same human review -------------------
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
  'base64');
seen.length = 0;
await page.setInputFiles('#fileInput', { name: 'process.png', mimeType: 'image/png', buffer: PNG });
await page.waitForTimeout(350);
ok('image enables AI Convert', !(await page.isDisabled('#btnConvert')));
await page.click('#btnConvert');
await page.waitForTimeout(800);
ok('image extraction draws the diagram', (await page.locator('#svgPane svg').count()) === 1);
ok('image upload now opens a review sheet', await page.isVisible('#reviewModal'));
ok('review is built from the extracted diagram',
   seen.includes('/bpmn-to-excel') && seen.includes('/excel-to-discovery'));
ok('approve commits onto the diagram, not a rebuild',
   /apply to the diagram/.test(await page.textContent('#btnApproveBuild')),
   await page.textContent('#btnApproveBuild'));

// ---- 9. Minimise / maximise ----------------------------------------------
await page.click('#btnReviewMin');
await page.waitForTimeout(250);
ok('minimise docks the sheet', await page.evaluate(() =>
  document.getElementById('reviewModal').classList.contains('docked')));
ok('docked sheet is still visible', await page.isVisible('#reviewModal'));
ok('the diagram behind is interactive again', await page.evaluate(() => {
  // A modal dialog makes the rest of the page inert; a docked one must not.
  const before = document.getElementById('zoomLabel').textContent;
  document.getElementById('btnZoomIn').click();
  return document.getElementById('zoomLabel').textContent !== before;
}));
await page.click('#btnReviewMin');
await page.waitForTimeout(250);
ok('maximise restores the modal', await page.evaluate(() =>
  !document.getElementById('reviewModal').classList.contains('docked')
  && document.getElementById('reviewModal').open));

seen.length = 0;
await page.click('#btnApproveBuild');
await page.waitForTimeout(700);
ok('approve patches the existing diagram', seen.includes('/patch') && seen.includes('/discovery-xlsx'));
ok('patch keeps the engine XML', (await xmlText()).includes('engine-only detail'));
ok('patch reports its changes', /Applied 2 change/.test(await status()), await status());

// ---- 10. The two-tab review report ---------------------------------------
calls['/ai-compliance'] = [];          // count only this run's slices
await page.click('#btnCompliance');
await page.waitForTimeout(900);
ok('review report opens', await page.isVisible('#insightModal'));
ok('two tabs offered', (await page.locator('#insightModal .tab').count()) === 2);
ok('checklist tab shows every rule',
   (await page.locator('#checklistTable tbody tr').count()) === RULES.length,
   `${await page.locator('#checklistTable tbody tr').count()} of ${RULES.length}`);
ok('failures sort to the top',
   (await page.locator('#checklistTable tbody tr').first().getAttribute('data-ok')) === 'no');
ok('rule text is shown, not just the id',
   /Every task sits in a named lane/.test(await page.textContent('#checklistTable')));
ok('checklist tallies', /43 verified/.test(await page.textContent('#checklistSummary')),
   await page.textContent('#checklistSummary'));
ok('second tab starts hidden', await page.locator('#paneModeller').isHidden());

await page.click('#insightModal .tab[data-tab="modeller"]');
await page.waitForTimeout(200);
ok('gap tab shows', await page.locator('#paneModeller').isVisible());
ok('checklist tab hides', await page.locator('#paneChecklist').isHidden());
ok('gaps listed', (await page.locator('#modellerTable tbody tr').count()) === 2);
const gapRow = await page.textContent('#modellerTable tbody tr:first-child');
ok('gap says what is missing', /No threshold/.test(gapRow));
ok('gap says why it matters', /Routing is guesswork/.test(gapRow));
ok('gap gives the question for the business', /spend limit/.test(gapRow), gapRow.slice(-58));

seen.length = 0;
await page.click('#btnInsightDownload');
await page.waitForTimeout(500);
ok('report downloads as the two-sheet workbook', seen.includes('/uplift-report'));
ok('only the two sheets are asked for',
   JSON.stringify(bodies['/uplift-report']?.onlySheets) === '["checklist","modeller"]',
   JSON.stringify(bodies['/uplift-report']?.onlySheets));
ok('workbook carries both data sets',
   !!bodies['/uplift-report']?.complianceResults && !!bodies['/uplift-report']?.modellerInputs);

await page.click('#btnInsightMin');
await page.waitForTimeout(250);
ok('the report can be minimised over the diagram too', await page.evaluate(() =>
  document.getElementById('insightModal').classList.contains('docked')));
await page.click('#btnInsightClose');
await page.waitForTimeout(200);
ok('closing the docked report hides it', !(await page.isVisible('#insightModal')));

const cmpBefore = seen.filter((r) => r === '/ai-compliance').length;
await page.click('#btnHealthQuality');
await page.waitForTimeout(350);
const reopened = await page.isVisible('#insightModal');
const cmpAfter = seen.filter((r) => r === '/ai-compliance').length;
await page.click('#btnInsightOk');
ok('the chip reopens the report without re-running it', reopened && cmpAfter === cmpBefore,
   `${cmpBefore} -> ${cmpAfter}`);

// ---- 11. Re-do says what it actually did ----------------------------------
await page.fill('#feedback', 'rename Submit purchase request to Raise purchase requisition');
await page.click('#btnRedo');
await page.waitForTimeout(700);
ok('re-do reports what changed', await page.isVisible('#redoNote'));
const note1 = await page.textContent('#redoNote');
ok('the change is described from the actual diff', /renamed/.test(note1) && /requisition/.test(note1),
   note1.slice(0, 88));
ok('status counts the changes', /applied 1 change/i.test(await status()), await status());

await page.fill('#feedback', 'the lines are going outside the frame, fix that');
await page.click('#btnRedo');
await page.waitForTimeout(700);
const note2 = await page.textContent('#redoNote');
ok('a no-op re-do says so plainly', /Nothing in the diagram changed/.test(note2), note2.slice(0, 78));
ok('and explains why it was not applied', /layout engine/.test(note2), note2.slice(-78));
ok('a no-op re-do is flagged as a warning',
   (await page.getAttribute('#status', 'data-kind')) === 'warn');


// ---- 12. Long routes bypass the gateway ----------------------------------
ok('the engine Function URL is used, with its trailing slash trimmed',
   requestedUrls.some((u) => u === '/ai-modeller-inputs'),
   requestedUrls.filter((u) => u.includes('//')).join(',') || 'no doubled slashes');
ok('no path ever went out with a doubled slash',
   !requestedUrls.some((u) => u.startsWith('//')),
   requestedUrls.filter((u) => u.startsWith('//')).join(','));

// ---- 13. The audit is sliced ---------------------------------------------
const auditCalls = calls['/ai-compliance'] || [];
ok('45 rules went out as 3 slices of 20', auditCalls.length === 3,
   `${auditCalls.length} calls`);
ok('the slices cover the catalogue exactly once',
   JSON.stringify(auditCalls.map((c) => c.ruleOffset).sort((a, b) => a - b)) === '[0,20,40]',
   JSON.stringify(auditCalls.map((c) => c.ruleOffset)));
ok('every slice asks for the same batch size',
   auditCalls.every((c) => c.ruleLimit === 20));
ok('and every rule came back scored',
   (await page.locator('#checklistTable tbody tr').count()) === RULES.length);

// ---- 14. A stale input image is dropped ----------------------------------
seen.length = 0;
await page.setInputFiles('#fileInput', { name: 'process.png', mimeType: 'image/png', buffer: PNG });
await page.waitForTimeout(350);
ok('image pane shows the upload', await page.evaluate(() =>
  document.getElementById('imgPane').classList.contains('has-image')));

await page.setInputFiles('#notesInput', {
  name: 'transcript.docx',
  mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  buffer: Buffer.from('PK-fake-docx'),
});
await page.waitForTimeout(450);
ok('a transcription upload clears the stale image', await page.evaluate(() =>
  !document.getElementById('imgPane').classList.contains('has-image')));
ok('and the img element carries no src', await page.evaluate(() =>
  !document.getElementById('imgPreview').getAttribute('src')));
ok('the drop hint is back', await page.isVisible('#imgPane .hint'));
ok('AI Convert is disabled again with no image', await page.isDisabled('#btnConvert'));
await page.click('#btnReviewCancel');

await page.setInputFiles('#fileInput', { name: 'again.png', mimeType: 'image/png', buffer: PNG });
await page.waitForTimeout(300);
await page.setInputFiles('#excelInput', { name: 'd.xlsx', mimeType: 'application/vnd.ms-excel', buffer: Buffer.from('x') });
await page.waitForTimeout(400);
ok('an Excel upload clears it too', await page.evaluate(() =>
  !document.getElementById('imgPane').classList.contains('has-image')));
await page.click('#btnReviewCancel');

console.log('\n--- results ---');
let pass = true;
for (const [n, good, extra] of R) { pass &&= good; console.log(`${good ? 'PASS' : 'FAIL'}  ${n.padEnd(44)} ${extra}`); }
console.log('\nroutes hit:', [...new Set(seen)].sort().join(' '));
console.log('downloads:', downloads.join(', ') || 'none');
console.log('page errors:', errors.length ? errors : 'none');
console.log(pass && !errors.length ? '\nALL PASS' : '\nFAILURES');

await browser.close();
server.close();
