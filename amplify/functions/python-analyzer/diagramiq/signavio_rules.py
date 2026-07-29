"""Comprehensive SAP Signavio BPMN 2.0 best-practice validation engine.

Six rule categories derived from Signavio's official modeling guidelines:
  1. Context & Structure (CS)  — process integrity, start/end, complexity
  2. Layout (LY)               — flow direction, overlap, spacing, screen fit
  3. Naming (NM)               — verb-object tasks, past-tense events, questions on XOR
  4. Connections & Flow (CF)    — explicit gateways, deadlocks, matching splits/joins
  5. Notation & Elements (NE)   — correct BPMN element usage, boundary events, typed tasks
  6. Architecture (AR)          — subprocess nesting, element counts, level readability
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, List, Optional, Set, Tuple

from .bpmn_parser import BPMNElement, BPMNModel


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class Severity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


CATEGORY_META = {
    "context_structure": {
        "label": "Context & Structure",
        "icon": "CS",
        "description": "Process integrity: start/end events, complexity limits, happy path",
    },
    "layout": {
        "label": "Layout",
        "icon": "LY",
        "description": "Flow direction, overlaps, spacing, screen-fit constraints",
    },
    "naming": {
        "label": "Naming",
        "icon": "NM",
        "description": "Verb-object tasks, past-tense events, gateway questions",
    },
    "connections_flow": {
        "label": "Connections & Flow",
        "icon": "CF",
        "description": "Explicit gateways, deadlocks, split/join matching",
    },
    "notation_elements": {
        "label": "Notation & Elements",
        "icon": "NE",
        "description": "Correct BPMN element usage, boundary events, typed tasks",
    },
    "architecture": {
        "label": "Architecture",
        "icon": "AR",
        "description": "Subprocess nesting, element counts, level readability",
    },
}


@dataclass
class SignavioIssue:
    rule_id: str
    severity: Severity
    category: str
    category_key: str
    element_id: Optional[str]
    element_name: Optional[str]
    message: str

    def __str__(self) -> str:
        name_part = f" '{self.element_name}'" if self.element_name else ""
        elem_part = f" [{self.element_id}{name_part}]" if self.element_id else ""
        return f"[{self.severity.value}] {self.rule_id} ({self.category}): {self.message}{elem_part}"

    def display_row(self) -> tuple:
        elem = self.element_name or self.element_id or "-"
        return (self.severity.value, self.rule_id, self.category, elem, self.message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VERB_OBJECT_RE = re.compile(r'^[A-Z][a-z]+(\s+\w+)+$')
_PAST_TENSE_RE = re.compile(r'\b\w+(ed|ied|en|ung|ted|ced)\b', re.I)
_QUESTION_RE = re.compile(r'\?$')
_GENERIC_NAME_RE = re.compile(
    r'^(Task|Event|Gateway|Node|Activity|Flow|Process)\s*\d*$', re.I)


def _issue(rule_id: str, sev: Severity, cat: str, cat_key: str,
           elem_id: Optional[str], elem_name: Optional[str], msg: str) -> SignavioIssue:
    return SignavioIssue(rule_id, sev, cat, cat_key, elem_id, elem_name, msg)


def _build_adjacency(model: BPMNModel) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Build forward and reverse adjacency from sequence flows."""
    fwd: Dict[str, List[str]] = {}
    rev: Dict[str, List[str]] = {}
    for e in model.elements.values():
        if e.kind in ("task", "gateway", "event"):
            fwd.setdefault(e.id, [])
            rev.setdefault(e.id, [])
    for e in model.elements.values():
        if e.kind == "flow" and e.source_ref and e.target_ref:
            fwd.setdefault(e.source_ref, []).append(e.target_ref)
            rev.setdefault(e.target_ref, []).append(e.source_ref)
    return fwd, rev


