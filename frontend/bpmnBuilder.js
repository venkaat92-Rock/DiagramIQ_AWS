/* DiagramIQ AWS — BPMN 2.0 builder (JS port of the DiagramIQ desktop emitter).
 *
 * Takes the structured model returned by the AI ({lanes, nodes, flows}) and
 * produces Signavio-ready BPMN 2.0 XML with full diagram interchange, using
 * the approved layout framework:
 *   - collaboration + participant pool (so Signavio keeps our layout)
 *   - content-fit swimlane bands (compact; height follows content)
 *   - clean orthogonal routing: straight in-row, side-exit + target-column
 *     entry for lane crossings, loop-backs routed under the pool
 *   - gateway labels placed BELOW the diamond
 */

const COL_W = 210, ROW_H = 130, LANE_PAD = 14;
const LOOP_PAD = 28, LOOP_GAP = 22;   // loop-back channel inside the last lane
const MARGIN = 30, POOL_HDR = 30, LANE_HDR = 30;

const TAGS = {
  start: 'startEvent', end: 'endEvent', task: 'task',
  gateway: 'exclusiveGateway', intermediate: 'intermediateThrowEvent',
};

const esc = (s) => String(s ?? '')
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&apos;');

function nodeSize(type) {
  if (type === 'gateway') return [52, 52];
  if (type === 'start' || type === 'end' || type === 'intermediate') return [40, 40];
  return [134, 72];
}

export function layoutModel(model) {
  const nodesIn = (model.nodes || []).filter((n) => n && n.id);
  let lanes = (model.lanes || []).map(String).filter((s) => s.trim());
  if (!lanes.length) lanes = [...new Set(nodesIn.map((n) => String(n.lane || 'Process')))];
  const laneSet = new Set(lanes);

  // Rows per lane -> content-fit band heights.
  const rowsPerLane = {};
  for (const n of nodesIn) {
    const lane = laneSet.has(String(n.lane)) ? String(n.lane) : lanes[0];
    n._lane = lane;
    const r = Math.max(0, Math.floor(Number(n.row) || 0));
    n._row = r;
    n._col = Math.max(0, Number(n.col) || 0);
    rowsPerLane[lane] = Math.max(rowsPerLane[lane] || 0, r);
  }

  const laneBands = [];
  let y = MARGIN;
  for (const lane of lanes) {
    const rows = (rowsPerLane[lane] ?? 0) + 1;
    const h = rows * ROW_H + 2 * LANE_PAD;
    laneBands.push({ name: lane, y, h });
    y += h;
  }
  const poolY = MARGIN;
  let poolH = y - MARGIN;   // grows below if loop-backs need a channel
  const bandOf = Object.fromEntries(laneBands.map((b) => [b.name, b]));

  const maxCol = Math.max(0, ...nodesIn.map((n) => n._col));
  const gridX0 = MARGIN + POOL_HDR + LANE_HDR + 30;
  const poolX = MARGIN;
  const poolW = gridX0 - MARGIN + (maxCol + 1) * COL_W + 30;

  const nodes = {};
  for (const n of nodesIn) {
    const type = TAGS[String(n.type || 'task').toLowerCase()] ? String(n.type).toLowerCase() : 'task';
    const [w, h] = nodeSize(type);
    const band = bandOf[n._lane];
    const cx = gridX0 + n._col * COL_W + COL_W / 2;
    const cy = band.y + LANE_PAD + n._row * ROW_H + ROW_H / 2;
    nodes[n.id] = { id: n.id, type, name: String(n.name || ''), lane: n._lane,
      col: n._col, x: cx - w / 2, y: cy - h / 2, w, h, cx, cy };
  }

  // Edges with the framework routing.
  const flowsIn = (model.flows || []).filter((f) => nodes[f.from] && nodes[f.to] && f.from !== f.to);
  const inCount = {}, inIdx = [];
  flowsIn.forEach((f, i) => {
    const fwd = nodes[f.to].col > nodes[f.from].col;
    inIdx[i] = fwd ? (inCount[f.to] = (inCount[f.to] || 0) + 1) - 1 : 0;
  });

  let loopK = 0;
  const edges = flowsIn.map((f, i) => {
    const s = nodes[f.from], t = nodes[f.to];
    let pts, loop = -1;
    if (Math.abs(s.cy - t.cy) < 2 && t.cx > s.cx) {
      pts = [[s.x + s.w, s.cy], [t.x, t.cy]];                       // straight in-row
    } else if (Math.abs(s.cx - t.cx) < 2) {
      pts = t.cy > s.cy
        ? [[s.cx, s.y + s.h], [t.cx, t.y]]                          // straight down
        : [[s.cx, s.y], [t.cx, t.y + t.h]];                         // straight up
    } else if (t.col > s.col) {
      const ic = inCount[f.to] || 1, ii = inIdx[i];
      const chx = t.x - 22 - ii * 14;                               // channel left of target
      const ey = t.cy + (ii - (ic - 1) / 2) * 14;                   // staggered entry row
      pts = [[s.x + s.w, s.cy], [chx, s.cy], [chx, ey], [t.x, ey]];
    } else {
      // Loop-back: routed along a horizontal channel, placed below once the
      // channel's height is known. Waypoints are filled in after the pass.
      loop = loopK; loopK += 1;
      pts = [];
    }
    return { id: `flow_${i + 1}`, from: f.from, to: f.to,
      label: String(f.label || ''), points: pts, loop };
  });

  // The loop-back channel lives INSIDE the pool: the last lane grows to make
  // room for it. Routing loop-backs below the pool, as this did originally,
  // draws sequence flows outside the very container that owns them — which
  // reads as the diagram spilling out of its frame, and is not valid BPMN DI.
  if (loopK) {
    const channel = LOOP_PAD + loopK * LOOP_GAP;
    laneBands[laneBands.length - 1].h += channel;
    poolH += channel;
    const chy0 = poolY + poolH - channel + LOOP_PAD / 2;
    for (const e of edges) {
      if (e.loop < 0) continue;
      const s = nodes[e.from], t = nodes[e.to];
      const chy = chy0 + e.loop * LOOP_GAP;
      e.points = [[s.cx, s.y + s.h], [s.cx, chy], [t.cx, chy], [t.cx, t.y + t.h]];
    }
  }
  for (const e of edges) delete e.loop;

  return { poolX, poolY, poolW, poolH, lanes: laneBands, nodes, edges,
    width: poolX + poolW + MARGIN, height: poolY + poolH + MARGIN };
}

