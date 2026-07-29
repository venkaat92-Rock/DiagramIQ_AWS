"""Convert Process Discovery Excel sheets to Signavio-compatible BPMN 2.0 XML."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import List, Tuple

import openpyxl

# ---------------------------------------------------------------------------
# BPMN namespaces (same as bpmn_converter.py)
# ---------------------------------------------------------------------------
BPMN   = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC     = "http://www.omg.org/spec/DD/20100524/DC"
DI     = "http://www.omg.org/spec/DD/20100524/DI"
XSI    = "http://www.w3.org/2001/XMLSchema-instance"

# Standard Signavio element sizes
_DIM = {
    "startEvent":             (36, 36),
    "endEvent":               (36, 36),
    "exclusiveGateway":       (50, 50),
    "parallelGateway":        (50, 50),
    "inclusiveGateway":       (50, 50),
    "task":                   (120, 80),
    "userTask":               (120, 80),
    "serviceTask":            (120, 80),
    "scriptTask":             (120, 80),
    "sendTask":               (120, 80),
    "receiveTask":            (120, 80),
    "manualTask":             (120, 80),
    "businessRuleTask":       (120, 80),
    "intermediateCatchEvent": (36, 36),
    "intermediateThrowEvent": (36, 36),
    "dataObjectReference":    (100, 70),
}
_DEFAULT_DIM = (120, 80)

# Layout constants
_LANE_H      = 250      # height per lane (room for data objects above tasks)
_LANE_PAD    = 20       # vertical padding inside lane
_H_GAP       = 100      # horizontal gap between elements
_START_X     = 180      # left margin (room for lane labels)
_POOL_LABEL  = 40       # pool label width on the left
_LANE_LABEL  = 30       # lane label width
_EXT_POOL_H  = 60       # collapsed external pool strip height
_EXT_POOL_GAP = 10      # vertical gap between external pool and main pool


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class ProcessStep:
    activity: str
    description: str
    participant: str
    it_systems: str
    input_doc: str
    output_doc: str
    templates: str
    dependency: str
    frequency: str
    pain_points: str


@dataclass
class ProcessDefinition:
    process_name: str
    trigger_event: str
    success_outcome: str
    failure_outcome: str
    steps: list[ProcessStep] = field(default_factory=list)


@dataclass
class BPMNNode:
    id: str
    tag: str          # startEvent, task, exclusiveGateway, endEvent, ...
    name: str
    lane: str = ""
    external_participant: str = ""   # non-empty → node needs a messageFlow


@dataclass
class BPMNFlow:
    id: str
    source: str
    target: str
    name: str = ""     # condition label
    is_default: bool = False


@dataclass
class DataObjectCatalog:
    objects: list[tuple] = field(default_factory=list)  # (dor_id, do_id, display_name)
    lookup:  dict  = field(default_factory=dict)         # normalised_name -> (dor_id, do_id)


@dataclass
class TaskDataObj:
    """Per-task data object: each task gets its own input/output data object."""
    task_id: str
    dor_id: str
    do_id: str
    name: str
    kind: str       # "input" or "output"
    lane: str = ""  # lane the task belongs to


# ---------------------------------------------------------------------------
# Excel parser
# ---------------------------------------------------------------------------
_FAIL_KW = re.compile(
    r"must succeed|failure|unsuccessful|terminat"
    r"|not (successful|completed|eligible|required|needed|approved|possible|engaged)"
    r"|error|denied|denies|deny|rejected|reject|invalid|unable"
    r"|poor\s+\w*\s*(grade|rating|score)",
    re.IGNORECASE,
)


def _cell(row, idx: int) -> str:
    """Return cell value as stripped string, or empty string."""
    try:
        v = row[idx].value
    except IndexError:
        return ""
    return str(v).strip() if v is not None else ""


def parse_process_discovery_excel(file_path: str) -> ProcessDefinition:
    """Parse a Process Discovery Excel template into a ProcessDefinition."""
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(min_row=1, max_row=ws.max_row))
    if not rows:
        raise ValueError("Excel file is empty.")

    # --- Metadata (rows 1-4) ---
    proc_name = _cell(rows[0], 1) if len(rows) > 0 else "Process"
    trigger   = _cell(rows[1], 1) if len(rows) > 1 else "Start"
    success   = _cell(rows[2], 1) if len(rows) > 2 else "Process completed"
    failure   = _cell(rows[3], 1) if len(rows) > 3 else ""

    # Trim long outcomes to first sentence/line
    success = success.split("\n")[0].split("(")[0].strip()
    failure = failure.split("\n")[0].strip()

    # --- Find header row ---
    header_idx = None
    for i, row in enumerate(rows):
        a = _cell(row, 0).lower()
        if "activity" in a or "subprocess" in a or "process" in a and "activity" in a:
            header_idx = i
            break
    if header_idx is None:
        header_idx = 5

    # --- Parse steps ---
    steps: list[ProcessStep] = []
    for row in rows[header_idx + 1:]:
        activity = _cell(row, 0)
        if not activity:
            continue
        steps.append(ProcessStep(
            activity=activity,
            description=_cell(row, 1),
            participant=_normalize_participant(_cell(row, 2)),
            it_systems=_cell(row, 3),
            input_doc=_cell(row, 4),
            output_doc=_cell(row, 5),
            templates=_cell(row, 6),
            dependency=_cell(row, 7),
            frequency=_cell(row, 8),
            pain_points=_cell(row, 9),
        ))

    if not steps:
        raise ValueError("No process steps found in the Excel file.")

    wb.close()
    return ProcessDefinition(
        process_name=proc_name or "Process",
        trigger_event=trigger or "Start",
        success_outcome=success or "Process completed",
        failure_outcome=failure,
        steps=steps,
    )


def _normalize_participant(raw: str) -> str:
    """Collapse complex participant strings to a primary role."""
    if not raw:
        return "Default"
    raw = re.sub(r"\(.*?\)", "", raw).strip()
    if "/" in raw:
        raw = raw.split("/")[0].strip()
    return raw or "Default"


# ---------------------------------------------------------------------------
# External-participant helpers
# ---------------------------------------------------------------------------
_EXT_KEYWORDS = frozenset([
    "supplier", "vendor", "customer", "client",
    "external", "partner", "third party", "third-party",
])


def _is_external_participant(name: str) -> bool:
    """Return True if the participant name represents an external/collapsed pool."""
    low = name.lower()
    return any(kw in low for kw in _EXT_KEYWORDS)


def _find_internal_lane(nodes: list[BPMNNode], proc: ProcessDefinition) -> str:
    """Return the most recent internal (non-external) lane seen so far."""
    for n in reversed(nodes):
        if n.lane and not _is_external_participant(n.lane):
            return n.lane
    for step in proc.steps:
        if not _is_external_participant(step.participant):
            return step.participant
    return "Default"


def _make_event_label(primary: str, fallback: str, suffix: str, words: int = 4) -> str:
    """Build a concise event label from a longer description."""
    src = primary.strip() if primary.strip() else fallback
    parts = src.split()[:words]
    label = " ".join(parts) or f"{fallback} {suffix}"
    if len(label) > 30:
        label = label[:27] + "..."
    return label


# ---------------------------------------------------------------------------
# Data object catalogue
# ---------------------------------------------------------------------------
def _collect_data_objects(steps: list[ProcessStep]) -> DataObjectCatalog:
    """Collect all unique input/output documents across steps."""
    cat = DataObjectCatalog()
    counter = 0
    for step in steps:
        for raw in (step.input_doc, step.output_doc):
            if not raw or not raw.strip():
                continue
            key = raw.strip().lower()
            if key not in cat.lookup:
                counter += 1
                dor_id = f"dor_{counter}"
                do_id  = f"do_{counter}"
                cat.lookup[key] = (dor_id, do_id)
                cat.objects.append((dor_id, do_id, raw.strip()))
    return cat


# ---------------------------------------------------------------------------
# Gateway inference
# ---------------------------------------------------------------------------
def _infer_gateways(proc: ProcessDefinition) -> Tuple[list[BPMNNode], list[BPMNFlow]]:
    """
    Build BPMN nodes and flows from the process definition.

    Gateway strategy (two-gateway rework loop):
      [merge XOR] → [task] → [split XOR] --Yes--> next step
           ↑                       |
           └───── No ──────────────┘
    - External-participant steps become intermediate message events.
    """
    nodes: list[BPMNNode] = []
    flows: list[BPMNFlow] = []
    flow_counter = 0

    def _fid() -> str:
        nonlocal flow_counter
        flow_counter += 1
        return f"flow_{flow_counter}"

    # --- Start event ---
    start_id = "startEvent_1"
    first_lane = proc.steps[0].participant if proc.steps else "Default"
    if _is_external_participant(first_lane):
        first_lane = next(
            (s.participant for s in proc.steps if not _is_external_participant(s.participant)),
            "Default",
        )
    start_label = _make_event_label(proc.trigger_event, proc.process_name, "started")
    nodes.append(BPMNNode(start_id, "startEvent", start_label, first_lane))

    prev_id = start_id
    last_main_task = start_id   # tracks the last real task (not gateway)
    gateway_count = 0

    _CATCH_VERBS = ("receive", "accept", "get", "collect", "obtain", "retrieve", "await")

    for i, step in enumerate(proc.steps):
        task_id = f"task_{i + 1}"
        dep = step.dependency
        is_ext = _is_external_participant(step.participant)
        lane = step.participant if not is_ext else _find_internal_lane(nodes, proc)

        # --- Conditional: two-gateway rework loop around previous task ---
        if _FAIL_KW.search(dep) and last_main_task != start_id:
            gateway_count += 1

            # Find lane of the previous main task
            prev_task_lane = next(
                (n.lane for n in nodes if n.id == last_main_task), lane)

            # 1. MERGE gateway (converging) — inserted before last_main_task
            merge_id = f"gateway_merge_{gateway_count}"
            nodes.append(BPMNNode(merge_id, "exclusiveGateway", "", prev_task_lane))

            # Redirect all flows targeting last_main_task → merge gateway
            for f in flows:
                if f.target == last_main_task:
                    f.target = merge_id

            # merge → last_main_task
            flows.append(BPMNFlow(_fid(), merge_id, last_main_task))

            # 2. SPLIT gateway (diverging) — after last_main_task
            split_id = f"gateway_{gateway_count}"
            gw_name = _dep_to_question(dep)
            nodes.append(BPMNNode(split_id, "exclusiveGateway", gw_name, prev_task_lane))

            # last_main_task → split
            flows.append(BPMNFlow(_fid(), last_main_task, split_id))

            # "No": split → merge (loop back for rework)
            flows.append(BPMNFlow(_fid(), split_id, merge_id, name="No"))

            # "Yes" continues from split gateway
            prev_id = split_id

        else:
            # --- Normal step on the main (happy) path ---
            if is_ext:
                is_catch = any(step.activity.lower().startswith(v) for v in _CATCH_VERBS)
                tag = "intermediateCatchEvent" if is_catch else "intermediateThrowEvent"
                nodes.append(BPMNNode(task_id, tag, step.activity, lane,
                                      external_participant=step.participant))
            else:
                nodes.append(BPMNNode(task_id, "task", step.activity, lane))

            # Attach "Yes" label if coming from a split gateway
            flow_name = "Yes" if prev_id.startswith("gateway_") else ""
            flows.append(BPMNFlow(_fid(), prev_id, task_id, name=flow_name))
            prev_id = task_id
            last_main_task = task_id

    # --- Success end event ---
    end_id = "endEvent_1"
    last_lane = proc.steps[-1].participant if proc.steps else "Default"
    if _is_external_participant(last_lane):
        last_lane = _find_internal_lane(nodes, proc)
    end_label = _make_event_label(proc.success_outcome, proc.process_name, "complete")
    nodes.append(BPMNNode(end_id, "endEvent", end_label, last_lane))

    flow_name = "Yes" if prev_id.startswith("gateway_") else ""
    flows.append(BPMNFlow(_fid(), prev_id, end_id, name=flow_name))

    return nodes, flows


def _dep_to_question(dep: str) -> str:
    """Convert a dependency string into a concise gateway question.

    'Procurement engagement not required' → 'Engagement required?'
    'Governance Forum denies strategy'    → 'Strategy endorsed?'
    'Supplier receives Poor audit grade'  → 'Audit grade acceptable?'
    """
    low = dep.lower().strip()

    # ---- "not <word>" → strip negation, keep the positive question ----
    m = re.search(r"(.+?)\s+not\s+(\w+)", dep, re.IGNORECASE)
    if m:
        subject = m.group(1).strip()
        pos_word = m.group(2).strip()
        # Take last 2 meaningful words of subject + positive word
        subj_words = [w for w in subject.split()
                      if w.lower() not in ("is", "are", "was", "were", "in", "the", "a")]
        short = " ".join(subj_words[-2:])
        return f"{short} {pos_word}?".strip()

    # ---- "denies/deny/denied/rejected" → "X endorsed/approved?" ----
    if re.search(r"deni|reject", low):
        parts = dep.strip().split()
        obj_words = [w for w in parts
                     if not re.match(r"(?i)^(denies?|denied|deny|rejects?|rejected|forum|governance|the|a)$", w)]
        label = " ".join(obj_words[-2:]) if obj_words else "Decision"
        return f"{label} endorsed?"

    # ---- "poor grade/rating" → "X acceptable?" ----
    if re.search(r"poor|invalid", low):
        parts = dep.strip().split()
        obj_words = [w for w in parts
                     if not re.match(r"(?i)^(poor|receives?|supplier|the|a|an)$", w)]
        label = " ".join(obj_words[-2:]) if obj_words else "Result"
        return f"{label} acceptable?"

    # ---- Fallback ----
    short = dep.strip()[:28]
    return short.rstrip("?") + "?"


def _make_gateway_question(activity: str) -> str:
    """Turn a task name into a gateway question."""
    words = activity.strip().split()
    if len(words) >= 2:
        verb = words[0].lower()
        obj = " ".join(words[1:])
        past = {
            "verify": "verified", "validate": "validated", "check": "checked",
            "approve": "approved", "confirm": "confirmed", "review": "reviewed",
            "process": "processed", "complete": "completed", "accept": "accepted",
            "authenticate": "authenticated", "authorize": "authorized",
            "assess": "assessed", "submit": "submitted", "classify": "classified",
        }
        past_verb = past.get(verb, verb + "ed" if not verb.endswith("e") else verb + "d")
        return f"{obj} {past_verb}?"
    return f"{activity}?"


def _extract_failure_label(dep: str, global_failure: str, idx: int) -> str:
    """Extract a short failure label from dependency text or global failure outcome."""
    for kw in ["terminat", "failure", "error", "not completed", "not successful"]:
        if kw in dep.lower():
            for part in re.split(r"[;.]", dep):
                if kw in part.lower():
                    label = part.strip()
                    if len(label) > 60:
                        label = label[:57] + "..."
                    return label
    if global_failure:
        lines = global_failure.split("\n")
        label = lines[min(idx - 1, len(lines) - 1)].strip()
        if len(label) > 60:
            label = label[:57] + "..."
        return label
    return f"Process failed ({idx})"


# ---------------------------------------------------------------------------
# Layout engine (lane-aware)
# ---------------------------------------------------------------------------
def _layout_with_lanes(
    nodes: list[BPMNNode],
    flows: list[BPMNFlow],
    external_pool_names: list[str] = (),
) -> Tuple[dict, dict, list[str], float, float]:
    """
    Compute positions for all elements in a lane-aware left-to-right layout.

    Returns:
        positions:    {node_id: (x, y, w, h)}
        lane_bounds:  {lane_name: (x, y, w, h)}
        lane_order:   [internal lane names in order]
        total_width:  canvas width
        main_pool_y:  y-coordinate of the top of the main pool
    """
    # Determine lane order — external participants are NOT lanes in the main pool
    lane_order: list[str] = []
    for n in nodes:
        if n.lane and n.lane not in lane_order and not _is_external_participant(n.lane):
            lane_order.append(n.lane)
    if not lane_order:
        lane_order = ["Default"]

    # Offset the main pool downward to make room for external collapsed pools above
    n_ext = len(external_pool_names)
    main_pool_y = 20 + n_ext * (_EXT_POOL_H + _EXT_POOL_GAP)

    lane_y_top = {lane: main_pool_y + i * _LANE_H for i, lane in enumerate(lane_order)}

    # Identify backward edges (No-loop flows) that create cycles
    backward_ids = set()
    for f in flows:
        if f.name == "No" and f.target.startswith("gateway_merge_"):
            backward_ids.add(f.id)

    # Build adjacency for topological sort — excluding backward edges
    out_edges: dict[str, list[str]] = {n.id: [] for n in nodes}
    in_edges:  dict[str, list[str]] = {n.id: [] for n in nodes}
    for f in flows:
        if f.id in backward_ids:
            continue   # skip cycle-creating edges
        if f.source in out_edges:
            out_edges[f.source].append(f.target)
        if f.target in in_edges:
            in_edges[f.target].append(f.source)

    # Kahn's topological sort
    in_degree = {n.id: len(in_edges[n.id]) for n in nodes}
    queue = [nid for nid, deg in in_degree.items() if deg == 0]
    topo_order: list[str] = []
    while queue:
        queue.sort(key=lambda nid: next(i for i, n in enumerate(nodes) if n.id == nid))
        nid = queue.pop(0)
        topo_order.append(nid)
        for tgt in out_edges.get(nid, []):
            in_degree[tgt] -= 1
            if in_degree[tgt] == 0:
                queue.append(tgt)

    for n in nodes:
        if n.id not in topo_order:
            topo_order.append(n.id)

    # Assign column index
    col: dict[str, int] = {}
    for nid in topo_order:
        preds = in_edges.get(nid, [])
        col[nid] = (max(col.get(p, 0) for p in preds) + 1) if preds else 0

    max_col = max(col.values()) if col else 0

    # Compute element positions
    positions: dict[str, tuple] = {}
    for n in nodes:
        w, h = _DIM.get(n.tag, _DEFAULT_DIM)
        x = _START_X + col[n.id] * (_H_GAP + 120)
        lane = n.lane if n.lane in lane_y_top else lane_order[0]
        # Tasks in the lower portion; upper area reserved for data objects
        y_center = lane_y_top[lane] + _LANE_H * 0.68
        y = y_center - h / 2
        positions[n.id] = (x, y, w, h)

    total_width = _START_X + (max_col + 1) * (_H_GAP + 120) + _H_GAP

    # Lane bounds
    lane_bounds: dict[str, tuple] = {}
    for lane in lane_order:
        ly = lane_y_top[lane]
        lane_bounds[lane] = (
            _POOL_LABEL + _LANE_LABEL,
            ly,
            total_width - _POOL_LABEL - _LANE_LABEL,
            _LANE_H,
        )

    return positions, lane_bounds, lane_order, total_width, main_pool_y


# ---------------------------------------------------------------------------
# BPMN XML builder
# ---------------------------------------------------------------------------
def build_bpmn_from_excel(file_path: str, process_name: str = "") -> str:
    """Read a Process Discovery Excel file and return Signavio-compatible BPMN 2.0 XML."""
    proc = parse_process_discovery_excel(file_path)
    if process_name:
        proc.process_name = process_name

    # Build flow graph
    nodes, flows = _infer_gateways(proc)

    # Per-task data objects (each task gets its own, no sharing across lanes)
    task_dos: list[TaskDataObj] = []
    _do_ctr = 0
    node_ids_tmp = {n.id for n in nodes}
    node_lane_tmp = {n.id: n.lane for n in nodes}
    for i, step in enumerate(proc.steps):
        nid = f"task_{i + 1}"
        if nid not in node_ids_tmp:
            continue
        if step.input_doc.strip():
            _do_ctr += 1
            task_dos.append(TaskDataObj(
                nid, f"dor_{_do_ctr}", f"do_{_do_ctr}",
                step.input_doc.strip(), "input", node_lane_tmp.get(nid, ""),
            ))
        if step.output_doc.strip():
            _do_ctr += 1
            task_dos.append(TaskDataObj(
                nid, f"dor_{_do_ctr}", f"do_{_do_ctr}",
                step.output_doc.strip(), "output", node_lane_tmp.get(nid, ""),
            ))

    # Find unique external participant names (order-preserving)
    ext_names: list[str] = list(dict.fromkeys(
        n.external_participant for n in nodes if n.external_participant
    ))

    # Layout — external pool names shift the main pool down
    positions, lane_bounds, lane_order, total_width, main_pool_y = _layout_with_lanes(
        nodes, flows, external_pool_names=ext_names
    )

    # --- Register namespaces ---
    ET.register_namespace("bpmn",   BPMN)
    ET.register_namespace("bpmndi", BPMNDI)
    ET.register_namespace("dc",     DC)
    ET.register_namespace("di",     DI)
    ET.register_namespace("xsi",    XSI)

    # --- Root: definitions ---
    defs = ET.Element(f"{{{BPMN}}}definitions", {
        "id": "Definitions_1",
        "targetNamespace": "http://diagramiq",
        f"{{{XSI}}}schemaLocation": (
            "http://www.omg.org/spec/BPMN/20100524/MODEL "
            "http://www.omg.org/spec/BPMN/2.0/20100501/BPMN20.xsd"
        ),
    })

    process_id = "Process_1"
    has_ext = bool(ext_names)
    ext_pool_ids: dict[str, str] = {}
    mf_node_map:  dict[str, str] = {}   # node_id -> messageFlow id

    # --- Collaboration (only when external pools exist) ---
    if has_ext:
        collab = ET.SubElement(defs, f"{{{BPMN}}}collaboration", {"id": "Collaboration_1"})
        ET.SubElement(collab, f"{{{BPMN}}}participant", {
            "id": "pool_main",
            "name": proc.process_name,
            "processRef": process_id,
        })
        for i, ext in enumerate(ext_names):
            pid = f"pool_ext_{i + 1}"
            ext_pool_ids[ext] = pid
            ET.SubElement(collab, f"{{{BPMN}}}participant", {"id": pid, "name": ext})

        # Message flows
        mf_counter = 0
        for n in nodes:
            if not n.external_participant:
                continue
            mf_counter += 1
            mf_id = f"mf_{mf_counter}"
            mf_node_map[n.id] = mf_id
            ext_pid = ext_pool_ids[n.external_participant]
            if n.tag == "intermediateCatchEvent":
                src_ref, tgt_ref = ext_pid, n.id
            else:
                src_ref, tgt_ref = n.id, ext_pid
            ET.SubElement(collab, f"{{{BPMN}}}messageFlow", {
                "id": mf_id,
                "sourceRef": src_ref,
                "targetRef": tgt_ref,
            })

    # --- Process element ---
    process = ET.SubElement(defs, f"{{{BPMN}}}process", {
        "id": process_id,
        "name": proc.process_name,
        "isExecutable": "false",
    })

    # --- Lane set ---
    lane_id_map: dict[str, str] = {}
    if lane_order:
        lane_set = ET.SubElement(process, f"{{{BPMN}}}laneSet", {"id": "LaneSet_1"})
        for i, lane_name in enumerate(lane_order):
            lane_id = f"Lane_{i + 1}"
            lane_id_map[lane_name] = lane_id
            lane_el = ET.SubElement(lane_set, f"{{{BPMN}}}lane", {
                "id": lane_id,
                "name": lane_name,
            })
            for n in nodes:
                if n.lane == lane_name:
                    ET.SubElement(lane_el, f"{{{BPMN}}}flowNodeRef").text = n.id

    # --- Build incoming/outgoing maps ---
    incoming: dict[str, list[str]] = {n.id: [] for n in nodes}
    outgoing: dict[str, list[str]] = {n.id: [] for n in nodes}
    for f in flows:
        if f.source in outgoing:
            outgoing[f.source].append(f.id)
        if f.target in incoming:
            incoming[f.target].append(f.id)

    # --- Flow nodes ---
    node_map = {n.id: n for n in nodes}
    for n in nodes:
        attribs: dict[str, str] = {"id": n.id, "name": n.name}

        # Set default flow on exclusive gateways (prefer "Yes" / first outgoing)
        if n.tag == "exclusiveGateway":
            for f in flows:
                if f.source == n.id and (f.name == "Yes" or not f.name):
                    attribs["default"] = f.id
                    break

        elem = ET.SubElement(process, f"{{{BPMN}}}{n.tag}", attribs)

        # Message event definitions for intermediate events
        if n.tag in ("intermediateCatchEvent", "intermediateThrowEvent"):
            ET.SubElement(elem, f"{{{BPMN}}}messageEventDefinition",
                          {"id": f"msgDef_{n.id}"})

        for fid in incoming[n.id]:
            ET.SubElement(elem, f"{{{BPMN}}}incoming").text = fid
        for fid in outgoing[n.id]:
            ET.SubElement(elem, f"{{{BPMN}}}outgoing").text = fid

    # --- Sequence flows ---
    for f in flows:
        fattribs: dict[str, str] = {
            "id": f.id,
            "sourceRef": f.source,
            "targetRef": f.target,
        }
        if f.name:
            fattribs["name"] = f.name
        flow_el = ET.SubElement(process, f"{{{BPMN}}}sequenceFlow", fattribs)
        if f.name and not f.is_default:
            src_node = node_map.get(f.source)
            if src_node and "Gateway" in src_node.tag:
                cond = ET.SubElement(flow_el, f"{{{BPMN}}}conditionExpression", {
                    f"{{{XSI}}}type": "bpmn:tFormalExpression",
                })
                cond.text = f.name

    # --- Per-task data objects and 1:1 associations ---
    data_assocs: list[tuple] = []
    for tdo in task_dos:
        ET.SubElement(process, f"{{{BPMN}}}dataObjectReference", {
            "id": tdo.dor_id, "name": tdo.name, "dataObjectRef": tdo.do_id,
        })
        ET.SubElement(process, f"{{{BPMN}}}dataObject", {"id": tdo.do_id})
        if tdo.kind == "input":
            aid = f"dia_{tdo.dor_id}"
            assoc = ET.SubElement(process, f"{{{BPMN}}}dataInputAssociation", {"id": aid})
            ET.SubElement(assoc, f"{{{BPMN}}}sourceRef").text = tdo.dor_id
            ET.SubElement(assoc, f"{{{BPMN}}}targetRef").text = tdo.task_id
            data_assocs.append((aid, tdo.dor_id, tdo.task_id))
        else:
            aid = f"doa_{tdo.dor_id}"
            assoc = ET.SubElement(process, f"{{{BPMN}}}dataOutputAssociation", {"id": aid})
            ET.SubElement(assoc, f"{{{BPMN}}}sourceRef").text = tdo.task_id
            ET.SubElement(assoc, f"{{{BPMN}}}targetRef").text = tdo.dor_id
            data_assocs.append((aid, tdo.task_id, tdo.dor_id))

    # --- BPMNDI section ---
    _build_bpmndi(
        defs, process_id, nodes, flows, positions,
        lane_bounds, lane_order, lane_id_map, total_width,
        main_pool_y, ext_names, ext_pool_ids, mf_node_map, task_dos,
        has_ext, data_assocs,
    )

    # --- Serialize ---
    ET.indent(defs, space="  ")
    xml_str = ET.tostring(defs, encoding="unicode", xml_declaration=False)
    out = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

    # v1.2.17 - deterministic layout pass: place input data objects above
    # their task, output data objects below, and route lane-crossing flows
    # as clean orthogonal connectors. Never fails the build.
    try:
        from .bpmn_layout_fixer import fix_layout
        fixed = fix_layout(out)
        if not fixed.lstrip().startswith("<?xml"):
            fixed = '<?xml version="1.0" encoding="UTF-8"?>\n' + fixed
        out = fixed
    except Exception as exc:
        import sys as _sys
        print(f"[layout-fixer] skipped: {exc}", file=_sys.stderr)

    return out


def _build_bpmndi(
    defs: ET.Element,
    process_id: str,
    nodes: list[BPMNNode],
    flows: list[BPMNFlow],
    positions: dict,
    lane_bounds: dict,
    lane_order: list[str],
    lane_id_map: dict[str, str],
    total_width: float,
    main_pool_y: float,
    ext_names: list[str],
    ext_pool_ids: dict[str, str],
    mf_node_map: dict[str, str],
    task_dos: list = (),
    has_ext: bool = False,
    data_assocs: list[tuple] = (),
):
    """Build the complete BPMNDI diagram interchange section."""
    diagram = ET.SubElement(defs, f"{{{BPMNDI}}}BPMNDiagram", {"id": "BPMNDiagram_1"})

    # Plane references the collaboration when external pools exist
    plane_ref = "Collaboration_1" if has_ext else process_id
    plane = ET.SubElement(diagram, f"{{{BPMNDI}}}BPMNPlane", {
        "id": "BPMNPlane_1",
        "bpmnElement": plane_ref,
    })

    pool_width = int(total_width - _POOL_LABEL)

    # --- External collapsed pool shapes (above the main pool) ---
    for i, ext in enumerate(ext_names):
        ext_y = 20 + i * (_EXT_POOL_H + _EXT_POOL_GAP)
        sh = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNShape", {
            "id": f"BPMNShape_{ext_pool_ids[ext]}",
            "bpmnElement": ext_pool_ids[ext],
            "isHorizontal": "true",
        })
        ET.SubElement(sh, f"{{{DC}}}Bounds", {
            "x": str(int(_POOL_LABEL)),
            "y": str(int(ext_y)),
            "width": str(pool_width),
            "height": str(int(_EXT_POOL_H)),
        })

    # --- Main pool shape ---
    if lane_order:
        pool_h = len(lane_order) * _LANE_H
        main_pool_bpmn = "pool_main" if has_ext else process_id
        pool_shape = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNShape", {
            "id": "BPMNShape_Pool_1",
            "bpmnElement": main_pool_bpmn,
            "isHorizontal": "true",
        })
        ET.SubElement(pool_shape, f"{{{DC}}}Bounds", {
            "x": str(int(_POOL_LABEL)),
            "y": str(int(main_pool_y)),
            "width": str(pool_width),
            "height": str(int(pool_h)),
        })

    # --- Lane shapes ---
    for lane_name in lane_order:
        lid = lane_id_map[lane_name]
        lx, ly, lw, lh = lane_bounds[lane_name]
        lane_shape = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNShape", {
            "id": f"BPMNShape_{lid}",
            "bpmnElement": lid,
            "isHorizontal": "true",
        })
        ET.SubElement(lane_shape, f"{{{DC}}}Bounds", {
            "x": str(int(lx)),
            "y": str(int(ly)),
            "width": str(int(lw)),
            "height": str(int(lh)),
        })

    # --- Element shapes (with BPMNLabel for events & gateways) ---
    _LABEL_TAGS = frozenset([
        "startEvent", "endEvent", "exclusiveGateway",
        "intermediateCatchEvent", "intermediateThrowEvent",
        "parallelGateway", "inclusiveGateway",
    ])
    for n in nodes:
        x, y, w, h = positions[n.id]
        shape_attribs = {
            "id": f"BPMNShape_{n.id}",
            "bpmnElement": n.id,
        }
        # Gateways need isMarkerVisible for the X marker in Signavio
        if "Gateway" in n.tag:
            shape_attribs["isMarkerVisible"] = "true"
        shape = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNShape", shape_attribs)
        ET.SubElement(shape, f"{{{DC}}}Bounds", {
            "x": str(int(x)),
            "y": str(int(y)),
            "width": str(int(w)),
            "height": str(int(h)),
        })
        # Signavio requires explicit BPMNLabel bounds on events & gateways
        if n.name and n.tag in _LABEL_TAGS:
            label_w = max(len(n.name) * 7.0, 40.0)
            label_h = 22.0
            label_x = x + w / 2.0 - label_w / 2.0
            label_y = y + h + 5.0       # place label BELOW the element
            lbl = ET.SubElement(shape, f"{{{BPMNDI}}}BPMNLabel")
            ET.SubElement(lbl, f"{{{DC}}}Bounds", {
                "x": str(round(label_x, 2)),
                "y": str(round(label_y, 2)),
                "width": str(round(label_w, 2)),
                "height": str(round(label_h, 2)),
            })

    # --- Data object shapes (inside swimlane, above their own task) ---
    do_positions: dict[str, tuple] = {}
    do_w, do_h = 88, 70
    # Track per-task placement to offset input left, output right
    _task_do_idx: dict[str, int] = {}
    for tdo in task_dos:
        if tdo.task_id not in positions:
            continue
        tx, ty, tw, th = positions[tdo.task_id]
        idx = _task_do_idx.get(tdo.task_id, 0)
        _task_do_idx[tdo.task_id] = idx + 1
        if tdo.kind == "input":
            # Input: above-left of task
            do_x = int(tx - 5)
        else:
            # Output: above-right of task
            do_x = int(tx + tw - do_w + 5)
        # If both input+output on same task, offset the second one further right
        if idx > 0:
            do_x = int(tx + tw + 10)
        do_y = int(ty - do_h - 12)
        do_positions[tdo.dor_id] = (do_x, do_y, do_w, do_h)
        sh = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNShape", {
            "id": f"BPMNShape_{tdo.dor_id}",
            "bpmnElement": tdo.dor_id,
        })
        ET.SubElement(sh, f"{{{DC}}}Bounds", {
            "x": str(do_x), "y": str(do_y),
            "width": str(do_w), "height": str(do_h),
        })
        lbl = ET.SubElement(sh, f"{{{BPMNDI}}}BPMNLabel")
        ET.SubElement(lbl, f"{{{DC}}}Bounds", {
            "x": str(do_x + 4), "y": str(do_y + 14),
            "width": str(do_w - 8), "height": str(do_h - 18),
        })

    # --- Sequence flow edges (with BPMNLabel for named flows) ---
    node_pos = {n.id: positions[n.id] for n in nodes}
    node_map = {n.id: n for n in nodes}
    for f in flows:
        edge = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNEdge", {
            "id": f"BPMNEdge_{f.id}",
            "bpmnElement": f.id,
        })
        _add_waypoints(edge, f, node_pos, node_map)
        # Named flows (Yes/No) — label positioned NEAR the gateway (source)
        if f.name and f.source in node_pos:
            sx, sy, sw, sh_ = node_pos[f.source]
            src_cx = sx + sw / 2
            src_cy = sy + sh_ / 2
            fl_w = max(len(f.name) * 8.0, 28.0)
            fl_h = 18.0

            # Check if target is to the left (backward / No loop)
            tgt_left = False
            if f.target in node_pos:
                tx2 = node_pos[f.target][0]
                tgt_left = (tx2 + node_pos[f.target][2] / 2) < src_cx - 10

            if tgt_left:
                # "No" loop — label below the gateway
                fl_x = src_cx - fl_w / 2
                fl_y = sy + sh_ + 8
            else:
                # "Yes" forward — label just right of the gateway, above the line
                fl_x = sx + sw + 5
                fl_y = src_cy - fl_h - 4

            fl_lbl = ET.SubElement(edge, f"{{{BPMNDI}}}BPMNLabel")
            ET.SubElement(fl_lbl, f"{{{DC}}}Bounds", {
                "x": str(round(fl_x, 2)),
                "y": str(round(fl_y, 2)),
                "width": str(round(fl_w, 2)),
                "height": str(round(fl_h, 2)),
            })

    # --- Message flow edges ---
    for n in nodes:
        if not n.external_participant or n.id not in mf_node_map:
            continue
        mf_id = mf_node_map[n.id]
        ext_idx = ext_names.index(n.external_participant)
        ext_y = 20 + ext_idx * (_EXT_POOL_H + _EXT_POOL_GAP)

        nx, ny, nw, nh = positions[n.id]
        node_cx  = nx + nw / 2
        node_top = ny

        ext_cx     = _POOL_LABEL + pool_width / 2
        ext_bottom = ext_y + _EXT_POOL_H

        edge = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNEdge", {
            "id": f"BPMNEdge_{mf_id}",
            "bpmnElement": mf_id,
        })
        if n.tag == "intermediateCatchEvent":
            wp1 = (ext_cx,   ext_bottom)
            wp2 = (node_cx,  node_top)
        else:
            wp1 = (node_cx,  node_top)
            wp2 = (ext_cx,   ext_bottom)
        for wx, wy in (wp1, wp2):
            ET.SubElement(edge, f"{{{DI}}}waypoint",
                          {"x": str(int(wx)), "y": str(int(wy))})

    # --- Data association edges (connect tasks ↔ data objects) ---
    for assoc_id, src_id, tgt_id in data_assocs:
        # Determine source/target positions (could be task or data object)
        if src_id in node_pos:
            sx, sy, sw, sh_ = node_pos[src_id]
        elif src_id in do_positions:
            sx, sy, sw, sh_ = do_positions[src_id]
        else:
            continue
        if tgt_id in node_pos:
            tx, ty, tw, th_ = node_pos[tgt_id]
        elif tgt_id in do_positions:
            tx, ty, tw, th_ = do_positions[tgt_id]
        else:
            continue

        edge = ET.SubElement(plane, f"{{{BPMNDI}}}BPMNEdge", {
            "id": f"BPMNEdge_{assoc_id}",
            "bpmnElement": assoc_id,
        })
        # Source bottom-center → target top-center
        wp_sx = sx + sw / 2
        wp_sy = sy + sh_
        wp_tx = tx + tw / 2
        wp_ty = ty
        ET.SubElement(edge, f"{{{DI}}}waypoint",
                      {"x": str(int(wp_sx)), "y": str(int(wp_sy))})
        ET.SubElement(edge, f"{{{DI}}}waypoint",
                      {"x": str(int(wp_tx)), "y": str(int(wp_ty))})


def _add_waypoints(
    edge: ET.Element,
    flow: BPMNFlow,
    positions: dict,
    node_map: dict[str, BPMNNode],
):
    """Add orthogonal waypoints for a sequence flow."""
    sx, sy, sw, sh = positions.get(flow.source, (0, 0, 120, 80))
    tx, ty, tw, th = positions.get(flow.target, (0, 0, 120, 80))

    src_cx, src_cy = sx + sw / 2, sy + sh / 2
    tgt_cx, tgt_cy = tx + tw / 2, ty + th / 2

    def wp(x, y):
        ET.SubElement(edge, f"{{{DI}}}waypoint", {
            "x": str(int(x)), "y": str(int(y)),
        })

    # --- Backward loop (e.g. "No" from gateway back to previous task) ---
    # Route BELOW the elements so the loop line is visually distinct
    if tgt_cx < src_cx - 10:
        loop_y = max(sy + sh, ty + th) + 60   # 60px below the lower element
        wp(src_cx, sy + sh)       # exit bottom of source
        wp(src_cx, loop_y)        # go down
        wp(tgt_cx, loop_y)        # go left at the low level
        wp(tgt_cx, ty + th)       # go up to bottom of target
        return

    dx = tgt_cx - src_cx
    dy = tgt_cy - src_cy

    if abs(dx) >= abs(dy):
        if dx >= 0:
            x1, y1 = sx + sw, src_cy
            x4, y4 = tx,       tgt_cy
        else:
            x1, y1 = sx,       src_cy
            x4, y4 = tx + tw,  tgt_cy
    else:
        if dy >= 0:
            x1, y1 = src_cx, sy + sh
            x4, y4 = tgt_cx, ty
        else:
            x1, y1 = src_cx, sy
            x4, y4 = tgt_cx, ty + th

    if abs(y1 - y4) < 5:
        wp(x1, y1); wp(x4, y4)
    elif abs(x1 - x4) < 5:
        wp(x1, y1); wp(x4, y4)
    else:
        if abs(dx) >= abs(dy):
            mid_x = (x1 + x4) / 2
            wp(x1, y1); wp(mid_x, y1); wp(mid_x, y4); wp(x4, y4)
        else:
            mid_y = (y1 + y4) / 2
            wp(x1, y1); wp(x1, mid_y); wp(x4, mid_y); wp(x4, y4)
