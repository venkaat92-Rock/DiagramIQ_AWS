/* DiagramIQ AWS — app logic. */
import { buildBpmn, layoutModel } from './bpmnBuilder.js';
import { renderSvg } from './svgPreview.js';
import { createZoom } from './zoom.js';

const $ = (id) => document.getElementById(id);
let zoom;                       // preview zoom/pan controller, built on load
const state = {
  apiUrl: '', model: null, xml: '', imageB64: '', mediaType: 'image/png',
  // Engine state. `xml` is the working BPMN once anything produces one;
  // `changes` accumulates the patcher's change log for the uplift report.
  reviewXlsx: '', changes: [], discovery: null, lastReport: null,
  // `history` is the rollback stack: each entry holds the whole working
  // document as it stood *before* the step named in its label.
  history: [], validation: null, complianceReport: null,
};

/* Model picker: dropdown of verified models + free-text custom ID. */
function currentModelId() {
  const sel = $('modelSelect').value;
  if (sel === '__custom__') return $('modelId').value.trim() || undefined;
  return sel;
}

function initModelPicker() {
  const sel = $('modelSelect');
  const custom = $('modelId');
  const saved = localStorage.getItem('diagramiq.model');
  if (saved) {
    const opt = [...sel.options].find((o) => o.value === saved);
    if (opt) { sel.value = saved; }
    else { sel.value = '__custom__'; custom.value = saved; custom.classList.remove('hidden'); }
  }
  sel.addEventListener('change', () => {
    custom.classList.toggle('hidden', sel.value !== '__custom__');
    if (sel.value !== '__custom__') localStorage.setItem('diagramiq.model', sel.value);
    else custom.focus();
  });
  custom.addEventListener('change', () => {
    if (custom.value.trim()) localStorage.setItem('diagramiq.model', custom.value.trim());
  });
}

function setStatus(msg, kind = 'info') {
  const el = $('status');
  el.textContent = msg;
  el.dataset.kind = kind;
}

const ENGINE_BTNS = ['btnValidate', 'btnUplift', 'btnAiUplift', 'btnAiNaming',
                     'btnAiGateways', 'btnAiLayout', 'btnCompliance', 'btnReviewXlsx'];

function setBusy(busy) {
  // Releasing busy must restore each button from state, never enable blindly.
  $('btnConvert').disabled = busy || !state.imageB64;
  $('btnRedo').disabled = busy || !state.model;
  $('btnApprove').disabled = busy || !state.xml;
  $('btnRollback').disabled = busy || !state.history.length;
  for (const id of ENGINE_BTNS) $(id).disabled = busy || !state.xml;
  $('btnReport').disabled = busy || !(state.changes.length || state.complianceResults);
  document.body.classList.toggle('busy', busy);
}

async function loadOutputs() {
  try {
    const r = await fetch('./amplify_outputs.json', { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    const o = await r.json();
    state.apiUrl = o?.custom?.diagramiqApiUrl || '';
  } catch { /* not deployed yet */ }
  if (!state.apiUrl) {
    setStatus('Backend not connected — deploy via Amplify (see README). You can still explore the UI.', 'warn');
  } else {
    setStatus('Ready. Upload a process image, then press ✨ AI Convert.');
  }
}

/* ---------- image handling (downscale client-side, like the desktop app) --- */
function acceptImage(file) {
  if (!file || !file.type.startsWith('image/')) { setStatus('Please choose an image file.', 'error'); return; }
  const img = new Image();
  img.onload = () => {
    const MAX = 1900;
    const scale = Math.min(1, MAX / Math.max(img.width, img.height));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(img.width * scale));
    c.height = Math.max(1, Math.round(img.height * scale));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    const dataUrl = c.toDataURL('image/png');
    state.imageB64 = dataUrl.split(',')[1];
    state.mediaType = 'image/png';
    $('imgPreview').src = dataUrl;
    $('imgPane').classList.add('has-image');
    $('btnConvert').disabled = false;
    setStatus(`Image loaded (${img.width}×${img.height}) — press ✨ AI Convert.`);
  };
  img.onerror = () => setStatus('Could not read that image.', 'error');
  img.src = URL.createObjectURL(file);
}

/* ---------- API calls ------------------------------------------------------ */
async function post(path, body) {
  if (!state.apiUrl) throw new Error('Backend not connected — deploy the Amplify backend first (see README).');
  const r = await fetch(state.apiUrl + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

/* ---------- engine helpers ------------------------------------------------- */

/** Read a File as base64 (no data: prefix). */
function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1]);
    r.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    r.readAsDataURL(file);
  });
}

