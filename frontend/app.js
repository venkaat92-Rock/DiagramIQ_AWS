/* DiagramIQ AWS — app logic. */
import { buildBpmn } from './bpmnBuilder.js';
import { renderSvg } from './svgPreview.js';

const $ = (id) => document.getElementById(id);
const state = { apiUrl: '', model: null, xml: '', imageB64: '', mediaType: 'image/png' };

function setStatus(msg, kind = 'info') {
  const el = $('status');
  el.textContent = msg;
  el.dataset.kind = kind;
}

function setBusy(busy) {
  for (const id of ['btnConvert', 'btnRedo', 'btnApprove']) $(id).disabled = busy;
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
  $('btnApprove').disabled = false;
  $('xmlOut').value = xml;
}

async function aiConvert() {
  if (!state.imageB64) { setStatus('Upload an image first.', 'error'); return; }
  setBusy(true);
  setStatus('✨ AI is reading the diagram… (10–60s)');
  try {
    const data = await post('/convert', {
      imageBase64: state.imageB64,
      mediaType: state.mediaType,
      modelId: $('modelId').value.trim() || undefined,
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
      modelId: $('modelId').value.trim() || undefined,
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

function approveDownload() {
  if (!state.xml) return;
  const name = ($('processName').value.trim() || 'diagramiq_process')
    .replace(/[^\w\- ]+/g, '').replace(/\s+/g, '_');
  const blob = new Blob([state.xml], { type: 'application/xml' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${name}.bpmn`;
  a.click();
  URL.revokeObjectURL(a.href);
  setStatus(`Saved ${name}.bpmn — import it in Signavio via Import BPMN 2.0 XML.`);
}

/* ---------- wire up -------------------------------------------------------- */
window.addEventListener('DOMContentLoaded', () => {
  loadOutputs();
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
});
