"""BPMN 2.0 validator implementing Signavio best-practice rules."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set

from .bpmn_parser import BPMNElement, BPMNModel


class Severity(Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass
class ValidationIssue:
    rule_id: int
    severity: Severity
    category: str
    element_id: Optional[str]
    element_name: Optional[str]
    message: str

    def __str__(self) -> str:
        name_part = f" '{self.element_name}'" if self.element_name else ""
        elem_part = f" [{self.element_id}{name_part}]" if self.element_id else ""
        return f"[{self.severity.value}] Rule {self.rule_id} ({self.category}): {self.message}{elem_part}"

    def display_row(self) -> tuple:
        """Return (severity, rule_id, category, element, message) for treeview display."""
        elem = self.element_name or self.element_id or "-"
        return (self.severity.value, str(self.rule_id), self.category, elem, self.message)


def validate_bpmn(model: BPMNModel) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    issues.extend(_check_structure(model))
    issues.extend(_check_naming(model))
    issues.extend(_check_layout(model))
    return issues


# ---------------------------------------------------------------------------
# PROCESS STRUCTURE RULES
# ---------------------------------------------------------------------------

def _check_structure(model: BPMNModel) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    # Rule 1: Deadlock detection
    issues.extend(_check_deadlocks(model))

    # Rule 3: Multi-merge — task with multiple incoming flows (no gateway join)
    for elem in model.get_tasks():
        if len(elem.incoming) > 1:
            issues.append(ValidationIssue(
                rule_id=3, severity=Severity.ERROR, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Activity has multiple incoming flows without a gateway join (multi-merge).",
            ))

    # Rule 4: Task with multiple outgoing flows — must use gateway
    for elem in model.get_tasks():
        if len(elem.outgoing) > 1:
            issues.append(ValidationIssue(
                rule_id=4, severity=Severity.ERROR, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Activity has multiple outgoing flows. Use an explicit gateway split.",
            ))

    # Rule 5: Intermediate events with multiple outgoing flows
    for elem in model.get_events():
        if "intermediate" in elem.tag_local().lower() and len(elem.outgoing) > 1:
            issues.append(ValidationIssue(
                rule_id=5, severity=Severity.ERROR, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Intermediate event has multiple outgoing flows. Use a gateway split.",
            ))

    # Rule 6: Start event with multiple outgoing flows
    for elem in model.get_events():
        if elem.tag_local() == "startEvent" and len(elem.outgoing) > 1:
            issues.append(ValidationIssue(
                rule_id=6, severity=Severity.ERROR, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Start event has multiple outgoing flows. Use an explicit gateway.",
            ))

    # Rule 8: Gateway acting as both split and join
    for elem in model.get_gateways():
        if len(elem.incoming) > 1 and len(elem.outgoing) > 1:
            issues.append(ValidationIssue(
                rule_id=8, severity=Severity.ERROR, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Gateway acts as both split and join. Split into two separate gateways.",
            ))

    # Rule 11: Every process needs at least one start and one end event
    for proc_id in model.processes:
        proc_elems = [e for e in model.elements.values() if e.process_id == proc_id]
        tags = [e.tag_local() for e in proc_elems]
        if "startEvent" not in tags:
            issues.append(ValidationIssue(
                rule_id=11, severity=Severity.ERROR, category="Process Structure",
                element_id=proc_id, element_name=None,
                message=f"Process '{proc_id}' has no start event.",
            ))
        if "endEvent" not in tags:
            issues.append(ValidationIssue(
                rule_id=11, severity=Severity.ERROR, category="Process Structure",
                element_id=proc_id, element_name=None,
                message=f"Process '{proc_id}' has no end event.",
            ))

    # Rule 14: Message flows must be between separate pools only
    for mf in model.get_message_flows():
        src = model.elements.get(mf.source_ref or "")
        tgt = model.elements.get(mf.target_ref or "")
        if src and tgt and src.process_id and tgt.process_id:
            if src.process_id == tgt.process_id:
                issues.append(ValidationIssue(
                    rule_id=14, severity=Severity.ERROR, category="Process Structure",
                    element_id=mf.id, element_name=None,
                    message="Message flow connects elements in the same pool/process. Use a sequence flow instead.",
                ))

    # Rule 17: Meaningless gateways (only one outgoing path)
    for elem in model.get_gateways():
        if len(elem.outgoing) == 1 and len(elem.incoming) <= 1:
            issues.append(ValidationIssue(
                rule_id=17, severity=Severity.WARNING, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Gateway has only one outgoing flow (meaningless split gateway).",
            ))
        elif len(elem.outgoing) == 0:
            issues.append(ValidationIssue(
                rule_id=17, severity=Severity.WARNING, category="Process Structure",
                element_id=elem.id, element_name=elem.name,
                message="Gateway has no outgoing flows.",
            ))

    # Rule 18: Message flows to/from gateways are invalid
    for mf in model.get_message_flows():
        src = model.elements.get(mf.source_ref or "")
        tgt = model.elements.get(mf.target_ref or "")
        if src and src.kind == "gateway":
            issues.append(ValidationIssue(
                rule_id=18, severity=Severity.ERROR, category="Process Structure",
                element_id=mf.id, element_name=None,
                message="Message flow cannot originate from a gateway.",
            ))
        if tgt and tgt.kind == "gateway":
            issues.append(ValidationIssue(
                rule_id=18, severity=Severity.ERROR, category="Process Structure",
                element_id=mf.id, element_name=None,
                message="Message flow cannot target a gateway.",
            ))

    # Rule 19: Only one start event per process
    for proc_id in model.processes:
        proc_elems = [e for e in model.elements.values() if e.process_id == proc_id]
        start_events = [e for e in proc_elems if e.tag_local() == "startEvent"]
        if len(start_events) > 1:
            issues.append(ValidationIssue(
                rule_id=19, severity=Severity.ERROR, category="Process Structure",
                element_id=proc_id, element_name=None,
                message=f"Process '{proc_id}' has {len(start_events)} start events. Only one is allowed.",
            ))

    # Rule 20: Only one start event per subprocess
    subprocesses = [e for e in model.elements.values() if e.tag_local() == "subProcess"]
    for sp in subprocesses:
        sp_children = [e for e in model.elements.values() if e.process_id == sp.id]
        sp_starts = [e for e in sp_children if e.tag_local() == "startEvent"]
        if len(sp_starts) > 1:
            issues.append(ValidationIssue(
                rule_id=20, severity=Severity.ERROR, category="Process Structure",
                element_id=sp.id, element_name=sp.name,
                message=f"Subprocess has {len(sp_starts)} start events. Only one is allowed.",
            ))

    return issues


def _check_deadlocks(model: BPMNModel) -> List[ValidationIssue]:
    """Rule 1: Detect elements with no path to an end event."""
    issues = []

    # Build forward adjacency from sequence flows
    outgoing: Dict[str, List[str]] = {}
    for elem in model.elements.values():
        if elem.kind == "flow" and elem.source_ref and elem.target_ref:
            outgoing.setdefault(elem.source_ref, []).append(elem.target_ref)

    for proc_id in model.processes:
        proc_elements = {
            e.id for e in model.elements.values()
            if e.process_id == proc_id and e.kind in ("task", "gateway", "event")
        }
        if not proc_elements:
            continue

        end_events = {
            e.id for e in model.elements.values()
            if e.process_id == proc_id and e.tag_local() == "endEvent"
        }
        if not end_events:
            continue

        # Reverse BFS from end events
        reverse: Dict[str, List[str]] = {e_id: [] for e_id in proc_elements}
        for elem in model.elements.values():
            if elem.kind == "flow" and elem.source_ref and elem.target_ref:
                if elem.target_ref in reverse and elem.source_ref in reverse:
                    reverse[elem.target_ref].append(elem.source_ref)

        reachable: Set[str] = set()
        queue = list(end_events & proc_elements)
        reachable.update(queue)
        while queue:
            curr = queue.pop()
            for pred in reverse.get(curr, []):
                if pred not in reachable:
                    reachable.add(pred)
                    queue.append(pred)

        start_events = {
            e.id for e in model.elements.values()
            if e.process_id == proc_id and e.tag_local() == "startEvent"
        }
        unreachable = proc_elements - reachable - end_events - start_events
        for elem_id in unreachable:
            elem = model.elements.get(elem_id)
            if elem:
                issues.append(ValidationIssue(
                    rule_id=1, severity=Severity.ERROR, category="Process Structure",
                    element_id=elem_id, element_name=elem.name,
                    message="Element has no path to an end event (potential deadlock).",
                ))

    return issues


# ---------------------------------------------------------------------------
# NAMING RULES
# ---------------------------------------------------------------------------

_VERB_NOUN_RE = re.compile(r'^[A-Z][a-z]+(\s+\w+)+$')


def _check_naming(model: BPMNModel) -> List[ValidationIssue]:
    issues = []

    # Rule 33: All activities/tasks must have a name
    for elem in model.get_tasks():
        if not elem.name.strip():
            issues.append(ValidationIssue(
                rule_id=33, severity=Severity.ERROR, category="Naming",
                element_id=elem.id, element_name=None,
                message="Activity/task has no name.",
            ))

    # Rule 35: All events must have a name
    for elem in model.get_events():
        if elem.tag_local() not in ("boundaryEvent",) and not elem.name.strip():
            issues.append(ValidationIssue(
                rule_id=35, severity=Severity.WARNING, category="Naming",
                element_id=elem.id, element_name=None,
                message=f"{elem.tag_local()} has no name.",
            ))

    # Rule 36: All lanes must have a name
    for elem in model.get_lanes():
        if not elem.name.strip():
            issues.append(ValidationIssue(
                rule_id=36, severity=Severity.WARNING, category="Naming",
                element_id=elem.id, element_name=None,
                message="Lane/role has no name.",
            ))

    # Rule 37: Activities should follow verb+noun naming convention
    for elem in model.get_tasks():
        name = elem.name.strip()
        if name and not _VERB_NOUN_RE.match(name):
            issues.append(ValidationIssue(
                rule_id=37, severity=Severity.INFO, category="Naming",
                element_id=elem.id, element_name=name,
                message=f"Activity name '{name}' may not follow verb+noun convention (e.g. 'Approve Invoice').",
            ))

    # Rule 38: XOR gateways should have a named condition question
    for elem in model.get_gateways():
        if "exclusive" in elem.tag_local().lower() and len(elem.outgoing) > 1:
            if not elem.name.strip():
                issues.append(ValidationIssue(
                    rule_id=38, severity=Severity.INFO, category="Naming",
                    element_id=elem.id, element_name=None,
                    message="Exclusive gateway with multiple outgoing flows should have a name (decision question).",
                ))

    return issues


# ---------------------------------------------------------------------------
# LAYOUT RULES
# ---------------------------------------------------------------------------

def _check_layout(model: BPMNModel) -> List[ValidationIssue]:
    issues = []

    # Rule 21: Overlapping nodes (only if DI info is present)
    positioned = [
        e for e in model.elements.values()
        if e.width > 0 and e.height > 0 and e.kind in ("task", "gateway", "event")
    ]
    for i, a in enumerate(positioned):
        for b in positioned[i + 1:]:
            ax2, ay2 = a.x + a.width, a.y + a.height
            bx2, by2 = b.x + b.width, b.y + b.height
            if a.x < bx2 and ax2 > b.x and a.y < by2 and ay2 > b.y:
                issues.append(ValidationIssue(
                    rule_id=21, severity=Severity.ERROR, category="Layout",
                    element_id=a.id, element_name=a.name,
                    message=f"Element overlaps with '{b.name or b.id}'.",
                ))

    # Rule 22: Maximum element count — suggest subprocesses
    non_flow = [
        e for e in model.elements.values()
        if e.kind not in ("flow", "message_flow", "other")
    ]
    if len(non_flow) > 50:
        issues.append(ValidationIssue(
            rule_id=22, severity=Severity.WARNING, category="Layout",
            element_id=None, element_name=None,
            message=f"Diagram has {len(non_flow)} elements (recommended max: 50). Consider using subprocesses.",
        ))

    # Rule 29: Check minimum spacing between elements (if DI present)
    MIN_SPACING = 10.0
    for i, a in enumerate(positioned):
        for b in positioned[i + 1:]:
            ax2, ay2 = a.x + a.width, a.y + a.height
            bx2, by2 = b.x + b.width, b.y + b.height
            gap_x = max(b.x - ax2, a.x - bx2, 0)
            gap_y = max(b.y - ay2, a.y - by2, 0)
            if gap_x > 0 and gap_y > 0:
                continue  # Diagonal, skip
            gap = max(gap_x, gap_y)
            if 0 < gap < MIN_SPACING:
                issues.append(ValidationIssue(
                    rule_id=29, severity=Severity.INFO, category="Layout",
                    element_id=a.id, element_name=a.name,
                    message=f"Element is very close to '{b.name or b.id}' (gap: {gap:.0f}px). Increase spacing.",
                ))

    return issues