const TEXT_MIMES = ['application/xml', 'text/csv', 'text/plain'];
function download(bytesOrText, filename, mime) {
  // Base64 payloads come back from the API; text we build in the browser.
  const blob = typeof bytesOrText === 'string' && !TEXT_MIMES.includes(mime)
    ? new Blob([Uint8Array.from(atob(bytesOrText), (c) => c.charCodeAt(0))], { type: mime })
    : new Blob([bytesOrText], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** Take a BPMN as the working document. */
function adoptXml(xml, source) {
  state.xml = xml;
  state.xmlSource = source;
  $('xmlOut').value = xml;
  for (const id of ENGINE_BTNS) $(id).disabled = !xml;
  $('btnApprove').disabled = !xml;
}

/** Draw a model in the preview pane and describe it in the stats line.
    Deliberately does not touch state.xml: an engine route owns the XML it
    returned, and regenerating it from the (lossier) preview model would throw
    away exactly the detail the uplift just added. */
function renderPreview(model) {
  const name = $('processName').value.trim() || model?.process_name || 'DiagramIQ AWS Process';
  const layout = layoutModel({ ...model, process_name: name });
  zoom.render(renderSvg(layout));
  const nodes = Object.values(layout.nodes);
  const count = (...types) => nodes.filter((n) => types.includes(n.type)).length;
  $('stats').textContent =
    `Understood:  ${layout.lanes.length} lanes · ${count('task')} tasks · ` +
    `${count('gateway')} gateways · ${count('start', 'end', 'intermediate')} events · ` +
    `${layout.edges.length} connections`;
  $('btnRedo').disabled = false;
}

/** Adopt a BPMN and render what it contains. Every XML-producing route returns
    `model`, so the preview fills in for Excel, Visio and uploads — not only
    the image path. */
function setXml(xml, note, model) {
  adoptXml(xml, 'engine');
  if (model && model.nodes && model.nodes.length) {
    state.model = model;
    try {
      renderPreview(model);
    } catch (e) {
      setStatus(`Preview could not be drawn: ${e.message}`, 'warn');
    }
  }
  if (note) setStatus(note);
  runPostBuildChecks();
}


/* ---------- version history (⤺ Rollback) ----------------------------------
   Every step that replaces the working diagram stacks the previous one first,
   so a re-do that comes back worse than what it replaced is one click to undo.
   Feedback text is kept too — you get back the wording you were refining. */

const HISTORY_MAX = 20;
const clone = (v) => (v ? JSON.parse(JSON.stringify(v)) : null);

/** Capture the working document before `label` replaces it. */
function pushHistory(label) {
  if (!state.xml && !state.model) return;      // nothing worth restoring yet
  state.history.push({
    label,
    xml: state.xml,
    xmlSource: state.xmlSource,
    model: clone(state.model),
    discovery: clone(state.discovery),
    changes: [...state.changes],
    complianceResults: clone(state.complianceResults),
    complianceReport: clone(state.complianceReport),
    reviewXlsx: state.reviewXlsx,
    validation: clone(state.validation),
    feedback: $('feedback').value,
  });
  if (state.history.length > HISTORY_MAX) state.history.shift();
  syncRollback();
}

function syncRollback() {
  const n = state.history.length;
  const btn = $('btnRollback');
  btn.disabled = !n;
  btn.textContent = n ? `⤺ Rollback (${n})` : '⤺ Rollback';
  btn.title = n ? `Undo "${state.history[n - 1].label}" and restore the previous version`
                : 'Nothing to roll back to yet';
}

function rollback() {
  const prev = state.history.pop();
  if (!prev) return;
  Object.assign(state, {
    xml: prev.xml, model: prev.model, discovery: prev.discovery,
    changes: prev.changes, complianceResults: prev.complianceResults,
    complianceReport: prev.complianceReport, reviewXlsx: prev.reviewXlsx,
    validation: prev.validation,
  });
  adoptXml(prev.xml, prev.xmlSource);
  $('feedback').value = prev.feedback || '';
  if (prev.model && prev.model.nodes && prev.model.nodes.length) {
    renderPreview(prev.model);
  } else {
    // Nothing to draw from — better a blank pane than a diagram that is no
    // longer the working document.
    zoom.clear();
    $('stats').textContent = '';
    $('btnRedo').disabled = true;
  }
  paintChecks();
  syncRollback();
  setBusy(false);
  setStatus(`Rolled back — undid "${prev.label}". ` +
            (state.history.length ? `${state.history.length} more step(s) available.`
                                  : 'This is the earliest version kept.'));
}

/* ---------- post-build checks ---------------------------------------------
   BPMN rule validation is local and cheap, so it runs by itself the moment a
   diagram exists. The 76-rule quality audit costs a model call, so it stays
   behind a click — but it sits in the same strip, right under the diagram. */

const VALIDATION_TITLE = 'Validation report';
const QUALITY_TITLE = 'Quality & compliance report';

function validationRows(payload) {
  const rows = [['Severity', 'Source', 'Rule', 'Element', 'Message']];
  for (const i of payload.issues || []) {
    rows.push([i.severity, i.source, i.rule_id, i.element_name || i.element_id || '—', i.message]);
  }
  return rows;
}

function chip(id, text, cls, onOpen) {
  const el = $(id);
  el.textContent = text;
  el.className = `hchip ${cls}`.trim();
  el.disabled = !onOpen;
  el.onclick = onOpen || null;
  if (onOpen) el.classList.add('live');
}

/** Redraw both chips from whatever state currently holds. */
function paintChecks() {
  $('health').hidden = !state.xml;
  if (!state.xml) return;

  const v = state.validation;
  if (!v) {
    chip('btnHealthRules', 'BPMN rules — checking…', '');
  } else if (v.error) {
    chip('btnHealthRules', `BPMN rules — check failed`, 'warn');
  } else {
    const { errors = 0, warnings = 0 } = v.summary || {};
    const text = errors || warnings
      ? `BPMN rules — ${errors} error${errors === 1 ? '' : 's'}, ${warnings} warning${warnings === 1 ? '' : 's'}`
      : 'BPMN rules — clean';
    chip('btnHealthRules', text, errors ? 'err' : warnings ? 'warn' : 'ok',
         () => showReport(VALIDATION_TITLE, v, validationRows(v)));
  }

  const c = state.complianceReport;
  if (!c) {
    chip('btnHealthQuality', 'Data quality — run the 76-rule audit', '', compliance);
  } else {
    chip('btnHealthQuality', `Data quality — ${c.headline}`, c.cls,
         () => showReport(QUALITY_TITLE, c.payload, c.rows));
  }
}

let checkSeq = 0;

/** Validate the working diagram in the background. Never throws, never takes
    the busy lock — it must not get in the way of the next action. */
async function runPostBuildChecks() {
  state.validation = null;
  // A verdict describes the document it was run against. Once that document is
  // replaced, showing the old tally next to the new diagram would be a lie —
  // and it would ride into the uplift report as though it still applied.
  state.complianceReport = null;
  state.complianceResults = null;
  // A newer diagram invalidates an in-flight check; only the latest may paint.
  const seq = ++checkSeq;
  paintChecks();
  if (!state.xml || !state.apiUrl) return;
  try {
    const payload = await post('/validate', { xml: state.xml });
    if (seq !== checkSeq) return;
    state.validation = payload;
  } catch (e) {
    if (seq !== checkSeq) return;
    state.validation = { error: e.message, issues: [], summary: {} };
  }
  paintChecks();
}

/** Show a validation or compliance report in a sheet, with a download. */
function showReport(title, payload, csvRows) {
  state.lastReport = { title, rows: csvRows };
  $('reportTitle').textContent = title;

  const { summary = {} } = payload;
  const chips = [];
  if (summary.errors !== undefined) {
    chips.push(['err', `${summary.errors} errors`]);
    chips.push(['warn', `${summary.warnings} warnings`]);
    chips.push(['', `${summary.info} info`]);
  }
  $('reportSummary').innerHTML = '';
  for (const [cls, text] of chips) {
    const el = document.createElement('span');
    el.className = `chip ${cls}`.trim();
    el.textContent = text;
    $('reportSummary').appendChild(el);
  }

  const tbody = $('reportTable').querySelector('tbody');
  tbody.innerHTML = '';
  for (const r of csvRows.slice(1)) {
    const tr = document.createElement('tr');
    tr.dataset.sev = r[0];
    for (const cell of r) {
      const td = document.createElement('td');
      td.textContent = cell;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  $('reportModal').showModal();
}

function downloadReport() {
  if (!state.lastReport) return;
  const csv = state.lastReport.rows
    .map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(','))
    .join('\n');
  const name = state.lastReport.title.toLowerCase().replace(/[^\w]+/g, '_');
  download(csv, `${name}.csv`, 'text/csv');
}

/** Run an engine call with busy state and uniform error reporting. */
async function engine(label, fn) {
  if (!state.xml) { setStatus('Load or generate a BPMN first.', 'error'); return; }
  setBusy(true);
  setStatus(`${label}…`);
  try {
    await fn();
  } catch (e) {
    setStatus(`${label} failed: ${e.message}`, 'error');
  } finally {
    setBusy(false);
  }
}

/** The image/feedback path: the model is the source of truth, so the BPMN is
    (re)built from it and then drawn. */
function refreshFromModel() {
  const name = $('processName').value.trim() || state.model?.process_name || 'DiagramIQ AWS Process';
  adoptXml(buildBpmn(state.model, name).xml, 'model');
  renderPreview(state.model);
  runPostBuildChecks();
}

async function aiConvert() {
  if (!state.imageB64) { setStatus('Upload an image first.', 'error'); return; }
  pushHistory('AI Convert');
  setBusy(true);
  setStatus('✨ AI is reading the diagram… (10–60s)');
  try {
    const data = await post('/convert', {
      imageBase64: state.imageB64,
      mediaType: state.mediaType,
      modelId: currentModelId(),
    });
    state.model = data.model;
    if ($('processName').value.trim() === '' && state.model.process_name) {
      $('processName').value = state.model.process_name;
    }
    refreshFromModel();
    setStatus('Review the preview. Approve to download the BPMN, or type feedback and Re-do.');
  } catch (e) {
    setStatus(`AI Convert failed: ${e.message}`, 'error');
  } finally {
    setBusy(false);
  }
}

async function redo() {
  const fb = $('feedback').value.trim();
  if (!state.model) return;
  if (!fb) { refreshFromModel(); setStatus('Re-rendered (no feedback text given).'); return; }
  pushHistory('Re-do with feedback');
  setBusy(true);
  setStatus('Applying your feedback with AI…');
  try {
    const data = await post('/feedback', {
      model: state.model,
      feedback: fb,
      modelId: currentModelId(),
    });
    state.model = data.model;
    const wasEngine = state.xmlSource === 'engine';
    refreshFromModel();
    setStatus('Feedback applied — review again, then Approve.' + (wasEngine
      ? ' Note: Re-do rebuilds the BPMN from the AI\'s understanding, so uplift'
        + ' or layout work already in the XML is not carried over — ⤺ Rollback restores it.'
      : ''), wasEngine ? 'warn' : 'info');
  } catch (e) {
    setStatus(`Feedback failed: ${e.message}`, 'error');
  } finally {
    setBusy(false);
  }
}


/* ---------- review grid (the ⬆ Notes / ⬆ Excel popup) ---------------------- */

const REV_COLS = ['activity', 'description', 'participant', 'it_systems', 'dependency'];

/** Draw one editable row. Fields not shown in the grid ride along untouched. */
function revRow(step, i) {
  const tr = document.createElement('tr');
  const num = document.createElement('td');
  num.className = 'num';
  num.textContent = i + 1;
  tr.appendChild(num);

  for (const key of REV_COLS) {
    const td = document.createElement('td');
    const ta = document.createElement('textarea');
    ta.value = step[key] || '';
    ta.dataset.key = key;
    ta.rows = 1;
    td.appendChild(ta);
    tr.appendChild(td);
  }

  const del = document.createElement('td');
  const btn = document.createElement('button');
  btn.className = 'del';
  btn.textContent = '×';
  btn.title = 'Remove this step';
  btn.addEventListener('click', () => { tr.remove(); renumberRows(); });
  del.appendChild(btn);
  tr.appendChild(del);

  // Keep the untouched fields (documents, templates, frequency, bpmn_id) so the
  // round-trip back to Excel does not silently drop them.
  tr._extra = { ...step };
  return tr;
}

function renumberRows() {
  const rows = [...$('revTable').querySelectorAll('tbody tr')];
  rows.forEach((tr, i) => { tr.querySelector('td.num').textContent = i + 1; });
  $('revCount').textContent = `${rows.length} step${rows.length === 1 ? '' : 's'}`;
}

/** Read the grid back into a discovery object. */
function readReview() {
  const steps = [...$('revTable').querySelectorAll('tbody tr')].map((tr) => {
    const step = { ...(tr._extra || {}) };
    for (const ta of tr.querySelectorAll('textarea')) step[ta.dataset.key] = ta.value.trim();
    return step;
  }).filter((st) => Object.values(st).some((v) => String(v || '').trim()));

  return {
    ...(state.discovery || {}),
    process_name: $('revName').value.trim(),
    trigger_event: $('revTrigger').value.trim(),
    successful_outcome: $('revSuccess').value.trim(),
    unsuccessful_outcome: $('revFail').value.trim(),
    steps,
  };
}

function openReview(discovery, title) {
  state.discovery = discovery;
  $('reviewTitle').textContent = title || 'Review the extracted process';
  $('revName').value = discovery.process_name || '';
  $('revTrigger').value = discovery.trigger_event || '';
  $('revSuccess').value = discovery.successful_outcome || '';
  $('revFail').value = discovery.unsuccessful_outcome || '';

  const tbody = $('revTable').querySelector('tbody');
  tbody.innerHTML = '';
  (discovery.steps || []).forEach((st, i) => tbody.appendChild(revRow(st, i)));
  renumberRows();
  $('reviewModal').showModal();
}

async function exportReviewXlsx() {
  const discovery = readReview();
  try {
    const d = await post('/discovery-xlsx', { discovery, processName: discovery.process_name });
    download(d.fileBase64, d.filename, XLSX_MIME);
  } catch (e) {
    setStatus(`Export failed: ${e.message}`, 'error');
  }
}

/** Approve: turn the edited grid into BPMN and render it. */
async function approveReview() {
  const discovery = readReview();
  if (!discovery.steps.length) { setStatus('Add at least one step before approving.', 'error'); return; }
  $('reviewModal').close();
  pushHistory('Approve & build BPMN');
  setBusy(true);
  setStatus('Building BPMN from the approved steps…');
  try {
    const d = await post('/discovery-to-bpmn', {
      discovery, processName: discovery.process_name,
    });
    state.discovery = discovery;
    state.reviewXlsx = d.fileBase64 || '';
    setXml(d.xml, `BPMN built from ${discovery.steps.length} approved step(s).`, d.model);
  } catch (e) {
    setStatus(`Could not build the BPMN: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

/* ---------- engine actions ------------------------------------------------- */

const XLSX_MIME =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

const procName = () => $('processName').value.trim();

async function acceptBpmnFile(file) {
  if (!file) return;
  pushHistory(`Load ${file.name}`);
  setBusy(true);
  try {
    const xml = await file.text();
    let model = null;
    try {
      ({ model } = await post('/model', { xml }));
    } catch { /* preview is a bonus; the BPMN is usable without it */ }
    setXml(xml, `Loaded ${file.name}. Validate or uplift it.`, model);
    state.changes = [];
  } catch (e) {
    setStatus(`Could not load ${file.name}: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

async function acceptEngineFile(file, route, label) {
  if (!file) return;
  pushHistory(label);
  setBusy(true);
  setStatus(`${label}…`);
  try {
    const data = await post(route, {
      fileBase64: await fileToB64(file),
      filename: file.name,
      processName: procName(),
    });
    setXml(data.xml, `${label} done — ${file.name} converted. Review, then Approve.`, data.model);
    state.changes = [];
  } catch (e) {
    setStatus(`${label} failed: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

/** ⬆ Notes — .txt, .md, .docx or .pdf. The file is read server-side, so Word
    and PDF behave exactly like plain text. */
/** ⬆ Excel — a Process Discovery workbook opens in the same editable grid. */
async function acceptExcel(file) {
  if (!file) return;
  setBusy(true);
  setStatus(`Reading ${file.name}…`);
  try {
    const { discovery } = await post('/excel-to-discovery', {
      fileBase64: await fileToB64(file), filename: file.name,
    });
    openReview(discovery || {}, `Review — ${file.name}`);
    setStatus('Review and edit the steps, then Approve to build the BPMN.');
  } catch (e) {
    setStatus(`Excel upload failed: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

async function acceptNotes(file) {
  if (!file) return;
  setBusy(true);
  setStatus(`AI is reading ${file.name}… (10–60s)`);
  try {
    const data = await post('/notes', {
      fileBase64: await fileToB64(file),
      filename: file.name,
      processName: procName(),
    });
    state.reviewXlsx = data.fileBase64 || '';
    openReview(data.discovery || {}, `Review — ${file.name}`);
    setStatus('Review and edit the steps, then Approve to build the BPMN.');
  } catch (e) {
    setStatus(`Notes analysis failed: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

const validate = () => engine('Validating', async () => {
  const payload = await post('/validate', { xml: state.xml });
  state.validation = payload;
  paintChecks();
  const rows = validationRows(payload);
  if (rows.length === 1) { setStatus('Validation passed — no issues found.'); return; }
  showReport(VALIDATION_TITLE, payload, rows);
  const { errors = 0, warnings = 0 } = payload.summary || {};
  setStatus(`Validation: ${errors} errors, ${warnings} warnings.`);
});

const uplift = () => engine('Applying rule-based uplift', async () => {
  pushHistory('Rule-based uplift');
  const d = await post('/uplift', { xml: state.xml, processName: procName() });
  setXml(d.xml, 'Rule-based uplift applied. Validate again to see what changed.', d.model);
});

const aiUplift = () => engine('AI uplift', async () => {
  pushHistory('AI uplift');
  const d = await post('/ai-uplift', { xml: state.xml, processName: procName() });
  setXml(d.xml, 'AI uplift applied. Validate again to see what changed.', d.model);
});

const aiLayout = () => engine('Cleaning layout', async () => {
  pushHistory('Layout cleanup');
  const d = await post('/ai-layout', { xml: state.xml });
  setXml(d.xml, 'Layout cleaned (waypoints only).', d.model);
});

const aiNaming = () => engine('Reviewing names', async () => {
  // The route works on names, not XML — pull them out of the working document.
  const doc = new DOMParser().parseFromString(state.xml, 'application/xml');
  const taskNames = {};
  for (const el of doc.querySelectorAll('task, userTask, serviceTask, scriptTask, manualTask')) {
    const id = el.getAttribute('id');
    const name = el.getAttribute('name');
    if (id && name) taskNames[id] = name;
  }
  if (!Object.keys(taskNames).length) { setStatus('No named tasks to review.', 'warn'); return; }
  const { fixes } = await post('/ai-naming', { taskNames });
  const n = Object.keys(fixes || {}).length;
  if (!n) { setStatus('Naming review: no changes suggested.'); return; }
  pushHistory('Naming review');
  for (const [id, newName] of Object.entries(fixes)) {
    const el = doc.querySelector(`[id="${id}"]`);
    if (el) el.setAttribute('name', newName);
  }
  setXml(new XMLSerializer().serializeToString(doc),
         `Naming review applied ${n} rename${n === 1 ? '' : 's'}.`);
});

const aiGateways = () => engine('Looking for missing gateways', async () => {
  const { suggestions } = await post('/ai-gateways', { xml: state.xml });
  if (!suggestions?.length) { setStatus('No missing gateways suggested.'); return; }
  setStatus(`${suggestions.length} gateway suggestion(s): ` +
            suggestions.map((s) => s.name || s.question || JSON.stringify(s)).join(' · '));
});

const compliance = () => engine('Auditing against the 76 Auspost rules', async () => {
  const { results } = await post('/ai-compliance', { xml: state.xml });
  const rows = Object.entries(results || {});
  if (!rows.length) { setStatus('Compliance audit returned no verdicts.', 'warn'); return; }
  state.complianceResults = results;
  $('btnReport').disabled = false;
  const tally = rows.reduce((a, [, v]) => (a[v.status] = (a[v.status] || 0) + 1, a), {});
  const table = [['Status', 'Rule ID', 'Notes']];
  for (const [rid, v] of rows) table.push([v.status, rid, v.notes || '']);
  const failed = tally['Not Verified'] || 0;
  const payload = { summary: { errors: failed, warnings: tally.Pending || 0,
                               info: tally.Verified || 0 } };
  state.complianceReport = {
    payload, rows: table,
    headline: `${tally.Verified || 0} of ${rows.length} rules verified`,
    cls: failed ? 'err' : 'ok',
  };
  paintChecks();
  showReport(QUALITY_TITLE, payload, table);
  setStatus('Compliance: ' + Object.entries(tally).map(([k, v]) => `${v} ${k}`).join(' · '));
});

const reviewXlsx = () => engine('Building the review sheet', async () => {
  const d = await post('/bpmn-to-excel', { xml: state.xml, processName: procName() });
  state.reviewXlsx = d.fileBase64;
  // Read it straight back into the grid so the reviewer edits in place; the
  // sheet is still one click away via Export.
  const b64 = d.fileBase64;
  const { discovery } = await post('/excel-to-discovery', { fileBase64: b64 });
  openReview(discovery || {}, 'Review the current diagram');
});

async function applyPatch(file) {
  if (!file) return;
  await engine('Applying your edits', async () => {
    pushHistory(`Edits from ${file.name}`);
    const d = await post('/patch', { xml: state.xml, fileBase64: await fileToB64(file) });
    state.changes = d.changes || [];
    $('btnReport').disabled = state.changes.length === 0;
    setXml(d.xml, `Applied ${state.changes.length} change(s) from ${file.name}.`, d.model);
  });
}

const upliftReport = () => engine('Building the uplift report', async () => {
  const d = await post('/uplift-report', {
    changes: state.changes,
    sourceName: procName() || 'process',
    outputName: `${procName() || 'process'}.bpmn`,
    complianceResults: state.complianceResults || null,
  });
  download(d.fileBase64, d.filename, XLSX_MIME);
  setStatus(`${d.filename} downloaded.`);
});

async function approveDownload() {
  if (!state.xml) return;
  const name = (procName() || 'diagramiq_process')
    .replace(/[^\w\- ]+/g, '').replace(/\s+/g, '_');

  // Celonis rejects BPMN carrying the Signavio extension namespace, so the
  // target picker decides which normalisation runs before download.
  let xml = state.xml;
  const target = $('targetSelect').value;
  let normalised = false;
  try {
    setBusy(true);
    xml = (await post('/normalize', { xml, target })).xml;
    normalised = true;
  } catch (e) {
    // Still worth downloading — but never claim a compatibility the file
    // hasn't actually been through.
    setStatus(`Normalisation failed — downloading un-normalised: ${e.message}`, 'warn');
  } finally {
    setBusy(false);
  }

  download(xml, `${name}.bpmn`, 'application/xml');
  if (!normalised) return;
  setStatus(target === 'celonis'
    ? `Saved ${name}.bpmn (Celonis-compatible) — upload it to the Celonis Process Repository.`
    : `Saved ${name}.bpmn — import it in Signavio via Import BPMN 2.0 XML.`);
}

/* ---------- wire up -------------------------------------------------------- */
window.addEventListener('DOMContentLoaded', () => {
  zoom = createZoom({ pane: $('svgPane'), label: $('zoomLabel') });
  loadOutputs();
  initModelPicker();
  syncRollback();
  paintChecks();
  $('fileInput').addEventListener('change', (e) => acceptImage(e.target.files[0]));
  const drop = $('imgPane');
  drop.addEventListener('dragover', (e) => { e.preventDefault(); drop.classList.add('drag'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
  drop.addEventListener('drop', (e) => {
    e.preventDefault(); drop.classList.remove('drag');
    acceptImage(e.dataTransfer.files[0]);
  });
  $('btnConvert').addEventListener('click', aiConvert);
  $('btnRedo').addEventListener('click', redo);
  $('btnRollback').addEventListener('click', rollback);
  $('btnApprove').addEventListener('click', approveDownload);
  $('btnXml').addEventListener('click', () => $('xmlModal').showModal());
  $('btnXmlClose').addEventListener('click', () => $('xmlModal').close());

  // ---- preview zoom ----
  $('btnZoomIn').addEventListener('click', () => zoom.in());
  $('btnZoomOut').addEventListener('click', () => zoom.out());
  $('btnZoomFit').addEventListener('click', () => zoom.fit());
  $('btnZoom100').addEventListener('click', () => zoom.actual());

  // ---- engine: alternative inputs ----
  $('bpmnInput').addEventListener('change', (e) => acceptBpmnFile(e.target.files[0]));
  $('excelInput').addEventListener('change', (e) => acceptExcel(e.target.files[0]));
  $('visioInput').addEventListener('change',
    (e) => acceptEngineFile(e.target.files[0], '/visio', 'Visio → BPMN'));
  $('notesInput').addEventListener('change', (e) => acceptNotes(e.target.files[0]));

  // ---- engine: actions ----
  $('btnValidate').addEventListener('click', validate);
  $('btnUplift').addEventListener('click', uplift);
  $('btnAiUplift').addEventListener('click', aiUplift);
  $('btnAiNaming').addEventListener('click', aiNaming);
  $('btnAiGateways').addEventListener('click', aiGateways);
  $('btnAiLayout').addEventListener('click', aiLayout);
  $('btnCompliance').addEventListener('click', compliance);
  $('btnReviewXlsx').addEventListener('click', reviewXlsx);
  $('patchInput').addEventListener('change', (e) => applyPatch(e.target.files[0]));
  $('btnReport').addEventListener('click', upliftReport);

  // ---- review sheet ----
  $('btnAddRow').addEventListener('click', () => {
    const tbody = $('revTable').querySelector('tbody');
    tbody.appendChild(revRow({}, tbody.children.length));
    renumberRows();
  });
  $('btnExportXlsx').addEventListener('click', exportReviewXlsx);
  $('btnApproveBuild').addEventListener('click', approveReview);
  $('btnReviewClose').addEventListener('click', () => $('reviewModal').close());
  $('btnReviewCancel').addEventListener('click', () => $('reviewModal').close());

  // ---- report sheet ----
  $('btnReportDownload').addEventListener('click', downloadReport);
  $('btnReportClose').addEventListener('click', () => $('reportModal').close());
  $('btnReportOk').addEventListener('click', () => $('reportModal').close());
});
