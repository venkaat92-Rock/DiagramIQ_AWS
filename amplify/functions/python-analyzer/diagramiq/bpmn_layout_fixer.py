"""Deterministic BPMN layout normaliser (v1.2.23 - loop-back routing).

v1.2.23: event labels are pinned beside their event (no more stray end-event
label inflating the canvas), lanes shrink-to-fit the content, and loop-back
edges route through the UPPER lane's band so a cross-lane 2-step loop is clean.

Fixes the layout problems seen in transcript-generated BPMN, matching the
hand-tuned reference the user supplied. v1.2.22 adds taller lanes, a clear
top "loop band", a left-to-right re-flow of flow nodes along the forward
chain (so inserted merge/decision gateways never overlap a task), and
over-the-top routing for the negative loop-back edges. Decision-gateway
labels are concise and placed to the RIGHT of the diamond (wrap to ~2
lines); merge gateways are unlabelled.

  1. Taller lanes
     Each lane is re-stacked at a fixed, generous height so an input row
     above the task and an output row below the task both fit comfortably.

  2. Data-object placement
     - INPUT data objects sit ABOVE the task, aligned to the task's LEFT
       edge (top-left).
     - OUTPUT data objects sit BELOW the task, aligned to the task's RIGHT
       edge (bottom-right).

  3. Task / event / gateway vertical position
     Centred on a single row inside the lane.

  4. Sequence-flow routing
     - Same lane: a single straight horizontal segment.
     - Cross lane (up->down or down->up): clean orthogonal - exit the
       source's bottom/top centre, run vertically to the midpoint between
       the lanes, then horizontally to the target column, then into the
       target's top/bottom centre. Never diagonal.

  5. Gateway labels
     Each gateway's label is placed ABOVE the gateway shape.

Public API
----------
    fix_layout(xml: str) -> str
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

BPMN_NS    = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMN_DI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS      = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS      = "http://www.omg.org/spec/DD/20100524/DI"
SIG_NS     = "http://www.signavio.com"

# Geometry (px)
TASK_W, TASK_H = 120, 80
DO_W,   DO_H   = 90, 68
EVENT          = 36
GW_SIZE        = 40           # gateway diamond
LANE_H         = 360          # taller lanes - room for the loop-back band (was 300)
LANE_LABEL_PAD = 30           # pool left margin reserved for lane labels
COL_GAP        = 46           # horizontal gap between consecutive flow nodes

# Offsets inside a lane band of height LANE_H, lane top = LY
LOOP_Y_OFF = 24               # backward (loop-back) edges run in this top band
IN_Y_OFF   = 54               # input DO row y   -> +54..+122
TASK_Y_OFF = 150              # task y  = LY + this  -> task spans +150..+230
OUT_Y_OFF  = 262              # output DO row y -> +262..+330 within 360
DO_GAP     = 14


def _register(xml: str) -> None:
    if 'xmlns="' + BPMN_NS + '"' in xml:
        ET.register_namespace("", BPMN_NS)
    else:
        ET.register_namespace("bpmn", BPMN_NS)
    ET.register_namespace("bpmndi", BPMN_DI_NS)
    if 'xmlns:omgdc="' in xml:
        ET.register_namespace("omgdc", DC_NS)
        ET.register_namespace("omgdi", DI_NS)
    else:
        ET.register_namespace("dc", DC_NS)
        ET.register_namespace("di", DI_NS)
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("signavio", SIG_NS)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def fix_layout(xml: str, compact: bool = False) -> str:
    _register(xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml

    # Compact mode (Celonis export): the input/output data objects have been
    # removed, so use SHORT lanes with the task row near the top instead of the
    # tall 3-band lanes (input row / task / output row) the default needs.
    _LANE_H     = 160 if compact else LANE_H
    _TASK_Y_OFF = 40  if compact else TASK_Y_OFF
    _LOOP_Y_OFF = 12  if compact else LOOP_Y_OFF

    # ---- shape + bounds maps ----
    shape_el: Dict[str, ET.Element] = {}
    bounds_el: Dict[str, ET.Element] = {}
    for shape in root.iter(f"{{{BPMN_DI_NS}}}BPMNShape"):
        be = shape.get("bpmnElement")
        b = shape.find(f"{{{DC_NS}}}Bounds")
        if be and b is not None:
            shape_el[be] = shape
            bounds_el[be] = b

    def gx(eid):
        b = bounds_el[eid]
        return (float(b.get("x")), float(b.get("y")),
                float(b.get("width")), float(b.get("height")))

    def sx(eid, x=None, y=None, w=None, h=None):
        b = bounds_el[eid]
        if x is not None: b.set("x", str(int(round(x))))
        if y is not None: b.set("y", str(int(round(y))))
        if w is not None: b.set("width", str(int(round(w))))
        if h is not None: b.set("height", str(int(round(h))))

    # ---- lanes + membership ----
    lane_ids = [l.get("id") for l in root.iter(f"{{{BPMN_NS}}}lane")]
    lane_ids = [lid for lid in lane_ids if lid]
    elem_lane: Dict[str, str] = {}
    for lane in root.iter(f"{{{BPMN_NS}}}lane"):
        lid = lane.get("id")
        for ref in lane.findall(f"{{{BPMN_NS}}}flowNodeRef"):
            if ref.text:
                elem_lane[ref.text.strip()] = lid

    # process id (for pool detection)
    proc = root.find(f"{{{BPMN_NS}}}process")
    proc_id = proc.get("id") if proc is not None else None

    # ---- 1. restack lanes to LANE_H, update pool ----
    # order lanes by current y
    laned = [(lid, gx(lid)) for lid in lane_ids if lid in bounds_el]
    laned.sort(key=lambda t: t[1][1])
    lane_band: Dict[str, Tuple[float, float]] = {}
    if laned:
        pool_top = laned[0][1][1]
        lane_x   = laned[0][1][0]
        lane_w   = laned[0][1][2]
        for i, (lid, (lx, ly, lw, lh)) in enumerate(laned):
            new_y = pool_top + i * _LANE_H
            sx(lid, lane_x, new_y, lane_w, _LANE_H)
            lane_band[lid] = (new_y, _LANE_H)
        # resize pool to wrap all lanes
        if proc_id and proc_id in bounds_el:
            px, py, pw, ph = gx(proc_id)
            sx(proc_id, px, pool_top, pw, len(laned) * _LANE_H)

    # ---- 2. classify nodes ----
    task_tags = {"task","userTask","serviceTask","scriptTask","manualTask",
                 "sendTask","receiveTask","businessRuleTask","callActivity","subProcess"}
    gw_tags = {"exclusiveGateway","parallelGateway","inclusiveGateway",
               "eventBasedGateway","complexGateway"}
    ev_tags = {"startEvent","endEvent","intermediateThrowEvent",
               "intermediateCatchEvent","boundaryEvent"}
    kind: Dict[str, str] = {}
    gw_name: Dict[str, str] = {}
    start_id: Optional[str] = None
    for el in root.iter():
        eid = el.get("id")
        if not eid:
            continue
        lt = _local(el.tag)
        if lt in task_tags: kind[eid] = "task"
        elif lt in gw_tags:
            kind[eid] = "gateway"
            gw_name[eid] = el.get("name") or ""
        elif lt in ev_tags:
            kind[eid] = "event"
            if lt == "startEvent" and start_id is None:
                start_id = eid
        elif lt == "dataObjectReference": kind[eid] = "data"

    # ---- 3. data object -> (task, side) ----
    dor_info: Dict[str, Tuple[str, str]] = {}
    for dia in root.iter(f"{{{BPMN_NS}}}dataInputAssociation"):
        s = dia.find(f"{{{BPMN_NS}}}sourceRef"); t = dia.find(f"{{{BPMN_NS}}}targetRef")
        if s is not None and t is not None and s.text and t.text:
            dor_info[s.text.strip()] = (t.text.strip(), "in")
    for doa in root.iter(f"{{{BPMN_NS}}}dataOutputAssociation"):
        s = doa.find(f"{{{BPMN_NS}}}sourceRef"); t = doa.find(f"{{{BPMN_NS}}}targetRef")
        if s is not None and t is not None and s.text and t.text:
            dor_info[t.text.strip()] = (s.text.strip(), "out")

    # ---- 4. X-REFLOW: lay flow nodes left-to-right along the FORWARD chain.
    # The loop-back edges (sequenceFlow id ending in "_loopback") are excluded
    # so the walk never cycles. This guarantees the freshly-inserted merge +
    # decision gateways each get their own slot and never overlap a task. ----
    def _is_loop(fid: Optional[str]) -> bool:
        return bool(fid) and fid.endswith("_loopback")

    fwd: Dict[str, List[str]] = {}
    for sf in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        s, t = sf.get("sourceRef"), sf.get("targetRef")
        if s and t and not _is_loop(sf.get("id")):
            fwd.setdefault(s, []).append(t)

    flow_nodes = [e for e in kind if kind[e] in ("task", "gateway", "event")
                  and e in bounds_el]
    chain: List[str] = []
    seen: set = set()
    cur = start_id if start_id in bounds_el else None
    while cur and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = next((n for n in fwd.get(cur, []) if n not in seen), None)
    # any flow node not reached by the walk: append in current-x order
    for e in sorted(flow_nodes, key=lambda e: gx(e)[0]):
        if e not in seen:
            chain.append(e); seen.add(e)

    X0 = min((gx(e)[0] for e in flow_nodes), default=160.0)
    cursor = X0
    x_of: Dict[str, float] = {}
    for eid in chain:
        if eid not in bounds_el:
            continue
        k = kind.get(eid)
        w = TASK_W if k == "task" else (GW_SIZE if k == "gateway" else EVENT)
        x_of[eid] = cursor
        # a decision (named) gateway carries a label to its RIGHT, so leave
        # room before the next node or the label overruns the next column.
        extra = 92 if (k == "gateway" and gw_name.get(eid, "").strip()) else 0
        cursor += w + extra + COL_GAP

    # ---- 4b. position flow nodes: x from the reflow, y on the lane row ----
    for eid in flow_nodes:
        k = kind[eid]
        lane = elem_lane.get(eid)
        if lane not in lane_band:
            continue
        ltop, _ = lane_band[lane]
        nx = x_of.get(eid, gx(eid)[0])
        row_centre = ltop + _TASK_Y_OFF + TASK_H / 2
        if k == "task":
            sx(eid, nx, ltop + _TASK_Y_OFF, TASK_W, TASK_H)
        elif k == "gateway":
            sx(eid, nx, row_centre - GW_SIZE / 2, GW_SIZE, GW_SIZE)
        else:  # event
            sx(eid, nx, row_centre - EVENT / 2, EVENT, EVENT)
        if k == "gateway":
            # Decision gateways carry a question; merge gateways are unlabelled.
            # Default (Signavio) puts the label to the RIGHT of the diamond;
            # compact (Celonis) puts it BELOW so it reads cleanly and never
            # crowds the next node.
            if gw_name.get(eid, "").strip():
                if compact:
                    _label_below_gateway(shape_el[eid], gx(eid))
                else:
                    _label_right(shape_el[eid], gx(eid))
            else:
                _remove_label(shape_el[eid])
        elif k == "event":
            # keep the event's name label glued directly BELOW the event so it
            # can't drift far from the shape (a stale label position would
            # otherwise float off and inflate the canvas).
            _label_below(shape_el[eid], gx(eid))

    # ---- 5. data objects: input top-left, output bottom-right ----
    # map dataObjectReference id -> its process element (to centre its label)
    dref_el: Dict[str, ET.Element] = {}
    for e in root.iter(f"{{{BPMN_NS}}}dataObjectReference"):
        if e.get("id"):
            dref_el[e.get("id")] = e

    groups: Dict[Tuple[str, str], List[str]] = {}
    for dor, (task, side) in dor_info.items():
        groups.setdefault((task, side), []).append(dor)

    for (task, side), dors in groups.items():
        if task not in bounds_el:
            continue
        tx, ty, tw, th = gx(task)
        lane = elem_lane.get(task)
        ltop = lane_band.get(lane, (ty - TASK_Y_OFF, LANE_H))[0]
        dors = sorted(dors)
        if side == "in":
            row_y = ltop + IN_Y_OFF
            start_x = tx                     # left aligned to task
        else:
            row_y = ltop + OUT_Y_OFF
            n = len(dors)
            total = n * DO_W + (n - 1) * DO_GAP
            start_x = tx + tw - total        # right aligned to task
        for i, dor in enumerate(dors):
            if dor in bounds_el:
                dxp = start_x + i * (DO_W + DO_GAP)
                sx(dor, dxp, row_y, DO_W, DO_H)
                # v1.2.21 - text CENTRED in the box. Signavio top-aligns text
                # inside a BPMNLabel bounds, so to make a ~2-line name appear
                # vertically centred we place a band centred on the box's
                # vertical midpoint (height ~ 2 lines), full box width for
                # horizontal centring.
                lbl = shape_el[dor].find(f"{{{BPMN_DI_NS}}}BPMNLabel/{{{DC_NS}}}Bounds")
                if lbl is not None:
                    band_h = 34
                    lbl.set("x", str(int(round(dxp))))
                    lbl.set("y", str(int(round(row_y + DO_H / 2 - band_h / 2))))
                    lbl.set("width", str(DO_W))
                    lbl.set("height", str(band_h))
                # v1.2.25 - Signavio left-aligns a data-object label by default;
                # emit a signavioLabel align=center so the name sits CENTRED in
                # the box like a task name (the omgdc bounds give it the room).
                _center_do_label(dref_el.get(dor))

    # ---- 5b. size lanes + pool to FIT the reflowed content (shrink as well as
    #          grow) so the canvas has no large empty right-hand margin. ----
    if laned:
        rights: List[float] = []
        for eid in flow_nodes:
            x, y, w, h = gx(eid)
            rights.append(x + w)
            if kind[eid] == "gateway" and gw_name.get(eid, "").strip():
                rights.append(x + w + 8 + 84)      # right-side label extent
            if kind[eid] == "event":
                rights.append(x + w / 2 + 60)      # name label centred below
        for (_task, _side), _dors in groups.items():
            for _dor in _dors:
                if _dor in bounds_el:
                    x, y, w, h = gx(_dor)
                    rights.append(x + w)
        if rights:
            # Keep the leftmost node (e.g. the start event) INSIDE the lane:
            # lower the lane-left only when a node would otherwise fall outside
            # it (happens when the DI was grid-fabricated, e.g. a Visio import).
            # For Excel/transcript flows X0 >= lane_x already, so this is a no-op.
            left_anchor = min(lane_x, X0 - 40.0)
            new_w = max(max(rights) + 90 - left_anchor, 700.0)   # fit content, min 700
            for lid, (ly, _lh) in lane_band.items():
                sx(lid, left_anchor, ly, new_w, _LANE_H)
            for _pool_ref in (proc_id, "Process_1_pool"):
                if _pool_ref and _pool_ref in bounds_el:
                    sx(_pool_ref, left_anchor, pool_top, new_w, len(laned) * _LANE_H)

    # ---- 6. anchors ----
    def edges_of(eid):
        x, y, w, h = gx(eid)
        return {"left": (x, y + h/2), "right": (x + w, y + h/2),
                "top": (x + w/2, y), "bottom": (x + w/2, y + h),
                "cx": x + w/2, "cy": y + h/2,
                "x": x, "y": y, "w": w, "h": h}

    def set_wp(edge_el, pts):
        for wp in edge_el.findall(f"{{{DI_NS}}}waypoint"):
            edge_el.remove(wp)
        for (px, py) in pts:
            ET.SubElement(edge_el, f"{{{DI_NS}}}waypoint",
                          {"x": str(int(round(px))), "y": str(int(round(py)))})

    # ---- 7. sequence flows ----
    seq = {}
    for sf in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        seq[sf.get("id")] = (sf.get("sourceRef"), sf.get("targetRef"))

    for edge in root.iter(f"{{{BPMN_DI_NS}}}BPMNEdge"):
        be = edge.get("bpmnElement")
        if be not in seq:
            continue
        s, t = seq[be]
        if s not in bounds_el or t not in bounds_el:
            continue
        se, te = edges_of(s), edges_of(t)

        # Loop-back (negative branch): decision gateway G -> merge gateway M,
        # which sits to the LEFT (M is before the task, G after it). Route it
        # up out of G's top into the clear loop band near the lane top, run
        # backwards across to above M, then drop into M's top. Never crosses
        # the task row or the data objects (which sit between M and G).
        if _is_loop(be):
            # Run the backward edge in the loop band of the UPPER of the two
            # lanes (so it clears both when the merge is a lane above the
            # decision, e.g. Assign in one lane, Attempt in the next).
            bs = lane_band.get(elem_lane.get(s))
            bt = lane_band.get(elem_lane.get(t))
            tops = [b[0] for b in (bs, bt) if b]
            base = min(tops) if tops else (min(se["y"], te["y"]) - _LOOP_Y_OFF)
            band_y = base + _LOOP_Y_OFF
            set_wp(edge, [se["top"], (se["cx"], band_y),
                          (te["cx"], band_y), te["top"]])
            continue

        same_lane = (elem_lane.get(s) == elem_lane.get(t)) or abs(se["cy"] - te["cy"]) < 6

        if same_lane:
            # straight horizontal: right edge -> left edge
            set_wp(edge, [se["right"], te["left"]])
            continue

        # Cross-lane: route AROUND the data objects via the column gap and
        # the task SIDE edges (data objects sit above / below the task, so
        # the left/right edges at mid-height are always clear). This matches
        # the user's hand-drawn correction - the connector exits the side of
        # the source facing the target, runs vertically in the gap between
        # the two columns, then enters the side of the target facing the
        # source. Pure right-angle, never crosses a data-object box.
        if abs(se["cx"] - te["cx"]) > 40:
            if te["cx"] > se["cx"]:               # target to the right
                exitp  = se["right"]
                enterp = te["left"]
                gapx   = (se["x"] + se["w"] + te["x"]) / 2
            else:                                 # target to the left
                exitp  = se["left"]
                enterp = te["right"]
                gapx   = (te["x"] + te["w"] + se["x"]) / 2
            set_wp(edge, [exitp, (gapx, exitp[1]), (gapx, enterp[1]), enterp])
        else:
            # same column - drop/raise straight through the centre
            if te["cy"] > se["cy"]:
                mid = (se["bottom"][1] + te["top"][1]) / 2
                set_wp(edge, [se["bottom"], (se["cx"], mid), (te["cx"], mid), te["top"]])
            else:
                mid = (se["top"][1] + te["bottom"][1]) / 2
                set_wp(edge, [se["top"], (se["cx"], mid), (te["cx"], mid), te["bottom"]])

    # ---- 8. data associations ----
    assoc = {}
    for dia in root.iter(f"{{{BPMN_NS}}}dataInputAssociation"):
        s = dia.find(f"{{{BPMN_NS}}}sourceRef"); t = dia.find(f"{{{BPMN_NS}}}targetRef")
        if dia.get("id") and s is not None and t is not None:
            assoc[dia.get("id")] = (s.text.strip(), t.text.strip(), "in")
    for doa in root.iter(f"{{{BPMN_NS}}}dataOutputAssociation"):
        s = doa.find(f"{{{BPMN_NS}}}sourceRef"); t = doa.find(f"{{{BPMN_NS}}}targetRef")
        if doa.get("id") and s is not None and t is not None:
            assoc[doa.get("id")] = (t.text.strip(), s.text.strip(), "out")

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    for edge in root.iter(f"{{{BPMN_DI_NS}}}BPMNEdge"):
        be = edge.get("bpmnElement")
        if be not in assoc:
            continue
        dor, task, side = assoc[be]
        if dor not in bounds_el or task not in bounds_el:
            continue
        de, te = edges_of(dor), edges_of(task)
        if side == "in":
            ax = clamp(de["cx"], te["x"] + 8, te["x"] + te["w"] - 8)
            set_wp(edge, [(de["cx"], de["y"] + de["h"]), (ax, te["y"])])
        else:
            ax = clamp(de["cx"], te["x"] + 8, te["x"] + te["w"] - 8)
            set_wp(edge, [(ax, te["y"] + te["h"]), (de["cx"], de["y"])])

    return ET.tostring(root, encoding="unicode")


def _label_above(shape: ET.Element, bounds) -> None:
    """Ensure the shape has a BPMNLabel positioned just above it."""
    x, y, w, h = bounds
    lbl = shape.find(f"{{{BPMN_DI_NS}}}BPMNLabel")
    if lbl is None:
        lbl = ET.SubElement(shape, f"{{{BPMN_DI_NS}}}BPMNLabel")
    lb = lbl.find(f"{{{DC_NS}}}Bounds")
    if lb is None:
        lb = ET.SubElement(lbl, f"{{{DC_NS}}}Bounds")
    lb.set("x", str(int(round(x + w / 2 - 60))))
    lb.set("y", str(int(round(y - 28))))
    lb.set("width", "120")
    lb.set("height", "24")


def _label_right(shape: ET.Element, bounds) -> None:
    """Place a gateway's label to the RIGHT of the diamond in a narrow box so
    a short question (e.g. 'Delivery successful?') wraps to ~2 lines, matching
    the hand-tuned reference. Vertically centred on the diamond."""
    x, y, w, h = bounds
    lbl = shape.find(f"{{{BPMN_DI_NS}}}BPMNLabel")
    if lbl is None:
        lbl = ET.SubElement(shape, f"{{{BPMN_DI_NS}}}BPMNLabel")
    lb = lbl.find(f"{{{DC_NS}}}Bounds")
    if lb is None:
        lb = ET.SubElement(lbl, f"{{{DC_NS}}}Bounds")
    lb.set("x", str(int(round(x + w + 8))))
    lb.set("y", str(int(round(y + h / 2 - 15))))
    lb.set("width", "84")
    lb.set("height", "30")


def _label_below_gateway(shape: ET.Element, bounds) -> None:
    """Place a gateway's question label centred just BELOW the diamond (wraps
    to ~2 lines). Used for the compact / Celonis layout so the label sits under
    the gateway instead of crowding the node to its right. Creates the
    BPMNLabel if the gateway doesn't have one yet."""
    x, y, w, h = bounds
    lbl = shape.find(f"{{{BPMN_DI_NS}}}BPMNLabel")
    if lbl is None:
        lbl = ET.SubElement(shape, f"{{{BPMN_DI_NS}}}BPMNLabel")
    lb = lbl.find(f"{{{DC_NS}}}Bounds")
    if lb is None:
        lb = ET.SubElement(lbl, f"{{{DC_NS}}}Bounds")
    lb.set("x", str(int(round(x + w / 2 - 50))))
    lb.set("y", str(int(round(y + h + 6))))
    lb.set("width", "100")
    lb.set("height", "28")


