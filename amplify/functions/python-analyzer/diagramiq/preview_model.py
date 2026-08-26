"""BPMN 2.0 XML -> the structured model the browser preview renders.

frontend/bpmnBuilder.js and svgPreview.js expect the same shape the vision pass
returns: {process_name, lanes[], nodes[{id,type,name,lane,col,row}], flows[]}.
Producing it from any BPMN means every input channel — Visio, Excel, an uploaded
diagram, an uplift — lands in the same "what the AI understood" preview, not
just the image path.

Column and row come from the diagram's own coordinates where it has them, and
fall back to graph order when it does not (a freshly built BPMN may carry no DI).
"""
from __future__ import annotations

from .bpmn_parser import BPMNModel, parse_bpmn_xml


def _node_type(el) -> str:
    tag = el.tag_local()
    if tag == 'startEvent':
        return 'start'
    if tag == 'endEvent':
        return 'end'
    if el.kind == 'gateway':
        return 'gateway'
    if el.kind == 'task':
        return 'task'
    if el.kind == 'event':
        return 'intermediate'
    return 'task'


def _rank(values, tolerance: float):
    """Bucket coordinates into ordered indices, tolerating small jitter."""
    ordered = sorted(set(values))
    buckets: list[float] = []
    for v in ordered:
        if not buckets or v - buckets[-1] > tolerance:
            buckets.append(v)
    return {v: min(range(len(buckets)), key=lambda i: abs(buckets[i] - v))
            for v in ordered}


def model_from_bpmn(xml: str) -> dict:
    """Return the preview model. Never raises on odd input — worst case, empty."""
    try:
        m: BPMNModel = parse_bpmn_xml(xml)
    except Exception:
        return {'process_name': '', 'lanes': [], 'nodes': [], 'flows': []}

    lane_els = sorted(m.get_lanes(), key=lambda e: (e.y, e.name or ''))
    lane_of: dict[str, str] = {}
    for lane in lane_els:
        for ref in lane.flow_node_refs:
            lane_of[ref] = lane.name or 'Unassigned'
    lanes = [l.name or 'Unassigned' for l in lane_els]

    nodes_els = [e for e in m.elements.values()
                 if e.kind in ('task', 'gateway', 'event')]
    if not nodes_els:
        return {'process_name': '', 'lanes': lanes, 'nodes': [], 'flows': []}

    has_di = any(e.x or e.y for e in nodes_els)
    if has_di:
        col_rank = _rank([e.x for e in nodes_els], tolerance=40.0)
    else:
        # No diagram interchange: fall back to sequence order along the flows.
        order: dict[str, int] = {}
        outgoing = {}
        for f in m.get_flows():
            outgoing.setdefault(f.source_ref, []).append(f.target_ref)
        starts = [e.id for e in nodes_els if _node_type(e) == 'start'] or [nodes_els[0].id]
        seen, queue, depth = set(), list(starts), {s: 0 for s in starts}
        while queue:
            nid = queue.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            order[nid] = depth.get(nid, 0)
            for nxt in outgoing.get(nid, []):
                if nxt not in seen:
                    depth[nxt] = min(depth.get(nxt, 10**6), order[nid] + 1)
                    queue.append(nxt)
        for i, e in enumerate(nodes_els):
            order.setdefault(e.id, i)

    # Row is the node's position within its own lane, top to bottom.
    per_lane: dict[str, list] = {}
    for e in nodes_els:
        per_lane.setdefault(lane_of.get(e.id, lanes[0] if lanes else 'Process'), []).append(e)
    row_of: dict[str, int] = {}
    for lane, els in per_lane.items():
        if has_di:
            rank = _rank([e.y for e in els], tolerance=30.0)
            for e in els:
                row_of[e.id] = rank[e.y]
        else:
            for i, e in enumerate(sorted(els, key=lambda z: order.get(z.id, 0))):
                row_of[e.id] = 0 if len(els) == 1 else i % 3

    nodes = []
    for e in nodes_els:
        nodes.append({
            'id': e.id,
            'type': _node_type(e),
            'name': e.name or '',
            'lane': lane_of.get(e.id, lanes[0] if lanes else 'Process'),
            'col': col_rank[e.x] if has_di else order.get(e.id, 0),
            'row': row_of.get(e.id, 0),
        })

    flows = [{'from': f.source_ref, 'to': f.target_ref, 'label': f.name or ''}
             for f in m.get_flows() if f.source_ref and f.target_ref]

    name = ''
    for pid in m.processes:
        el = m.elements.get(pid)
        if el and el.name:
            name = el.name
            break

    return {
        'process_name': name,
        'lanes': lanes or ['Process'],
        'nodes': nodes,
        'flows': flows,
    }
