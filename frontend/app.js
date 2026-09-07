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
  history: [], validation: null, reviewReport: null, modellerInputs: null,
  // How the review grid's Approve should commit: 'build' makes a new BPMN from
  // the steps; 'patch' applies the edits onto the diagram already on screen,
  // which keeps its ids and its layout.
  reviewMode: 'build',
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

const DEGRADED_NOTE = ' · Running through the API gateway — the direct endpoint is '
  + 'not responding, so the longer AI passes may time out at 30s.';

function setStatus(msg, kind = 'info') {
  const el = $('status');
  // Degradation outlives any one message: the next thing that happens must not
  // scroll away the reason half the app is about to behave differently.
  const degraded = state.degraded && kind !== 'error';
  el.textContent = degraded ? msg + DEGRADED_NOTE : msg;
  el.dataset.kind = degraded ? 'warn' : kind;
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

const trimSlash = (u) => String(u || '').replace(/\/+$/, '');

async function loadOutputs() {
  try {
    const r = await fetch('./amplify_outputs.json', { cache: 'no-store' });
    if (!r.ok) throw new Error(String(r.status));
    const o = await r.json();
    state.apiUrl = trimSlash(o?.custom?.diagramiqApiUrl);
    // Function URLs, where present: they have no 30-second ceiling, which the
    // AI passes need. An older deployment has neither, and everything falls
    // back to the gateway.
    state.engineUrl = trimSlash(o?.custom?.diagramiqEngineUrl);
    state.aiUrl = trimSlash(o?.custom?.diagramiqAiUrl);
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

/** Drop the uploaded image. Another input channel has taken over, and leaving
    the old picture in the pane invites comparing it against a diagram it has
    nothing to do with. */
function clearImage() {
  if (!state.imageB64 && !$('imgPane').classList.contains('has-image')) return;
  state.imageB64 = '';
  $('imgPreview').removeAttribute('src');
  $('imgPane').classList.remove('has-image');
  $('btnConvert').disabled = true;
}

/* ---------- API calls ------------------------------------------------------ */

/** /convert and /feedback are the Node proxy; everything else is the Python
    engine. Each goes direct to its Lambda when a Function URL is published. */
const AI_PROXY_ROUTES = new Set(['/convert', '/feedback']);
function endpointFor(path) {
  const direct = AI_PROXY_ROUTES.has(path) ? state.aiUrl : state.engineUrl;
  return direct || state.apiUrl;
}

const origin = (u) => { try { return new URL(u).host; } catch { return u; } };

/** A failure before any response: DNS, connection refused, or a CORS preflight
    the endpoint did not answer. `fetch` reports all three the same way, with no
    status code — which is why the message has to name what was being called. */
const isUnreachable = (e) =>
  e instanceof TypeError || /failed to fetch|networkerror|load failed/i.test(e.message || '');

/** Throttling, wherever it came from.

    AWS answers a throttled invocation with 429 and a body carrying `Message`,
    not `error` — so it is not one of our own replies. Bedrock throttling
    arrives differently: the function catches it and returns 502 with the
    exception name in the text. Both are worth waiting out; nothing else is. */
function isThrottled(status, message) {
  if (status === 429) return true;
  return status === 502 && /throttl|too many requests|rate exceeded|quota/i.test(message);
}

async function send(base, path, body) {
  const r = await fetch(base + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    if (!data.error && (r.status === 503 || r.status === 504)) {
      // No error body means this never reached the function: the gateway gave
      // up while the Lambda was still working.
      throw new Error(`the gateway timed out (HTTP ${r.status}) — an API Gateway `
        + 'route cuts off at 30s and the AI passes run longer.');
    }
    const message = data.error || data.Message || data.message || `HTTP ${r.status}`;
    throw Object.assign(new Error(message), {
      status: r.status,
      throttled: isThrottled(r.status, message),
    });
  }
  return data;
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// Backoff for a throttled call. Jittered, so several calls throttled together
// do not all come back at the same instant and throttle each other again.
const BACKOFF_MS = [1500, 4000, 9000];

async function post(path, body) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await postOnce(path, body);
    } catch (err) {
      if (!err.throttled || attempt >= BACKOFF_MS.length) {
        if (err.throttled) {
          throw new Error(`AWS is throttling this account (HTTP ${err.status}) and it did `
            + `not clear after ${BACKOFF_MS.length} retries. Too many Lambda invocations `
            + 'are running at once — wait a minute for the in-flight ones to finish, or '
            + 'pick a lighter Bedrock model (Haiku 4.5) to shorten them.');
        }
        throw err;
      }
      const delay = BACKOFF_MS[attempt] + Math.round(Math.random() * 600);
      setStatus(`AWS throttled the request (HTTP ${err.status}). Retrying in `
        + `${Math.round(delay / 1000)}s — attempt ${attempt + 2} of ${BACKOFF_MS.length + 1}…`, 'warn');
      await wait(delay);
    }
  }
}

async function postOnce(path, body) {
  const direct = endpointFor(path);
  if (!direct) throw new Error('Backend not connected — deploy the Amplify backend first (see README).');
  const viaGateway = state.apiUrl && state.apiUrl !== direct ? state.apiUrl : '';

  try {
    return await send(direct, path, body);
  } catch (err) {
    // Only a *reachability* failure falls back. An error the function itself
    // returned is a real answer and re-sending it elsewhere would just repeat
    // the work and the failure.
    if (!viaGateway || !isUnreachable(err)) throw err;

    try {
      const data = await send(viaGateway, path, body);
      state.degraded = true;
      return data;
    } catch (err2) {
      if (!isUnreachable(err2)) throw err2;
      throw new Error(`the backend could not be reached. Neither ${origin(direct)} nor `
        + `${origin(viaGateway)} answered — this is usually a missing CORS `
        + 'configuration on the endpoint, or a backend that has not finished deploying.');
    }
  }
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
  // The Re-do note describes the edit that produced the *previous* document.
  $('redoNote').hidden = true;
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
    modellerInputs: clone(state.modellerInputs),
    reviewReport: clone(state.reviewReport),
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
    modellerInputs: prev.modellerInputs, reviewReport: prev.reviewReport,
    reviewXlsx: prev.reviewXlsx, validation: prev.validation,
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

  const r = state.reviewReport;
  if (!r) {
    chip('btnHealthQuality', 'Review report — checklist + what is still needed', '', compliance);
  } else {
    chip('btnHealthQuality', `Review report — ${r.headline}`, r.cls, () => openReviewReport());
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
  state.reviewReport = null;
  state.complianceResults = null;
  state.modellerInputs = null;
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

/* ---------- sheets: open, minimise, restore --------------------------------
   A modal dialog blocks the page, which is exactly wrong when the reviewer
   wants to read a report against the diagram it describes. Minimising reopens
   the same dialog non-modally, docked in the corner: the report stays
   readable and the diagram behind it stays live — zoom, pan and all. */

function openSheet(dlg) {
  dlg.classList.remove('docked');
  if (dlg.open) dlg.close();
  dlg.showModal();
}

function toggleDock(dlg, btn) {
  const dock = !dlg.classList.contains('docked');
  if (dlg.open) dlg.close();
  dlg.classList.toggle('docked', dock);
  if (dock) dlg.show(); else dlg.showModal();
  btn.textContent = dock ? '▣' : '▁';
  btn.title = dock ? 'Maximise' : 'Minimise';
  btn.setAttribute('aria-label', btn.title);
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
  openSheet($('reportModal'));
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
    // Same gate the transcription path has: the extraction is a draft until a
    // human has been through it step by step. The diagram is already drawn
    // behind the sheet — minimise it to compare the two.
    setStatus('Extracted. Opening the review sheet…');
    try {
      await openReviewForCurrent('Review — extracted from the image');
      setStatus('Check each step against the image, then Approve. Minimise the sheet to see the diagram.');
    } catch (e) {
      setStatus(`Diagram extracted, but the review sheet could not be built: ${e.message}`, 'warn');
    }
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
    const before = state.model;
    const data = await post('/feedback', {
      model: state.model,
      feedback: fb,
      modelId: currentModelId(),
    });
    state.model = data.model;
    const wasEngine = state.xmlSource === 'engine';
    refreshFromModel();

    const applied = diffModels(before, data.model);
    const notApplied = Array.isArray(data.notApplied) ? data.notApplied : [];
    showRedoNote(applied, notApplied);
    const head = applied.length
      ? `Re-do applied ${applied.length} change(s) — see the note below.`
      : 'Re-do changed nothing — see why below.';
    setStatus(head + (wasEngine && applied.length
      ? ' Note: Re-do rebuilds the BPMN from the AI\'s understanding, so uplift'
        + ' or layout work already in the XML is not carried over — ⤺ Rollback restores it.'
      : ''), applied.length ? (wasEngine ? 'warn' : 'info') : 'warn');
  } catch (e) {
    setStatus(`Feedback failed: ${e.message}`, 'error');
  } finally {
    setBusy(false);
  }
}


/** What actually changed between two models. The AI's own account of its
    edits is a claim; this is the ground truth, and it is what answers
    "it says it moved the task, but the diagram looks the same". */
function diffModels(before, after) {
  const A = new Map((before?.nodes || []).map((n) => [n.id, n]));
  const B = new Map((after?.nodes || []).map((n) => [n.id, n]));
  const out = [];
  for (const [id, n] of B) if (!A.has(id)) out.push(`added ${n.type || 'node'} “${n.name}”`);
  for (const [id, n] of A) if (!B.has(id)) out.push(`removed “${n.name}”`);
  for (const [id, n] of A) {
    const m = B.get(id);
    if (!m) continue;
    if (n.name !== m.name) out.push(`renamed “${n.name}” → “${m.name}”`);
    if (n.lane !== m.lane) out.push(`“${m.name}” moved to lane “${m.lane}”`);
    if (n.type !== m.type) out.push(`“${m.name}” is now a ${m.type}`);
    if (n.col !== m.col || n.row !== m.row) out.push(`“${m.name}” repositioned`);
  }
  const key = (f) => `${f.from}>${f.to}:${f.label || ''}`;
  const FA = new Set((before?.flows || []).map(key));
  const FB = new Set((after?.flows || []).map(key));
  const added = [...FB].filter((k) => !FA.has(k)).length;
  const gone = [...FA].filter((k) => !FB.has(k)).length;
  if (added) out.push(`${added} connection(s) added`);
  if (gone) out.push(`${gone} connection(s) removed`);
  return out;
}

/** Report the outcome of a Re-do under the feedback box. */
function showRedoNote(applied, notApplied) {
  const el = $('redoNote');
  const list = (items) => `<ul>${items.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`;
  const parts = [];
  parts.push(applied.length
    ? `<span class="head">Applied ${applied.length} change${applied.length === 1 ? '' : 's'}:</span>${list(applied)}`
    : '<span class="none">Nothing in the diagram changed.</span>');
  if (notApplied.length) {
    parts.push(`<span class="head">Not applied:</span>${list(notApplied)}`);
  }
  if (!applied.length && !notApplied.length) {
    parts.push('<ul><li>The AI returned the same model. Try naming the element '
             + 'exactly as it is labelled, and say what it should become.</li></ul>');
  }
  el.innerHTML = parts.join('');
  el.hidden = false;
}

const escapeHtml = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

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

function openReview(discovery, title, mode = 'build') {
  state.discovery = discovery;
  state.reviewMode = mode;
  $('btnApproveBuild').textContent = mode === 'patch'
    ? '✓ Approve & apply to the diagram' : '✓ Approve & build BPMN';
  $('reviewTitle').textContent = title || 'Review the extracted process';
  $('revName').value = discovery.process_name || '';
  $('revTrigger').value = discovery.trigger_event || '';
  $('revSuccess').value = discovery.successful_outcome || '';
  $('revFail').value = discovery.unsuccessful_outcome || '';

  const tbody = $('revTable').querySelector('tbody');
  tbody.innerHTML = '';
  (discovery.steps || []).forEach((st, i) => tbody.appendChild(revRow(st, i)));
  renumberRows();
  openSheet($('reviewModal'));
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

/** Open the review grid on the diagram currently on screen, by round-tripping
    it through the review workbook the engine already knows how to write. */
async function openReviewForCurrent(title) {
  const sheet = await post('/bpmn-to-excel', { xml: state.xml, processName: procName() });
  state.reviewXlsx = sheet.fileBase64;
  const { discovery } = await post('/excel-to-discovery', { fileBase64: sheet.fileBase64 });
  openReview(discovery || {}, title, 'patch');
}

/** Approve the edited grid.

    'build' makes a fresh BPMN from the steps — right when the steps are all
    there is (notes, a discovery workbook). 'patch' applies the edits onto the
    diagram already on screen, which is right when one exists: rebuilding it
    from a flat step list would throw away the ids and the faithful column and
    row positions read off the source image. */
async function approveReview() {
  const discovery = readReview();
  if (!discovery.steps.length) { setStatus('Add at least one step before approving.', 'error'); return; }
  const patching = state.reviewMode === 'patch' && !!state.xml;
  $('reviewModal').close();
  pushHistory(patching ? 'Approve reviewed steps' : 'Approve & build BPMN');
  setBusy(true);
  setStatus(patching ? 'Applying the approved steps to the diagram…'
                     : 'Building BPMN from the approved steps…');
  try {
    state.discovery = discovery;
    if (patching) {
      const sheet = await post('/discovery-xlsx', {
        discovery, processName: discovery.process_name,
      });
      state.reviewXlsx = sheet.fileBase64 || '';
      const d = await post('/patch', { xml: state.xml, fileBase64: sheet.fileBase64 });
      state.changes = d.changes || [];
      setXml(d.xml, state.changes.length
        ? `Applied ${state.changes.length} change(s) from your review.`
        : 'Approved — the review made no changes to the diagram.', d.model);
    } else {
      const d = await post('/discovery-to-bpmn', {
        discovery, processName: discovery.process_name,
      });
      state.reviewXlsx = d.fileBase64 || '';
      setXml(d.xml, `BPMN built from ${discovery.steps.length} approved step(s).`, d.model);
    }
  } catch (e) {
    setStatus(`Could not apply the review: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

/* ---------- engine actions ------------------------------------------------- */

const XLSX_MIME =
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

const procName = () => $('processName').value.trim();

async function acceptBpmnFile(file) {
  if (!file) return;
  clearImage();
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
  clearImage();
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
  clearImage();
  setBusy(true);
  setStatus(`Reading ${file.name}…`);
  try {
    const { discovery } = await post('/excel-to-discovery', {
      fileBase64: await fileToB64(file), filename: file.name,
    });
    openReview(discovery || {}, `Review — ${file.name}`, 'build');
    setStatus('Review and edit the steps, then Approve to build the BPMN.');
  } catch (e) {
    setStatus(`Excel upload failed: ${e.message}`, 'error');
  } finally { setBusy(false); }
}

/** What the document turned out to contain, in a phrase — so the reader can
    see the step table and the embedded flowchart were picked up, rather than
    assume it from a diagram that happens to look plausible. */
function describeSource(src) {
  if (!src) return '';
  if (src.mode === 'table') {
    return `Read the procedure table directly, without the AI — ${src.degraded}. `
      + 'Steps, roles and systems are here; the conditions and thresholds written in '
      + 'the clause text are not. Re-upload when the AI is available for the full reading.';
  }
  const bits = [];
  if (src.tables) bits.push(`${src.tables} table${src.tables === 1 ? '' : 's'}`);
  if (src.figures) bits.push(`${src.figures} figure${src.figures === 1 ? '' : 's'}`);
  if (src.headings) bits.push(`${src.headings} sections`);
  if (!bits.length) return '';
  let out = `Read ${bits.join(', ')}.`;
  if (src.skippedFigures) {
    // Word embeds pasted Office drawings as emf/wmf, which the model cannot read.
    out += ` ${src.skippedFigures} figure(s) were in a format the AI cannot read`
         + ' — export those as PNG and use ⬆ Image if they matter.';
  }
  return out;
}

/** ⬆ Notes / Document — a transcript or an SOP, as .txt, .md, .docx or .pdf.
    Word documents keep their headings, numbered clauses, tables and figures,
    and the figures go to the model with the text. */
async function acceptNotes(file) {
  if (!file) return;
  clearImage();
  setBusy(true);
  setStatus(`AI is reading ${file.name}… (10–60s)`);
  const payload = {
    fileBase64: await fileToB64(file),
    filename: file.name,
    processName: procName(),
    modelId: currentModelId(),
  };

  const show = (data) => {
    state.reviewXlsx = data.fileBase64 || '';
    const note = describeSource(data.source);
    openReview(data.discovery || {}, `Review — ${file.name}`, 'build');
    setStatus(`${note} Check the steps against the document, then Approve to build the BPMN.`
      .trim(), (data.source?.skippedFigures || data.source?.mode === 'table') ? 'warn' : 'info');
  };

  try {
    show(await post('/notes', payload));
  } catch (e) {
    // The AI call could not be made — throttled, or the engine unreachable.
    // A table-only read is a different shape of request: no Bedrock, a
    // sub-second invocation, and a far better chance of getting a concurrency
    // slot than the minute-long one that just failed. Worth one attempt before
    // telling the user there is nothing.
    setStatus(`${e.message} Trying without the AI — reading the procedure table…`, 'warn');
    try {
      show(await post('/notes', { ...payload, mode: 'table' }));
    } catch (e2) {
      setStatus(`Could not read ${file.name}: ${e.message}`
        + (/no step table/.test(e2.message) ? ' It has no procedure table to fall back on.' : ''),
        'error');
    }
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

/* ---------- the review report (two tabs) -----------------------------------
   Tab ①, the BPMN checklist, scores this diagram against every convention
   rule. Tab ②, what is still needed, is the judgement a rule check cannot
   make: the gaps a process expert would take back to the business, why each
   one matters, and the question to ask. Both tabs download as the two-sheet
   workbook the desktop app produced. */

function fillChecklistTab(results, rules) {
  const byId = new Map((rules || []).map((r) => [r.id, r]));
  const entries = Object.entries(results || {});
  const tally = entries.reduce((a, [, v]) => (a[v.status] = (a[v.status] || 0) + 1, a), {});
  const chips = [
    ['ok', `${tally.Verified || 0} verified`],
    ['err', `${tally['Not Verified'] || 0} not verified`],
    ['', `${tally['Not Applicable'] || 0} not applicable`],
  ];
  $('checklistSummary').innerHTML = chips
    .map(([c, t]) => `<span class="chip ${c}">${t}</span>`).join('');

  // Failures first — the point of the tab is what still needs doing.
  const rank = { 'Not Verified': 0, Verified: 1, 'Not Applicable': 2 };
  entries.sort((a, b) => (rank[a[1].status] ?? 3) - (rank[b[1].status] ?? 3));

  const tbody = $('checklistTable').querySelector('tbody');
  tbody.innerHTML = '';
  for (const [rid, v] of entries) {
    const rule = byId.get(rid);
    const tr = document.createElement('tr');
    tr.dataset.ok = v.status === 'Verified' ? 'yes' : v.status === 'Not Verified' ? 'no' : '';
    const cells = [
      v.status,
      rule ? `${rid} · ${rule.name}` : rid,
      v.notes || '',
    ];
    for (const [i, c] of cells.entries()) {
      const td = document.createElement('td');
      if (i === 1 && rule) {
        td.textContent = rid;
        const nm = document.createElement('div');
        nm.textContent = rule.name;
        nm.style.cssText = 'font-family:inherit;font-size:12px;color:#45566b;white-space:normal';
        td.appendChild(nm);
      } else {
        td.textContent = c;
      }
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  return { entries, tally };
}

function fillModellerTab(inputs) {
  const list = inputs || [];
  $('modellerSummary').innerHTML = list.length
    ? `<span class="chip warn">${list.length} gap${list.length === 1 ? '' : 's'} to take to the business</span>`
    : '<span class="chip ok">No gaps flagged — the diagram carries what a modeller needs</span>';

  const tbody = $('modellerTable').querySelector('tbody');
  tbody.innerHTML = '';
  list.forEach((g, i) => {
    const tr = document.createElement('tr');
    const num = document.createElement('td');
    num.className = 'num';
    num.textContent = i + 1;
    tr.appendChild(num);

    const cat = document.createElement('td');
    cat.innerHTML = `<span class="cat">${escapeHtml(g.category || '')}</span>`;
    tr.appendChild(cat);

    for (const v of [g.element_name || g.element_id || '— process-wide —',
                     g.what_missing || '', g.why_it_matters || '',
                     g.suggested_question || '']) {
      const td = document.createElement('td');
      td.textContent = v;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  });
}

function showTab(name) {
  for (const b of document.querySelectorAll('#insightModal .tab')) {
    b.classList.toggle('on', b.dataset.tab === name);
  }
  $('paneChecklist').hidden = name !== 'checklist';
  $('paneModeller').hidden = name !== 'modeller';
}

/** Render whatever the last run produced, without re-running it. */
function openReviewReport(tab = 'checklist') {
  const r = state.reviewReport;
  if (!r) return;
  fillChecklistTab(r.results, r.rules);
  fillModellerTab(r.inputs);
  $('insightCount').textContent = r.note;
  $('btnInsightDownload').disabled = false;
  showTab(tab);
  openSheet($('insightModal'));
}

/** Score the catalogue in slices.

    One call for all 76 rules is a single very long generation: slow enough to
    run into a caller's timeout, and long enough that one malformed token loses
    every verdict. Slices run concurrently, so the wall clock is roughly two
    batches rather than the whole catalogue, and a slice that fails costs only
    its own rules. */
const RULE_BATCH = 20;

/** Run `fn` over `items` with at most `limit` in flight, settling like
    Promise.allSettled so one failure costs only its own item. */
async function pool(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      try {
        out[i] = { status: 'fulfilled', value: await fn(items[i]) };
      } catch (reason) {
        out[i] = { status: 'rejected', reason };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

async function auditInBatches(xml) {
  // The first slice reports the catalogue size; the rest then go out together.
  const first = await post('/ai-compliance', { xml, ruleOffset: 0, ruleLimit: RULE_BATCH });
  const total = first.ruleTotal || RULE_BATCH;
  const offsets = [];
  for (let off = RULE_BATCH; off < total; off += RULE_BATCH) offsets.push(off);
  setStatus(`Scoring ${total} rules in ${offsets.length + 1} batches, and scanning for gaps…`);

  // Two at a time. Firing every remaining slice at once, alongside the gap
  // scan, is five concurrent Lambda invocations from one click — which is
  // enough to throttle an account whose concurrency limit is small, and the
  // slices then fail for a reason that has nothing to do with the diagram.
  const results = { ...(first.results || {}) };
  const rules = [...(first.rules || [])];
  let lost = 0;
  for (const part of await pool(offsets, 2,
      (off) => post('/ai-compliance', { xml, ruleOffset: off, ruleLimit: RULE_BATCH }))) {
    if (part.status !== 'fulfilled') { lost += 1; continue; }
    Object.assign(results, part.value.results || {});
    rules.push(...(part.value.rules || []));
  }
  return { results, rules, total, lost };
}

const compliance = () => engine('Building the review report', async () => {
  setStatus('Scoring the checklist and looking for gaps — this runs several AI passes…');
  // Independent passes: run them together, and let one survive the other
  // failing rather than losing both tabs to a single error.
  const [audit, gaps] = await Promise.all([
    auditInBatches(state.xml).catch((e) => ({ error: e.message })),
    post('/ai-modeller-inputs', { xml: state.xml }).catch((e) => ({ error: e.message })),
  ]);
  if (audit.error && gaps.error) throw new Error(audit.error);

  const results = audit.results || {};
  const inputs = Array.isArray(gaps.inputs) ? gaps.inputs : [];
  const entries = Object.entries(results);
  const tally = entries.reduce((a, [, v]) => (a[v.status] = (a[v.status] || 0) + 1, a), {});
  const failed = tally['Not Verified'] || 0;

  const notes = [];
  if (audit.error) notes.push(`checklist unavailable (${audit.error})`);
  if (gaps.error) notes.push(`gap scan unavailable (${gaps.error})`);
  if (!audit.error && !entries.length) notes.push('the checklist audit returned no verdicts');
  if (audit.lost) notes.push(`${audit.lost} rule batch(es) failed — ${entries.length} of `
                             + `${audit.total} rules scored`);

  state.complianceResults = entries.length ? results : null;
  state.modellerInputs = inputs;
  state.reviewReport = {
    results, rules: audit.rules || [], inputs,
    note: notes.length ? notes.join(' · ')
                       : `${entries.length} rules scored · ${inputs.length} gaps flagged`,
    headline: `${tally.Verified || 0}/${entries.length} verified · ${inputs.length} gaps`,
    cls: failed || !entries.length ? 'err' : inputs.length ? 'warn' : 'ok',
  };
  $('btnReport').disabled = !(state.changes.length || state.complianceResults);
  paintChecks();
  openReviewReport(failed || !inputs.length ? 'checklist' : 'checklist');
  setStatus(notes.length
    ? `Review report — ${notes.join(' · ')}`
    : `Review report: ${state.reviewReport.headline}.`,
    notes.length ? 'warn' : 'info');
});

/** The two-sheet workbook: BPMN Checklist + Required Inputs from Modeller. */
const downloadReviewReport = () => engine('Building the workbook', async () => {
  const d = await post('/uplift-report', {
    changes: state.changes || [],
    sourceName: procName() || 'process',
    outputName: `${procName() || 'process'}.bpmn`,
    complianceResults: state.complianceResults || null,
    modellerInputs: state.modellerInputs || null,
    onlySheets: ['checklist', 'modeller'],
  });
  download(d.fileBase64, `${(procName() || 'process').replace(/[^\w-]+/g, '_')}.review_report.xlsx`,
           XLSX_MIME);
  setStatus('Review report downloaded (BPMN Checklist + Required Inputs from Modeller).');
});

// The reviewer edits in place; the workbook is still one click away via Export.
const reviewXlsx = () => engine('Building the review sheet',
  () => openReviewForCurrent('Review the current diagram'));

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
    modellerInputs: state.modellerInputs || null,
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

  // ---- minimise / maximise on every sheet ----
  for (const [dlg, btn] of [['reviewModal', 'btnReviewMin'],
                            ['reportModal', 'btnReportMin'],
                            ['insightModal', 'btnInsightMin']]) {
    $(btn).addEventListener('click', () => toggleDock($(dlg), $(btn)));
  }

  // ---- review report ----
  for (const b of document.querySelectorAll('#insightModal .tab')) {
    b.addEventListener('click', () => showTab(b.dataset.tab));
  }
  $('btnInsightDownload').addEventListener('click', downloadReviewReport);
  $('btnInsightClose').addEventListener('click', () => $('insightModal').close());
  $('btnInsightOk').addEventListener('click', () => $('insightModal').close());

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