def _remove_label(shape: ET.Element) -> None:
    """Strip any BPMNLabel (merge gateways are unlabelled)."""
    lbl = shape.find(f"{{{BPMN_DI_NS}}}BPMNLabel")
    if lbl is not None:
        shape.remove(lbl)


def _center_do_label(dref_elem) -> None:
    """Add a Signavio label hint so the data-object NAME is centred (both
    axes) inside the box, like a task name. Without it Signavio left-aligns
    data-object labels. Idempotent - replaces any existing signavioLabel."""
    if dref_elem is None:
        return
    ext = dref_elem.find(f"{{{BPMN_NS}}}extensionElements")
    if ext is None:
        ext = ET.Element(f"{{{BPMN_NS}}}extensionElements")
        dref_elem.insert(0, ext)
    for sl in ext.findall(f"{{{SIG_NS}}}signavioLabel"):
        ext.remove(sl)
    ET.SubElement(ext, f"{{{SIG_NS}}}signavioLabel",
                  {"align": "center", "ref": "text_name",
                   "valign": "middle", "x": "0.0", "y": "0.0"})


def _label_below(shape: ET.Element, bounds) -> None:
    """Reposition an existing label to sit centred just BELOW the shape (used
    for start/end events so their name never drifts off into empty canvas).
    Does nothing if the shape has no label."""
    x, y, w, h = bounds
    lbl = shape.find(f"{{{BPMN_DI_NS}}}BPMNLabel")
    if lbl is None:
        return
    lb = lbl.find(f"{{{DC_NS}}}Bounds")
    if lb is None:
        lb = ET.SubElement(lbl, f"{{{DC_NS}}}Bounds")
    lb.set("x", str(int(round(x + w / 2 - 60))))
    lb.set("y", str(int(round(y + h + 4))))
    lb.set("width", "120")
    lb.set("height", "24")
