/* Reading a .docx in the browser, with no backend and no dependencies.
 *
 * Every previous fix for a failing upload routed around a different layer —
 * CORS, then the gateway ceiling, then throttling — and each one still needed
 * the request to reach a Lambda. This path needs nothing: a .docx is a ZIP of
 * XML, the browser can inflate with DecompressionStream and parse with
 * DOMParser, and a procedure table is already the discovery schema. So a
 * document with a step table can be read with the network switched off.
 *
 * It is the lesser reading — no clause text, no figures, no model — but it is
 * the one that always works, and the caller labels it as such.
 */

const W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';

/* ---------- ZIP ----------------------------------------------------------- */

const EOCD = 0x06054b50;
const CEN = 0x02014b50;

/** Entries of a ZIP, read from its central directory.
    The central directory is authoritative: a local header may carry zeroed
    sizes when the writer used a data descriptor, and Word does. */
function readCentralDirectory(view, bytes) {
  // The EOCD sits at the end, after a comment of up to 64KB.
  let eocd = -1;
  for (let i = bytes.length - 22; i >= 0 && i >= bytes.length - 66000; i--) {
    if (view.getUint32(i, true) === EOCD) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error('not a zip archive');

  const count = view.getUint16(eocd + 10, true);
  let p = view.getUint32(eocd + 16, true);
  const entries = new Map();
  const dec = new TextDecoder();

  for (let n = 0; n < count; n++) {
    if (view.getUint32(p, true) !== CEN) break;
    const method = view.getUint16(p + 10, true);
    const compressedSize = view.getUint32(p + 20, true);
    const nameLen = view.getUint16(p + 28, true);
    const extraLen = view.getUint16(p + 30, true);
    const commentLen = view.getUint16(p + 32, true);
    const localOffset = view.getUint32(p + 42, true);
    const name = dec.decode(bytes.subarray(p + 46, p + 46 + nameLen));
    entries.set(name, { method, compressedSize, localOffset });
    p += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

async function readEntry(view, bytes, entry) {
  // The local header's own name/extra lengths give the offset of the data.
  const off = entry.localOffset;
  const nameLen = view.getUint16(off + 26, true);
  const extraLen = view.getUint16(off + 28, true);
  const start = off + 30 + nameLen + extraLen;
  const raw = bytes.subarray(start, start + entry.compressedSize);

  if (entry.method === 0) return raw;                 // stored
  if (entry.method !== 8) throw new Error(`unsupported zip compression ${entry.method}`);

  const stream = new Blob([raw]).stream()
    .pipeThrough(new DecompressionStream('deflate-raw'));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/* ---------- document.xml -------------------------------------------------- */

const local = (el) => el.localName;
const wChildren = (el, name) =>
  [...el.children].filter((c) => c.namespaceURI === W && local(c) === name);

function paraText(p) {
  let out = '';
  for (const node of p.querySelectorAll('*')) {
    if (node.namespaceURI !== W) continue;
    const tag = local(node);
    if (tag === 't') out += node.textContent;
    else if (tag === 'tab') out += '\t';
    else if (tag === 'br' || tag === 'cr') out += ' ';
  }
  return out.trim();
}

function headingLevel(p) {
  const ppr = wChildren(p, 'pPr')[0];
  const style = ppr && wChildren(ppr, 'pStyle')[0];
  const val = style ? (style.getAttributeNS(W, 'val') || '') : '';
  const m = /^heading\s*([1-6])$/i.exec(val.trim());
  return m ? Number(m[1]) : 0;
}

function isListItem(p) {
  const ppr = wChildren(p, 'pPr')[0];
  return !!(ppr && wChildren(ppr, 'numPr').length);
}

function tableRows(tbl) {
  const rows = [];
  for (const tr of wChildren(tbl, 'tr')) {
    const cells = wChildren(tr, 'tc').map((tc) =>
      wChildren(tc, 'p').map(paraText).filter(Boolean).join(' ').trim());
    if (cells.some(Boolean)) rows.push(cells);
  }
  if (!rows.length) return [];
  const width = Math.max(...rows.map((r) => r.length));
  return rows.map((r) => [...r, ...Array(width - r.length).fill('')]);
}

/** Read a .docx File into the same shape the engine's reader produces:
    {text, tables, headings, figures, title}. Throws on anything that is not
    a readable Word file. */
export async function readDocx(file) {
  const buf = await file.arrayBuffer();
  const bytes = new Uint8Array(buf);
  const view = new DataView(buf);

  const entries = readCentralDirectory(view, bytes);
  const doc = entries.get('word/document.xml');
  if (!doc) throw new Error('that .docx has no readable document body');

  const xml = new TextDecoder().decode(await readEntry(view, bytes, doc));
  const parsed = new DOMParser().parseFromString(xml, 'application/xml');
  if (parsed.querySelector('parsererror')) throw new Error('that .docx is damaged');

  const body = [...parsed.documentElement.children]
    .find((el) => el.namespaceURI === W && local(el) === 'body');
  if (!body) throw new Error('that .docx has no readable document body');

  const out = { text: '', tables: [], headings: 0, figures: 0, title: '' };
  const lines = [];

  for (const el of body.children) {
    if (el.namespaceURI !== W) continue;
    const tag = local(el);

    if (tag === 'p') {
      const drawings = el.getElementsByTagNameNS(
        'http://schemas.openxmlformats.org/drawingml/2006/main', 'blip').length;
      if (drawings) out.figures += drawings;
      const text = paraText(el);
      if (!text) continue;
      if (!out.title && !lines.length) out.title = text.slice(0, 120);
      const level = headingLevel(el);
      if (level) { out.headings += 1; lines.push('', '#'.repeat(level) + ' ' + text); }
      else if (isListItem(el)) lines.push('- ' + text);
      else lines.push(text);
    } else if (tag === 'tbl') {
      const rows = tableRows(el);
      if (!rows.length) continue;
      out.tables.push(rows);
      lines.push('');
      rows.forEach((r, i) => {
        lines.push('| ' + r.join(' | ') + ' |');
        if (i === 0) lines.push('|' + Array(r.length).fill(' --- ').join('|') + '|');
      });
      lines.push('');
    }
  }

  out.text = lines.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  if (!out.text) throw new Error('no text found in that Word document');
  return out;
}

export const canReadLocally = () =>
  typeof DecompressionStream === 'function' && typeof DOMParser === 'function';
