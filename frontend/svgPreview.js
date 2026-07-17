/* DiagramIQ AWS — SVG preview renderer (port of the desktop bpmn_preview). */

const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function wrap(text, maxChars, maxLines = 3) {
  const words = String(text || '').split(/\s+/).filter(Boolean);
  const lines = [];
  let cur = '';
  for (const w of words) {
    if (!cur || (cur + ' ' + w).length <= maxChars) cur = cur ? cur + ' ' + w : w;
    else { lines.push(cur); cur = w; if (lines.length === maxLines) break; }
  }
  if (cur && lines.length < maxLines) lines.push(cur);
  if (lines.length === maxLines && words.join(' ').length > lines.join(' ').length) {
    lines[maxLines - 1] = lines[maxLines - 1].slice(0, maxChars - 1) + '…';
  }
  return lines;
}

export function renderSvg(layout) {
  const L = layout;
  const parts = [];
  parts.push(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${L.width} ${L.height}" font-family="Segoe UI, Arial, sans-serif">`);
  parts.push(`<rect x="0" y="0" width="${L.width}" height="${L.height}" fill="#ffffff"/>`);

  // Pool + header
  parts.push(`<rect x="${L.poolX}" y="${L.poolY}" width="${L.poolW}" height="${L.poolH}" fill="none" stroke="#8894a5" stroke-width="1.5"/>`);
  parts.push(`<rect x="${L.poolX}" y="${L.poolY}" width="30" height="${L.poolH}" fill="#f2f5f9" stroke="#8894a5" stroke-width="1"/>`);

  // Lanes
  for (const b of L.lanes) {
    parts.push(`<rect x="${L.poolX + 30}" y="${b.y}" width="${L.poolW - 30}" height="${b.h}" fill="none" stroke="#aeb8c4" stroke-width="1"/>`);
    parts.push(`<rect x="${L.poolX + 30}" y="${b.y}" width="30" height="${b.h}" fill="#f7f9fb" stroke="#aeb8c4" stroke-width="0.7"/>`);
    const lx = L.poolX + 45, ly = b.y + b.h / 2;
    parts.push(`<text x="${lx}" y="${ly}" font-size="11" fill="#5b6b7f" text-anchor="middle" transform="rotate(-90 ${lx} ${ly})">${esc(b.name.length > 42 ? b.name.slice(0, 41) + '…' : b.name)}</text>`);
  }

  // Edges
  for (const e of L.edges) {
    const pts = e.points.map((p) => p.join(',')).join(' ');
    parts.push(`<polyline points="${pts}" fill="none" stroke="#33475b" stroke-width="1.6"/>`);
    const [x2, y2] = e.points[e.points.length - 1];
    const [x1, y1] = e.points[e.points.length - 2];
    const a = Math.atan2(y2 - y1, x2 - x1);
    const p2 = [x2 - 9 * Math.cos(a - 0.45), y2 - 9 * Math.sin(a - 0.45)];
    const p3 = [x2 - 9 * Math.cos(a + 0.45), y2 - 9 * Math.sin(a + 0.45)];
    parts.push(`<polygon points="${x2},${y2} ${p2[0].toFixed(1)},${p2[1].toFixed(1)} ${p3[0].toFixed(1)},${p3[1].toFixed(1)}" fill="#33475b"/>`);
    if (e.label) {
      const [mx, my] = e.points[Math.floor(e.points.length / 2) - 1];
      parts.push(`<text x="${mx + 6}" y="${my - 6}" font-size="10" fill="#b0342a">${esc(e.label)}</text>`);
    }
  }

  // Nodes
  for (const n of Object.values(L.nodes)) {
    if (n.type === 'gateway') {
      const d = `M ${n.cx} ${n.y} L ${n.x + n.w} ${n.cy} L ${n.cx} ${n.y + n.h} L ${n.x} ${n.cy} Z`;
      parts.push(`<path d="${d}" fill="#ffe6a3" stroke="#b8860b" stroke-width="1.4"/>`);
      for (const [i, ln] of wrap(n.name, 26, 2).entries()) {
        parts.push(`<text x="${n.cx}" y="${n.y + n.h + 18 + i * 12}" font-size="10" fill="#101828" text-anchor="middle">${esc(ln)}</text>`);
      }
    } else if (n.type === 'start' || n.type === 'end' || n.type === 'intermediate') {
      const fill = n.type === 'start' ? '#d6ecd4' : n.type === 'end' ? '#f6d3ce' : '#fdf3c6';
      parts.push(`<ellipse cx="${n.cx}" cy="${n.cy}" rx="${n.w / 2}" ry="${n.h / 2}" fill="${fill}" stroke="#5b6b7f" stroke-width="1.4"/>`);
      parts.push(`<text x="${n.cx}" y="${n.cy + 3.5}" font-size="10" fill="#101828" text-anchor="middle">${esc(n.name.slice(0, 8))}</text>`);
    } else {
      parts.push(`<rect x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}" rx="7" fill="#dbe8fb" stroke="#3a5f8a" stroke-width="1.4"/>`);
      const lines = wrap(n.name, Math.max(6, Math.floor(n.w / 6.4)), 3);
      const y0 = n.cy - (lines.length - 1) * 6.5;
      for (const [i, ln] of lines.entries()) {
        parts.push(`<text x="${n.cx}" y="${(y0 + i * 13).toFixed(1)}" font-size="10.5" fill="#101828" text-anchor="middle">${esc(ln)}</text>`);
      }
    }
  }

  parts.push('</svg>');
  return parts.join('\n');
}