export function buildBpmn(model, processName) {
  const L = layoutModel(model);
  const name = processName || model.process_name || 'DiagramIQ AWS Process';
  const nodeList = Object.values(L.nodes);

  const incoming = {}, outgoing = {};
  for (const e of L.edges) {
    (outgoing[e.from] = outgoing[e.from] || []).push(e.id);
    (incoming[e.to] = incoming[e.to] || []).push(e.id);
  }

  const laneXml = L.lanes.map((b, i) => {
    const refs = nodeList.filter((n) => n.lane === b.name)
      .map((n) => `        <bpmn:flowNodeRef>${esc(n.id)}</bpmn:flowNodeRef>`).join('\n');
    return `      <bpmn:lane id="Lane_${i + 1}" name="${esc(b.name)}">\n${refs}\n      </bpmn:lane>`;
  }).join('\n');

  const nodeXml = nodeList.map((n) => {
    const tag = TAGS[n.type];
    const io = [
      ...(incoming[n.id] || []).map((f) => `        <bpmn:incoming>${f}</bpmn:incoming>`),
      ...(outgoing[n.id] || []).map((f) => `        <bpmn:outgoing>${f}</bpmn:outgoing>`),
    ].join('\n');
    return `      <bpmn:${tag} id="${esc(n.id)}" name="${esc(n.name)}">\n${io}\n      </bpmn:${tag}>`;
  }).join('\n');

  const flowXml = L.edges.map((e) =>
    `      <bpmn:sequenceFlow id="${e.id}" sourceRef="${esc(e.from)}" targetRef="${esc(e.to)}"${e.label ? ` name="${esc(e.label)}"` : ''} />`,
  ).join('\n');

  const bounds = (x, y, w, h) =>
    `<dc:Bounds x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${w.toFixed(1)}" height="${h.toFixed(1)}" />`;

  const laneShapes = L.lanes.map((b, i) =>
    `      <bpmndi:BPMNShape id="Shape_Lane_${i + 1}" bpmnElement="Lane_${i + 1}" isHorizontal="true">\n        ${bounds(L.poolX + POOL_HDR, b.y, L.poolW - POOL_HDR, b.h)}\n      </bpmndi:BPMNShape>`,
  ).join('\n');

  const nodeShapes = nodeList.map((n) => {
    let label = '';
    if (n.type === 'gateway' && n.name) {
      const lw = Math.max(80, Math.min(200, n.name.length * 6.2));
      label = `\n        <bpmndi:BPMNLabel>${bounds(n.cx - lw / 2, n.y + n.h + 6, lw, 30)}</bpmndi:BPMNLabel>`;
    }
    return `      <bpmndi:BPMNShape id="Shape_${esc(n.id)}" bpmnElement="${esc(n.id)}">\n        ${bounds(n.x, n.y, n.w, n.h)}${label}\n      </bpmndi:BPMNShape>`;
  }).join('\n');

  const edgeShapes = L.edges.map((e) => {
    const wps = e.points.map(([x, yy]) => `        <di:waypoint x="${x.toFixed(1)}" y="${yy.toFixed(1)}" />`).join('\n');
    return `      <bpmndi:BPMNEdge id="Edge_${e.id}" bpmnElement="${e.id}">\n${wps}\n      </bpmndi:BPMNEdge>`;
  }).join('\n');

  const xml = `<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI" xmlns:dc="http://www.omg.org/spec/DD/20100524/DC" xmlns:di="http://www.omg.org/spec/DD/20100524/DI" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" id="Definitions_1" targetNamespace="http://diagramiq.aws" xsi:schemaLocation="http://www.omg.org/spec/BPMN/20100524/MODEL http://www.omg.org/spec/BPMN/2.0/20100501/BPMN20.xsd">
  <bpmn:collaboration id="Collaboration_1">
    <bpmn:participant id="Participant_1" name="${esc(name)}" processRef="Process_1" />
  </bpmn:collaboration>
  <bpmn:process id="Process_1" name="${esc(name)}" isExecutable="false">
    <bpmn:laneSet id="LaneSet_1">
${laneXml}
    </bpmn:laneSet>
${nodeXml}
${flowXml}
  </bpmn:process>
  <bpmndi:BPMNDiagram id="BPMNDiagram_1">
    <bpmndi:BPMNPlane id="BPMNPlane_1" bpmnElement="Collaboration_1">
      <bpmndi:BPMNShape id="Shape_Participant_1" bpmnElement="Participant_1" isHorizontal="true">
        ${bounds(L.poolX, L.poolY, L.poolW, L.poolH)}
      </bpmndi:BPMNShape>
${laneShapes}
${nodeShapes}
${edgeShapes}
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</bpmn:definitions>
`;
  return { xml, layout: L };
}
