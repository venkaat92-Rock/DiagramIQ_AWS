"""BPMN XML -> Process Discovery Excel intermediate.

New in v1.2.0. Lets the Uplift feature follow the same review-then-generate
pattern as the Notes (transcription) flow:

    [BPMN/XML upload] -> [parse to dict] -> [save Excel] -> [user reviews]
                                                              |
                                                              v
                                       [Excel re-read] -> [build uplifted BPMN]

The dict shape produced by `bpmn_to_process_dict()` matches exactly what
the AI returns from `transcription_to_excel`, so the existing
`save_excel_from_ai_response()` writer in `transcription_to_excel.py` can
be reused as-is.
"""
from __future__ import annotations

from typing import Dict, List

from .bpmn_parser import BPMNElement, BPMNModel, parse_bpmn_xml


# ---------------------------------------------------------------------------
# Element-name -> human-readable role / system inference
# ---------------------------------------------------------------------------

_SYSTEM_HINTS = (
    "SAP", "Oracle", "Salesforce", "ServiceNow", "Workday", "Outlook",
    "Teams", "Excel", "SharePoint", "Tableau", "Signavio", "JIRA",
    "Confluence", "Power BI", "Snowflake", "Anthropic", "Gemini",
)


def _infer_system(activity_name: str, description: str) -> str:
    """Best-effort: pull a system name out of the activity if it's mentioned."""
    blob = f"{activity_name} {description}".lower()
    for sys in _SYSTEM_HINTS:
        if sys.lower() in blob:
            return sys
    return ""


def _outcome_text(events: List[BPMNElement], target_kind: str) -> str:
    """Return a comma-joined string of event names for start/end events."""
    matches = [e for e in events if e.tag_local() == target_kind and e.name.strip()]
    if not matches:
        return ""
    seen: set[str] = set()
    out: List[str] = []
    for e in matches:
        nm = e.name.strip()
        if nm not in seen:
            seen.add(nm)
            out.append(nm)
    return " | ".join(out[:5])  # cap at 5 to keep cell readable


def _participant_for(elem: BPMNElement, model: BPMNModel) -> str:
    """Find the swimlane that contains an element; return its name.

    v1.2.1 - lane membership is captured by bpmn_parser as
    `lane.flow_node_refs` (a list of element IDs declared inside the lane
    via <bpmn:flowNodeRef>). Earlier versions wrongly checked
    `lane.outgoing`, so this column was always empty.
    """
    for lane in model.elements.values():
        if lane.kind != "lane":
            continue
        if elem.id in lane.flow_node_refs:
            return (lane.name or "").strip() or "Unnamed lane"
    return "Unassigned"


def _is_orphan_task(task: BPMNElement) -> bool:
    """v1.2.1 - a task with no incoming AND no outgoing sequence flows is
    disconnected from the process and should be flagged for removal."""
    return not task.incoming and not task.outgoing


def _decision_dependency(elem: BPMNElement, model: BPMNModel) -> str:
    """If the element is preceded by a gateway, capture the gateway condition."""
    for inc_id in elem.incoming:
        flow = model.elements.get(inc_id)
        if not flow or flow.kind != "flow":
            continue
        src = model.elements.get(flow.source_ref or "")
        if src and src.kind == "gateway":
            cond = (flow.name or "").strip()
            if cond:
                return cond
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bpmn_to_process_dict(xml_content: str, default_process_name: str = "Uplifted Process") -> Dict:
    """Convert BPMN XML to the same dict shape AI emits in transcription flow.

    Returns:
        {
          "process_name":         str,
          "trigger_event":        str,
          "successful_outcome":   str,
          "unsuccessful_outcome": str,
          "steps": [
            {activity, description, participant, it_systems,
             input_document, output_document, templates,
             dependency, frequency, pain_points}
          ]
        }
    """
    model = parse_bpmn_xml(xml_content)

    events = model.get_events()
    tasks  = model.get_tasks()

    # ── Metadata ───────────────────────────────────────────────────────────
    proc_name = default_process_name
    if model.processes:
        first_proc_id = model.processes[0]
        proc_elem = model.elements.get(first_proc_id)
        if proc_elem and proc_elem.name.strip():
            proc_name = proc_elem.name.strip()

    trigger    = _outcome_text(events, "startEvent")
    succ_out   = _outcome_text(events, "endEvent")
    unsucc_out = ""  # BPMN doesn't distinguish success vs failure end events

    # ── Steps (one row per task / call activity) ──────────────────────────
    # v1.2.1 - skip orphan tasks (disconnected from the flow). The AI uplift
    # prompt is told to drop them, but stripping them at this stage means the
    # user doesn't see them in the Excel review at all.
    # v1.2.5 - each step also carries `bpmn_id` so the patcher can match an
    # edited Excel row back to the exact source element in the original BPMN.
    steps: List[Dict] = []
    orphan_count = 0
    for task in sorted(tasks, key=lambda t: (t.y, t.x)):
        if _is_orphan_task(task):
            orphan_count += 1
            continue

        # Skip elements with no name and no useful info
        name = (task.name or "").strip()
        if not name and not task.id:
            continue
        if not name:
            name = f"Activity {task.tag_local()}"

        participant = _participant_for(task, model)
        dependency  = _decision_dependency(task, model)
        system      = _infer_system(name, "")

        pain = ""
        if participant == "Unassigned":
            pain = "Task not assigned to a swimlane - assign a role"

        steps.append({
            "bpmn_id":         task.id,        # v1.2.5 - round-trip key
            "activity":        name[:80],
            "description":     "",
            "participant":     participant,
            "it_systems":      system,
            "input_document":  "",
            "output_document": "",
            "templates":       "",
            "dependency":      dependency,
            "frequency":       "",
            "pain_points":     pain,
        })

    # If we removed orphans, capture the fact in the trigger field so it shows
    # up in the Excel review header for transparency.
    if orphan_count > 0:
        trigger = (trigger + f" | NOTE: {orphan_count} orphan task(s) removed from review").strip(" |")

    # If we got nothing, leave a placeholder so the Excel template still loads.
    if not steps:
        steps.append({
            "activity":        "Add tasks here",
            "description":     "BPMN file had no tasks - please populate manually.",
            "participant":     "Unknown",
            "it_systems":      "",
            "input_document":  "",
            "output_document": "",
            "templates":       "",
            "dependency":      "",
            "frequency":       "",
            "pain_points":     "Empty BPMN imported - review needed",
        })

    return {
        "process_name":         proc_name,
        "trigger_event":        trigger,
        "successful_outcome":   succ_out,
        "unsuccessful_outcome": unsucc_out,
        "steps":                steps,
    }


def parse_bpmn_file_to_dict(file_path: str, default_process_name: str = "Uplifted Process") -> Dict:
    """Read a BPMN/XML file and convert to process dict."""
    from pathlib import Path
    xml_content = Path(file_path).read_text(encoding="utf-8", errors="replace")
    return bpmn_to_process_dict(xml_content, default_process_name)