def _build_flow_maps(model: BPMNModel) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """Build outgoing/incoming flow-ID maps from sequenceFlow elements.

    Many BPMN files don't include <incoming>/<outgoing> child elements,
    so we derive connections from the sequenceFlow sourceRef/targetRef.
    """
    outgoing_flows: Dict[str, List[str]] = {}  # element_id -> [flow_ids]
    incoming_flows: Dict[str, List[str]] = {}  # element_id -> [flow_ids]
    for e in model.elements.values():
        if e.kind == "flow" and e.source_ref and e.target_ref:
            outgoing_flows.setdefault(e.source_ref, []).append(e.id)
            incoming_flows.setdefault(e.target_ref, []).append(e.id)
    return outgoing_flows, incoming_flows


def _effective_outgoing(elem: BPMNElement, out_map: Dict[str, List[str]]) -> List[str]:
    """Return outgoing flow IDs, using the flow map as fallback."""
    return elem.outgoing if elem.outgoing else out_map.get(elem.id, [])


def _effective_incoming(elem: BPMNElement, in_map: Dict[str, List[str]]) -> List[str]:
    """Return incoming flow IDs, using the flow map as fallback."""
    return elem.incoming if elem.incoming else in_map.get(elem.id, [])


# ═══════════════════════════════════════════════════════════════════════════
# Category 1: Context & Structure (CS)
# ═══════════════════════════════════════════════════════════════════════════

def _check_context_structure(model: BPMNModel) -> List[SignavioIssue]:
    issues: List[SignavioIssue] = []
    CAT, KEY = "Context & Structure", "context_structure"

    for proc_id in model.processes:
        proc_elems = [e for e in model.elements.values() if e.process_id == proc_id]
        tags = [e.tag_local() for e in proc_elems]

        # CS-01: Single start event
        starts = [e for e in proc_elems if e.tag_local() == "startEvent"]
        if len(starts) == 0:
            issues.append(_issue("CS-01", Severity.ERROR, CAT, KEY, proc_id, None,
                                 f"Process '{proc_id}' has no start event."))
        elif len(starts) > 1:
            issues.append(_issue("CS-01", Severity.ERROR, CAT, KEY, proc_id, None,
                                 f"Process '{proc_id}' has {len(starts)} start events; only one allowed."))

        # CS-02: Single end event
        ends = [e for e in proc_elems if e.tag_local() == "endEvent"]
        if len(ends) == 0:
            issues.append(_issue("CS-02", Severity.ERROR, CAT, KEY, proc_id, None,
                                 f"Process '{proc_id}' has no end event."))
        elif len(ends) > 1:
            issues.append(_issue("CS-02", Severity.WARNING, CAT, KEY, proc_id, None,
                                 f"Process '{proc_id}' has {len(ends)} end events; prefer a single end event."))

        # CS-03: Max 12 flow objects per process level
        flow_objects = [e for e in proc_elems
                        if e.kind in ("task", "gateway") and e.tag_local() != "subProcess"]
        if len(flow_objects) > 12:
            issues.append(_issue("CS-03", Severity.WARNING, CAT, KEY, proc_id, None,
                                 f"Process has {len(flow_objects)} flow objects (recommended max: 12). "
                                 f"Consider using subprocesses."))

        # CS-06: Every element on path from start to end (deadlock check)
        fwd, rev = _build_adjacency(model)
        end_ids = {e.id for e in ends}
        start_ids = {e.id for e in starts}
        proc_node_ids = {e.id for e in proc_elems if e.kind in ("task", "gateway", "event")}

        if end_ids and start_ids:
            # Reverse BFS from end events
            reachable_from_end: Set[str] = set()
            queue = list(end_ids & proc_node_ids)
            reachable_from_end.update(queue)
            while queue:
                cur = queue.pop()
                for pred in rev.get(cur, []):
                    if pred not in reachable_from_end and pred in proc_node_ids:
                        reachable_from_end.add(pred)
                        queue.append(pred)

            unreachable = proc_node_ids - reachable_from_end - end_ids - start_ids
            for uid in unreachable:
                elem = model.elements.get(uid)
                if elem:
                    issues.append(_issue("CS-06", Severity.ERROR, CAT, KEY,
                                         uid, elem.name,
                                         "Element has no path to an end event (potential deadlock)."))

        # CS-07: At least one task
        tasks = [e for e in proc_elems if e.kind == "task"]
        if not tasks:
            issues.append(_issue("CS-07", Severity.WARNING, CAT, KEY, proc_id, None,
                                 "Process has no tasks/activities."))

    return issues


