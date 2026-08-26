/* DiagramIQ AWS — zoom and pan for the preview pane.
 *
 * The renderer emits an SVG carrying a viewBox and no intrinsic size, so
 * zooming is just a matter of giving it an explicit pixel width: the viewBox
 * scales the drawing, and the pane scrolls once it no longer fits.
 *
 * "Fit" is the default and it is sticky — the diagram re-fits when the window
 * resizes or a new one is rendered, until the user zooms explicitly.
 */

const MIN = 0.15, MAX = 6, STEP = 1.25;

export function createZoom({ pane, label }) {
  let z = 1;
  let fit = true;

  const svg = () => pane.querySelector('svg');

  /** The drawing's own size, from its viewBox. */
  function natural() {
    const el = svg();
    if (!el) return null;
    const vb = (el.getAttribute('viewBox') || '').split(/[\s,]+/).map(Number);
    return vb.length === 4 && vb[2] > 0 && vb[3] > 0 ? { w: vb[2], h: vb[3] } : null;
  }

  function fitScale() {
    const n = natural();
    if (!n) return 1;
    const cw = pane.clientWidth - 18, ch = pane.clientHeight - 18;
    if (cw <= 0 || ch <= 0) return 1;
    // Never blow a small diagram up past 1:1 — enlarging it adds no detail.
    return Math.min(1, cw / n.w, ch / n.h);
  }

  function apply() {
    const el = svg(), n = natural();
    if (!el || !n) { label.textContent = '—'; return; }
    if (fit) z = fitScale();
    z = Math.min(MAX, Math.max(MIN, z));
    el.style.width = `${Math.round(n.w * z)}px`;
    el.style.height = `${Math.round(n.h * z)}px`;
    label.textContent = fit ? `Fit ${Math.round(z * 100)}%` : `${Math.round(z * 100)}%`;
    pane.classList.toggle('pannable', el.clientWidth > pane.clientWidth ||
                                      el.clientHeight > pane.clientHeight);
  }

  /** Zoom keeping the point under (cx, cy) — pane coordinates — put. */
  function zoomAt(next, cx, cy) {
    const before = z;
    fit = false;
    z = Math.min(MAX, Math.max(MIN, next));
    const ratio = z / before;
    const sx = pane.scrollLeft, sy = pane.scrollTop;
    apply();
    pane.scrollLeft = (sx + cx) * ratio - cx;
    pane.scrollTop = (sy + cy) * ratio - cy;
  }

  const centre = () => [pane.clientWidth / 2, pane.clientHeight / 2];

  // Ctrl/⌘ + wheel zooms; a plain wheel scrolls the pane as usual.
  pane.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    const r = pane.getBoundingClientRect();
    zoomAt(z * (e.deltaY < 0 ? STEP : 1 / STEP), e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  // Drag to pan once the diagram overflows.
  let drag = null;
  pane.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || !pane.classList.contains('pannable')) return;
    drag = { x: e.clientX, y: e.clientY, sx: pane.scrollLeft, sy: pane.scrollTop };
    pane.setPointerCapture(e.pointerId);
    pane.classList.add('panning');
  });
  pane.addEventListener('pointermove', (e) => {
    if (!drag) return;
    pane.scrollLeft = drag.sx - (e.clientX - drag.x);
    pane.scrollTop = drag.sy - (e.clientY - drag.y);
  });
  const endDrag = () => { drag = null; pane.classList.remove('panning'); };
  pane.addEventListener('pointerup', endDrag);
  pane.addEventListener('pointercancel', endDrag);

  // Only re-fit on resize while the user has not taken manual control.
  window.addEventListener('resize', () => { if (fit) apply(); });

  return {
    /** Replace the drawing and re-apply the current zoom. */
    render(svgMarkup) {
      pane.innerHTML = svgMarkup;
      pane.scrollLeft = pane.scrollTop = 0;
      apply();
    },
    clear() { pane.innerHTML = ''; label.textContent = '—'; pane.classList.remove('pannable'); },
    in() { zoomAt(z * STEP, ...centre()); },
    out() { zoomAt(z / STEP, ...centre()); },
    actual() { fit = false; z = 1; apply(); },
    fit() { fit = true; apply(); },
    get scale() { return z; },
  };
}
