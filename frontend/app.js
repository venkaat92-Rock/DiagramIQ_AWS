/* DiagramIQ AWS — app logic. */
import { buildBpmn } from './bpmnBuilder.js';
import { renderSvg } from './svgPreview.js';

const $ = (id) => document.getElementById(id);
const state = {
  apiUrl: '', model: null, xml: '', imageB64: '', mediaType: 'image/png',
  // Engine state. `xml` is the working BPMN once anything produces one;
  // `changes` accumulates the patcher's change log for the uplift report.
  reviewXlsx: '', changes: [],
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
  for (const id of ['btnConvert', 'btnRedo', 'btnApprove']) $(id).disabled = busy;
  // Engine buttons need a BPMN, so releasing busy must not enable them blindly.
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

function download(bytesOrText, filename, mime) {
  const blob = typeof bytesOrText === 'string' && mime !== 'application/xml'
    ? new Blob([Uint8Array.from(atob(bytesOrText), (c) => c.charCodeAt(0))], { type: mime })
    : new Blob([bytesOrText], { type: mime });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

/** Adopt a BPMN XML as the working document (from upload, uplift, or patch). */
function setXml(xml, note) {
  state.xml = xml;
  $('xmlOut').value = xml;
  for (const id of ENGINE_BTNS) $(id).disabled = !xml;
  $('btnApprove').disabled = !xml;
  if (note) setStatus(note);
}

function renderIssues(payload) {
  const pane = $('issuesPane');
  const tbody = $('issuesTable').querySelector('tbody');
  tbody.innerHTML = '';
  const { issues = [], summary = {} } = payload;
  $('issuesSummary').textContent =
    `— ${summary.errors || 0} errors · ${summary.warnings || 0} warnings · ${summary.info || 0} info`;
  for (const i of issues) {
    const tr = document.createElement('tr');
    tr.dataset.sev = i.severity;
    for (const cell of [i.severity, i.source, i.rule_id, i.element_name || i.element_id || '—', i.message]) {
      const td = document.createElement('td');
      td.textContent = cell;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  pane.classList.toggle('hidden', issues.length === 0);
  if (!issues.length) setStatus('Validation passed — no issues found.');
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

function refreshFromModel() {
  const name = $('processName').value.trim() || state.model?.process_name || 'DiagramIQ AWS Process';
  const { xml, layout } = buildBpmn(state.model, name);
  state.xml = xml;
  $('svgPane').innerHTML = renderSvg(layout);
  const nodes = Object.values(layout.nodes);
  const stats = {
    lanes: layout.lanes.length,
    tasks: nodes.filter((n) => n.type === 'task').length,
    gateways: nodes.filter((n) => n.type === 'gateway').length,
    events: nodes.filter((n) => ['start', 'end', 'intermediate'].includes(n.type)).length,
    flows: layout.edges.length,
  };
  $('stats').textContent =
    `Understood:  ${stats.lanes} lanes · ${stats.tasks} tasks · ${stats.gateways} gateways · ${stats.events} events · ${stats.flows} connections`;
  $('btnRedo').disabled = false;
  // Hands the freshly built XML to the engine bar as the working document.
  setXml(xml);
}

async function aiConvert() {
  if (!state.imageB64) { setStatus('Upload an image first.', 'error'); return; }
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
  setBusy(true);
  setStatus('Applying your feedback with AI…');
  try {
    const data = await post('/feedback', {
      model: state.model,
      feedback: fb,
      modelId: currentModelId(),
    });
    state.model = data.model;
    refreshFromModel();
    setStatus('Feedback applied — review again, then Approve.');
  } catch (e) {
    setStatus(`Feedback failed: ${e.message}`, 'error');
  } finally {
    setBusy(false);
  }
}

/* ---------- engine actions ------------------------------------------------- */

const procName = () => $('processName').value.trim();

async function acceptBpmnFile(file) {
  if (!file) return;
  setBusy(true);
  try {
    setXml(await file.text(), `Loaded ${file.name}. Validate or uplift it.`);
    state.changes = [];
  } finally { setBusy(false); }
}

async function acceptEngineFile(file, route, label) {
  if (!file) return;
  setBusy(true);
  setStatus(`${label}…`);
  try {
    const data = await post(route, {
      fileBase64: await fileToB64(file),
      filename: file.name,
      processName: procName(),
    });
    setXml(data.xml, `${label} done — ${file.name} converted. Review, then Approve.`);
    state.changes = [];
  } catch (e) {
    setStatus(`${label} failed: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

async function acceptNotes(file) {
  if (!file) return;
  setBusy(true);
  setStatus('AI is reading the notes… (10–60s)');
  try {
    const data = await post('/notes', { text: await file.text(), processName: procName() });
    state.reviewXlsx = data.fileBase64;
    download(data.fileBase64, data.filename,
             'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    $('btnReviewXlsx').disabled = false;
    setStatus(`Process Discovery Excel downloaded (${data.filename}). ` +
              'Edit it, then re-upload via ⬆ Excel to build the BPMN.');
  } catch (e) {
    setStatus(`Notes analysis failed: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

const validate = () => engine('Validating', async () => {
  renderIssues(await post('/validate', { xml: state.xml }));
});

const uplift = () => engine('Applying rule-based uplift', async () => {
  const d = await post('/uplift', { xml: state.xml, processName: procName() });
  setXml(d.xml, 'Rule-based uplift applied. Validate again to see what changed.');
});

const aiUplift = () => engine('AI uplift', async () => {
  const d = await post('/ai-uplift', { xml: state.xml, processName: procName() });
  setXml(d.xml, 'AI uplift applied. Validate again to see what changed.');
});

const aiLayout = () => engine('Cleaning layout', async () => {
  const d = await post('/ai-layout', { xml: state.xml });
  setXml(d.xml, 'Layout cleaned (waypoints only).');
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
  const tally = rows.reduce((a, [, v]) => (a[v.status] = (a[v.status] || 0) + 1, a), {});
  state.complianceResults = results;
  $('btnReport').disabled = false;
  setStatus('Compliance: ' + Object.entries(tally).map(([k, v]) => `${v} ${k}`).join(' · ') +
            ' — download the uplift report for the full checklist.');
});

const reviewXlsx = () => engine('Building the review spreadsheet', async () => {
  const d = await post('/bpmn-to-excel', { xml: state.xml, processName: procName() });
  state.reviewXlsx = d.fileBase64;
  download(d.fileBase64, d.filename,
           'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  setStatus(`${d.filename} downloaded. Edit it, then use “⬆ Apply edited .xlsx”.`);
});

async function applyPatch(file) {
  if (!file) return;
  await engine('Applying your edits', async () => {
    const d = await post('/patch', { xml: state.xml, fileBase64: await fileToB64(file) });
    state.changes = d.changes || [];
    $('btnReport').disabled = state.changes.length === 0;
    setXml(d.xml, `Applied ${state.changes.length} change(s) from ${file.name}.`);
  });
}

const upliftReport = () => engine('Building the uplift report', async () => {
  const d = await post('/uplift-report', {
    changes: state.changes,
    sourceName: procName() || 'process',
    outputName: `${procName() || 'process'}.bpmn`,
    complianceResults: state.complianceResults || null,
  });
  download(d.fileBase64, d.filename,
           'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
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
  loadOutputs();
  initModelPicker();
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
  $('btnApprove').addEventListener('click', approveDownload);
  $('btnXml').addEventListener('click', () => $('xmlModal').showModal());
  $('btnXmlClose').addEventListener('click', () => $('xmlModal').close());

  // ---- engine: alternative inputs ----
  $('bpmnInput').addEventListener('change', (e) => acceptBpmnFile(e.target.files[0]));
  $('excelInput').addEventListener('change',
    (e) => acceptEngineFile(e.target.files[0], '/excel-to-bpmn', 'Excel → BPMN'));
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
});