# ═══════════════════════════════════════════════════════════════════════════
# Category 2: Layout (LY)
# ═══════════════════════════════════════════════════════════════════════════

def _check_layout(model: BPMNModel) -> List[SignavioIssue]:
    issues: List[SignavioIssue] = []
    CAT, KEY = "Layout", "layout"

    positioned = [e for e in model.elements.values()
                  if e.width > 0 and e.height > 0 and e.kind in ("task", "gateway", "event")]

    # LY-01: Flow direction should be left-to-right
    fwd, _ = _build_adjacency(model)
    backward_count = 0
    for src_id, targets in fwd.items():
        src = model.elements.get(src_id)
        if not src or src.width == 0:
            continue
        for tgt_id in targets:
            tgt = model.elements.get(tgt_id)
            if not tgt or tgt.width == 0:
                continue
            if tgt.x + tgt.width < src.x:  # target is fully left of source
                backward_count += 1
    if backward_count > 0:
        issues.append(_issue("LY-01", Severity.WARNING, CAT, KEY, None, None,
                             f"{backward_count} flow(s) go right-to-left. "
                             f"Signavio recommends left-to-right flow direction."))

    # LY-02: Overlapping elements
    for i, a in enumerate(positioned):
        for b in positioned[i + 1:]:
            ax2, ay2 = a.x + a.width, a.y + a.height
            bx2, by2 = b.x + b.width, b.y + b.height
            if a.x < bx2 and ax2 > b.x and a.y < by2 and ay2 > b.y:
                issues.append(_issue("LY-02", Severity.ERROR, CAT, KEY,
                                     a.id, a.name,
                                     f"Element overlaps with '{b.name or b.id}'."))

    # LY-03: Minimum spacing >= 30px
    MIN_SPACING = 30.0
    for i, a in enumerate(positioned):
        for b in positioned[i + 1:]:
            ax2, ay2 = a.x + a.width, a.y + a.height
            bx2, by2 = b.x + b.width, b.y + b.height
            gap_x = max(b.x - ax2, a.x - bx2, 0)
            gap_y = max(b.y - ay2, a.y - by2, 0)
            if gap_x > 0 and gap_y > 0:
                continue
            gap = max(gap_x, gap_y)
            if 0 < gap < MIN_SPACING:
                issues.append(_issue("LY-03", Severity.INFO, CAT, KEY,
                                     a.id, a.name,
                                     f"Only {gap:.0f}px from '{b.name or b.id}' "
                                     f"(Signavio recommends >= 30px spacing)."))

    # LY-04: Diagram should fit on one screen (approx 1600x900 viewport)
    if positioned:
        max_x = max(e.x + e.width for e in positioned)
        max_y = max(e.y + e.height for e in positioned)
        min_x = min(e.x for e in positioned)
        min_y = min(e.y for e in positioned)
        diagram_w = max_x - min_x
        diagram_h = max_y - min_y
        if diagram_w > 2400:
            issues.append(_issue("LY-04", Severity.INFO, CAT, KEY, None, None,
                                 f"Diagram width ({diagram_w:.0f}px) exceeds single-screen threshold. "
                                 f"Consider subprocesses to reduce width."))
        if diagram_h > 1400:
            issues.append(_issue("LY-04", Severity.INFO, CAT, KEY, None, None,
                                 f"Diagram height ({diagram_h:.0f}px) exceeds single-screen threshold. "
                                 f"Consider reducing parallel branches."))

    # LY-05: Crossing sequence flows (simplified check)
    flow_segments: List[Tuple[float, float, float, float]] = []
    for e in model.elements.values():
        if e.kind == "flow" and e.source_ref and e.target_ref:
            src = model.elements.get(e.source_ref)
            tgt = model.elements.get(e.target_ref)
            if src and tgt and src.width > 0 and tgt.width > 0:
                sx = src.x + src.width / 2
                sy = src.y + src.height / 2
                tx = tgt.x + tgt.width / 2
                ty = tgt.y + tgt.height / 2
                flow_segments.append((sx, sy, tx, ty))

    crossings = 0
    for i, (ax1, ay1, ax2, ay2) in enumerate(flow_segments):
        for bx1, by1, bx2, by2 in flow_segments[i + 1:]:
            if _segments_cross(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
                crossings += 1
    if crossings > 0:
        issues.append(_issue("LY-05", Severity.WARNING, CAT, KEY, None, None,
                             f"{crossings} sequence flow crossing(s) detected. "
                             f"Rearrange elements to avoid crossed arrows."))

    return issues


def _segments_cross(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2) -> bool:
    """Check if two line segments cross (simple CCW test)."""
    def ccw(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)

    d1 = ccw(ax1, ay1, ax2, ay2, bx1, by1)
    d2 = ccw(ax1, ay1, ax2, ay2, bx2, by2)
    d3 = ccw(bx1, by1, bx2, by2, ax1, ay1)
    d4 = ccw(bx1, by1, bx2, by2, ax2, ay2)

    if d1 * d2 < 0 and d3 * d4 < 0:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Category 3: Naming (NM)
# ═══════════════════════════════════════════════════════════════════════════

def _check_naming(model: BPMNModel) -> List[SignavioIssue]:
    issues: List[SignavioIssue] = []
    CAT, KEY = "Naming", "naming"
    out_map, in_map = _build_flow_maps(model)

    # NM-04: All tasks must have a name
    for elem in model.get_tasks():
        name = elem.name.strip()
        if not name:
            issues.append(_issue("NM-04", Severity.ERROR, CAT, KEY,
                                 elem.id, None,
                                 "Activity/task has no name."))
            continue

        # NM-01: Verb-object naming
        if not _VERB_OBJECT_RE.match(name) and not _GENERIC_NAME_RE.match(name):
            issues.append(_issue("NM-01", Severity.WARNING, CAT, KEY,
                                 elem.id, name,
                                 f"Task name '{name}' should follow verb-object convention "
                                 f"(e.g. 'Validate Invoice', not 'Invoice validation')."))

    # NM-05: All events must have a name
    for elem in model.get_events():
        if elem.tag_local() == "boundaryEvent":
            continue
        name = elem.name.strip()
        if not name:
            issues.append(_issue("NM-05", Severity.WARNING, CAT, KEY,
                                 elem.id, None,
                                 f"{elem.tag_local()} has no name."))
            continue

        # NM-02: Events should use past-tense or noun state
        if elem.tag_local() in ("endEvent", "intermediateThrowEvent", "intermediateCatchEvent"):
            if not _PAST_TENSE_RE.search(name) and not _GENERIC_NAME_RE.match(name):
                issues.append(_issue("NM-02", Severity.INFO, CAT, KEY,
                                     elem.id, name,
                                     f"Event name '{name}' should describe a state/result "
                                     f"(e.g. 'Invoice received', 'Order placed')."))

    # NM-03: XOR gateways should have a question as name
    for elem in model.get_gateways():
        eff_out = _effective_outgoing(elem, out_map)
        if "exclusive" in elem.tag_local().lower() and len(eff_out) > 1:
            name = elem.name.strip()
            if not name:
                issues.append(_issue("NM-03", Severity.WARNING, CAT, KEY,
                                     elem.id, None,
                                     "Exclusive gateway (XOR split) must have a decision question as name."))
            elif not _QUESTION_RE.search(name):
                issues.append(_issue("NM-03", Severity.INFO, CAT, KEY,
                                     elem.id, name,
                                     f"XOR gateway name '{name}' should end with '?' "
                                     f"(e.g. 'Invoice approved?')."))

    # NM-06: Lane names required
    for elem in model.get_lanes():
        if not elem.name.strip():
            issues.append(_issue("NM-06", Severity.WARNING, CAT, KEY,
                                 elem.id, None,
                                 "Lane/role has no name."))

    # NM-07: No duplicate task names
    for proc_id in model.processes:
        proc_tasks = [e for e in model.get_tasks() if e.process_id == proc_id]
        seen_names: Dict[str, str] = {}
        for t in proc_tasks:
            n = t.name.strip().lower()
            if n and not _GENERIC_NAME_RE.match(t.name.strip()):
                if n in seen_names:
                    issues.append(_issue("NM-07", Severity.WARNING, CAT, KEY,
                                         t.id, t.name,
                                         f"Duplicate task name '{t.name}' "
                                         f"(also used by {seen_names[n]})."))
                else:
                    seen_names[n] = t.id

    # NM-08: Sequence flow labels on XOR outgoing flows
    for elem in model.get_gateways():
        eff_out = _effective_outgoing(elem, out_map)
        if "exclusive" in elem.tag_local().lower() and len(eff_out) > 1:
            for flow_id in eff_out:
                flow = model.elements.get(flow_id)
                if flow and not flow.name.strip():
                    issues.append(_issue("NM-08", Severity.INFO, CAT, KEY,
                                         flow_id, None,
                                         f"Outgoing flow from XOR gateway '{elem.name or elem.id}' "
                                         f"has no label (should describe the condition)."))

    return issues


# ═══════════════════════════════════════════════════════════════════════════
# Category 4: Connections & Flow (CF)
# ═══════════════════════════════════════════════════════════════════════════

def _check_connections_flow(model: BPMNModel) -> List[SignavioIssue]:
    issues: List[SignavioIssue] = []
    CAT, KEY = "Connections & Flow", "connections_flow"

    # Build incoming/outgoing counts from sequence flows for elements that
    # may lack <incoming>/<outgoing> child elements in the XML
    outgoing_count: Dict[str, int] = {}
    incoming_count: Dict[str, int] = {}
    for e in model.elements.values():
        if e.kind == "flow" and e.source_ref and e.target_ref:
            outgoing_count[e.source_ref] = outgoing_count.get(e.source_ref, 0) + 1
            incoming_count[e.target_ref] = incoming_count.get(e.target_ref, 0) + 1

    def _n_out(elem: BPMNElement) -> int:
        return max(len(elem.outgoing), outgoing_count.get(elem.id, 0))

    def _n_in(elem: BPMNElement) -> int:
        return max(len(elem.incoming), incoming_count.get(elem.id, 0))

    # CF-01: Implicit splits from tasks (multiple outgoing without gateway)
    for elem in model.get_tasks():
        if _n_out(elem) > 1:
            issues.append(_issue("CF-01", Severity.ERROR, CAT, KEY,
                                 elem.id, elem.name,
                                 "Task has multiple outgoing flows. Use an explicit gateway split."))

    # CF-02: Implicit joins into tasks (multiple incoming without gateway)
    for elem in model.get_tasks():
        if _n_in(elem) > 1:
            issues.append(_issue("CF-02", Severity.ERROR, CAT, KEY,
                                 elem.id, elem.name,
                                 "Task has multiple incoming flows. Use an explicit gateway join."))

    # CF-04: Gateway acting as both split and join
    for elem in model.get_gateways():
        if _n_in(elem) > 1 and _n_out(elem) > 1:
            issues.append(_issue("CF-04", Severity.ERROR, CAT, KEY,
                                 elem.id, elem.name,
                                 "Gateway acts as both split and join simultaneously. "
                                 "Split into separate join and split gateways."))

    # CF-07: Split gateway should have a matching join
    fwd, _ = _build_adjacency(model)
    for elem in model.get_gateways():
        if _n_out(elem) > 1:
            gw_type = elem.tag_local()
            # Look for a matching join of same type downstream
            has_matching_join = False
            visited: Set[str] = set()
            queue = list(fwd.get(elem.id, []))
            while queue:
                nid = queue.pop()
                if nid in visited:
                    continue
                visited.add(nid)
                n = model.elements.get(nid)
                if n and n.kind == "gateway" and n.tag_local() == gw_type and len(n.incoming) > 1:
                    has_matching_join = True
                    break
                queue.extend(fwd.get(nid, []))
            if not has_matching_join:
                issues.append(_issue("CF-07", Severity.WARNING, CAT, KEY,
                                     elem.id, elem.name,
                                     f"Split gateway has no matching join of same type ({gw_type})."))

    # CF-08: Isolated elements (no connections at all)
    # Build sets of connected elements from sequence flows (since many BPMN files
    # don't include <incoming>/<outgoing> child elements on each node)
    connected_ids: Set[str] = set()
    for e in model.elements.values():
        if e.kind == "flow":
            if e.source_ref:
                connected_ids.add(e.source_ref)
            if e.target_ref:
                connected_ids.add(e.target_ref)
    for elem in model.elements.values():
        if elem.kind in ("task", "gateway", "event"):
            has_connections = (elem.incoming or elem.outgoing or elem.id in connected_ids)
            if not has_connections and elem.tag_local() != "boundaryEvent":
                issues.append(_issue("CF-08", Severity.ERROR, CAT, KEY,
                                     elem.id, elem.name,
                                     "Element is isolated (no incoming or outgoing flows)."))

    return issues


# ═══════════════════════════════════════════════════════════════════════════
# Category 5: Notation & Elements (NE)
# ═══════════════════════════════════════════════════════════════════════════

def _check_notation_elements(model: BPMNModel) -> List[SignavioIssue]:
    issues: List[SignavioIssue] = []
    CAT, KEY = "Notation & Elements", "notation_elements"
    out_map, in_map = _build_flow_maps(model)

    # NE-01: Boundary events must be attached
    for elem in model.get_events():
        if elem.tag_local() == "boundaryEvent" and not elem.attached_to:
            issues.append(_issue("NE-01", Severity.ERROR, CAT, KEY,
                                 elem.id, elem.name,
                                 "Boundary event is not attached to any activity."))

    # NE-03: Message flows between different pools
    for mf in model.get_message_flows():
        src = model.elements.get(mf.source_ref or "")
        tgt = model.elements.get(mf.target_ref or "")
        if src and tgt and src.process_id and tgt.process_id:
            if src.process_id == tgt.process_id:
                issues.append(_issue("NE-03", Severity.ERROR, CAT, KEY,
                                     mf.id, None,
                                     "Message flow connects elements in the same pool. "
                                     "Use a sequence flow instead."))

    # NE-04: Message flows to/from gateways
    for mf in model.get_message_flows():
        src = model.elements.get(mf.source_ref or "")
        tgt = model.elements.get(mf.target_ref or "")
        if src and src.kind == "gateway":
            issues.append(_issue("NE-04", Severity.ERROR, CAT, KEY,
                                 mf.id, None,
                                 "Message flow cannot originate from a gateway."))
        if tgt and tgt.kind == "gateway":
            issues.append(_issue("NE-04", Severity.ERROR, CAT, KEY,
                                 mf.id, None,
                                 "Message flow cannot target a gateway."))

    # NE-05: Start events should not have incoming flows
    for elem in model.get_events():
        if elem.tag_local() == "startEvent" and _effective_incoming(elem, in_map):
            issues.append(_issue("NE-05", Severity.ERROR, CAT, KEY,
                                 elem.id, elem.name,
                                 "Start event should not have incoming sequence flows."))

    # NE-06: End events should not have outgoing flows
    for elem in model.get_events():
        if elem.tag_local() == "endEvent" and _effective_outgoing(elem, out_map):
            issues.append(_issue("NE-06", Severity.ERROR, CAT, KEY,
                                 elem.id, elem.name,
                                 "End event should not have outgoing sequence flows."))

    # NE-07: Use typed tasks instead of generic 'task'
    for elem in model.get_tasks():
        if elem.tag_local() == "task":
            issues.append(_issue("NE-07", Severity.INFO, CAT, KEY,
                                 elem.id, elem.name,
                                 "Generic task detected. Consider using a typed task "
                                 "(userTask, serviceTask, scriptTask, etc.)."))

    return issues


# ═══════════════════════════════════════════════════════════════════════════
# Category 6: Architecture (AR)
# ═══════════════════════════════════════════════════════════════════════════

def _check_architecture(model: BPMNModel) -> List[SignavioIssue]:
    issues: List[SignavioIssue] = []
    CAT, KEY = "Architecture", "architecture"

    # AR-03: Max 50 elements per diagram
    non_flow = [e for e in model.elements.values()
                if e.kind not in ("flow", "message_flow", "other")]
    if len(non_flow) > 50:
        issues.append(_issue("AR-03", Severity.WARNING, CAT, KEY, None, None,
                             f"Diagram has {len(non_flow)} elements (recommended max: 50). "
                             f"Consider splitting into subprocesses."))

    # AR-01: Max 3 levels of subprocess nesting
    subprocesses = [e for e in model.elements.values() if e.tag_local() == "subProcess"]
    for sp in subprocesses:
        depth = _subprocess_depth(model, sp.id)
        if depth > 3:
            issues.append(_issue("AR-01", Severity.WARNING, CAT, KEY,
                                 sp.id, sp.name,
                                 f"Subprocess nesting depth is {depth} (recommended max: 3)."))

    # AR-04: Subprocess should contain 3-12 elements
    for sp in subprocesses:
        children = [e for e in model.elements.values()
                    if e.process_id == sp.id and e.kind in ("task", "gateway", "event")]
        if children and len(children) < 3:
            issues.append(_issue("AR-04", Severity.INFO, CAT, KEY,
                                 sp.id, sp.name,
                                 f"Subprocess has only {len(children)} elements "
                                 f"(recommended: 3-12). Consider inlining."))
        elif len(children) > 12:
            issues.append(_issue("AR-04", Severity.INFO, CAT, KEY,
                                 sp.id, sp.name,
                                 f"Subprocess has {len(children)} elements "
                                 f"(recommended: 3-12). Consider splitting."))

    return issues


def _subprocess_depth(model: BPMNModel, sp_id: str, depth: int = 1) -> int:
    """Recursively compute subprocess nesting depth."""
    children = [e for e in model.elements.values()
                if e.process_id == sp_id and e.tag_local() == "subProcess"]
    if not children:
        return depth
    return max(_subprocess_depth(model, c.id, depth + 1) for c in children)


# ═══════════════════════════════════════════════════════════════════════════
# Category dispatch
# ═══════════════════════════════════════════════════════════════════════════

_CATEGORY_CHECKS: Dict[str, Callable[[BPMNModel], List[SignavioIssue]]] = {
    "context_structure": _check_context_structure,
    "layout":            _check_layout,
    "naming":            _check_naming,
    "connections_flow":  _check_connections_flow,
    "notation_elements": _check_notation_elements,
    "architecture":      _check_architecture,
}


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def validate_signavio(
    model: BPMNModel,
    categories: Optional[List[str]] = None,
) -> List[SignavioIssue]:
    """Run Signavio best-practice validation.

    Args:
        model: Parsed BPMN model.
        categories: List of category keys to check (e.g. ["naming", "layout"]).
                    If None, runs ALL categories.

    Returns:
        List of SignavioIssue instances sorted by severity (ERROR first).
    """
    issues: List[SignavioIssue] = []

    if categories is None:
        cats_to_run = list(_CATEGORY_CHECKS.keys())
    else:
        cats_to_run = [c for c in categories if c in _CATEGORY_CHECKS]

    for cat_key in cats_to_run:
        check_fn = _CATEGORY_CHECKS[cat_key]
        issues.extend(check_fn(model))

    severity_order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    issues.sort(key=lambda i: severity_order.get(i.severity, 9))
    return issues
