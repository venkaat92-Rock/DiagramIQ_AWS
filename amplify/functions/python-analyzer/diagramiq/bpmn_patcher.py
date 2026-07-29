"""Apply user-edited Excel deltas onto an original BPMN file.

v1.2.5 - replaces the old "rebuild BPMN from Excel from scratch" path for
the AI Uplift workflow.

Why this exists
---------------
The previous flow (v1.2.0 - v1.2.4) was:

    upload BPMN -> generate review Excel -> user edits -> rebuild a brand
    new BPMN from the Excel via excel_to_bpmn.build_bpmn_from_excel()

That works but throws away every detail of the original BPMN that the
Excel template doesn't capture: layout coordinates, Signavio extension
elements, swimlane decoration, message flows, data objects, annotations,
language metadata, and so on. It also runs an AI / heuristic pass that
takes seconds and is sensitive to LLM quirks.

This module flips the model: the original BPMN is the authoritative base.
The user's Excel edits become a small DELTA that gets applied surgically
on top of it. Result:

  - Sub-second runtime (no AI call)
  - Layout, extensions, metadata, all preserved
  - Only what the user edited actually changes

Round-trip key
--------------
Every row written to the review Excel by `transcription_to_excel.
save_excel_from_ai_response()` carries a hidden 11th column "BPMN ID
(do not edit)" populated from `bpmn_to_excel.bpmn_to_process_dict()`.
That ID lets the patcher map each Excel row to the exact element in the
original BPMN that produced it, regardless of reordering or renaming.

Public API
----------
    patch_bpmn_with_excel(original_xml: str, excel_path: str) -> str
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .bpmn_parser import BPMN_NS, parse_bpmn_xml


def patch_bpmn_with_excel(
    original_xml: str, excel_path: str
) -> Tuple[str, List[Dict]]:
    """Apply edits from `excel_path` to `original_xml`.

    Operations performed (in order):

    1. Load original BPMN as ElementTree.
    2. Read every row of the Excel review template.
    3. For each row whose `bpmn_id` matches an element in the original:
         - rename the element if `activity` differs from current @name
         - move the element to the named lane if `participant` differs
    4. For Excel rows whose `bpmn_id` is empty: append a new `<bpmn:task>`
       to the same process the other tasks live in. Layout for new tasks
       gets done downstream by signavio_normalize.
    5. For elements present in the original but absent from the Excel:
       remove them (user explicitly deleted that row).
    6. Run the result through `signavio_normalize.normalize_for_signavio`
       so the output is import-ready.

    Returns a tuple of (patched_xml, change_log) where change_log is a list
    of dicts in the format used by uplift_report.py.
    """
    rows = _read_excel_rows(excel_path)

    # Pre-register namespace prefixes so ET.tostring keeps the canonical
    # bpmn:/bpmndi:/dc:/di: instead of auto-generated ns0/ns1/...
    _register_namespaces(original_xml)

    try:
        root = ET.fromstring(original_xml)
    except ET.ParseError:
        # Caller's responsibility - we can't patch what we can't parse.
        # Return the original unchanged so the user sees the same outcome
        # they'd have got without us.
        return original_xml

    elements_by_id: Dict[str, ET.Element] = {
        e.get("id"): e for e in root.iter() if e.get("id")
    }
    lanes_by_name: Dict[str, ET.Element] = {
        e.get("name", "").strip(): e
        for e in root.iter(f"{{{BPMN_NS}}}lane")
        if e.get("name")
    }

    changes: List[Dict] = []
    seen_ids: set = set()

    for row in rows:
        bpmn_id = (row.get("bpmn_id") or "").strip()
        new_name = (row.get("activity") or "").strip()
        new_lane = (row.get("participant") or "").strip()

        if bpmn_id and bpmn_id in elements_by_id:
            seen_ids.add(bpmn_id)
            elem = elements_by_id[bpmn_id]

            cur_name = (elem.get("name") or "").strip()
            if new_name and new_name != cur_name:
                elem.set("name", new_name)
                changes.append({
                    "category":   "Patcher",
                    "rule":       "P3 - rename from review Excel",
                    "element_id": bpmn_id,
                    "before":     cur_name,
                    "after":      new_name,
                    "detail":     "user edited the activity name in the review Excel",
                })

            # v1.2.7 - ignore the placeholder strings bpmn_to_excel writes
            # when the original task wasn't in any swimlane. Only act on
            # values the user actually typed.
            _LANE_PLACEHOLDERS = {"Unassigned", "Unknown", "Unnamed lane", ""}
            if new_lane and new_lane not in _LANE_PLACEHOLDERS:
                cur_lane = _find_lane_for(root, bpmn_id)
                if cur_lane != new_lane:
                    _move_to_lane(root, bpmn_id, new_lane, lanes_by_name)
                    changes.append({
                        "category":   "Patcher",
                        "rule":       "P4 - re-lane from review Excel",
                        "element_id": bpmn_id,
                        "before":     cur_lane or "(unassigned)",
                        "after":      new_lane,
                        "detail":     "user changed the participant in the review Excel",
                    })

        elif not bpmn_id and new_name:
            # New row in the Excel -> user wants a new task added.
            new_task_id = _insert_task(root, new_name, new_lane, lanes_by_name)
            if new_task_id:
                seen_ids.add(new_task_id)
                changes.append({
                    "category":   "Patcher",
                    "rule":       "P5 - insert task from review Excel",
                    "element_id": new_task_id,
                    "before":     "(not present)",
                    "after":      new_name,
                    "detail":     f"new task added (lane: {new_lane or '(unassigned)'}); "
                                  f"user must wire up sequence flows in Signavio",
                })

    # Step 5: any task that was in the original but is NOT represented in
    # the Excel rows was explicitly deleted by the user. Drop it.
    original_task_ids = _collect_task_ids(root)
    deleted_ids = original_task_ids - seen_ids
    for did in deleted_ids:
        # Capture the name before we delete so we can show it in the report.
        elem_for_log = elements_by_id.get(did)
        before_name = (elem_for_log.get("name") if elem_for_log is not None else "") or did
        if _delete_element(root, did):
            changes.append({
                "category":   "Patcher",
                "rule":       "P6 - delete row removed from Excel",
                "element_id": did,
                "before":     before_name,
                "after":      "(removed)",
                "detail":     "user removed this row from the review Excel; "
                              "element + its sequence flows + lane membership all dropped",
            })

    # Step 6: re-serialise
    patched = ET.tostring(root, encoding="unicode")

    # Step 7: normalise for Signavio so namespace decls are intact, BPMNDiagram
    # has shapes for any new task, etc. This is the same pass run by the AI
    # uplift path, so output formats stay consistent across all uplift modes.
    try:
        from .signavio_normalize import normalize_for_signavio
        patched = normalize_for_signavio(patched)
    except Exception:
        pass

    rename_count = sum(1 for c in changes if c["rule"].startswith("P3"))
    relane_count = sum(1 for c in changes if c["rule"].startswith("P4"))
    insert_count = sum(1 for c in changes if c["rule"].startswith("P5"))
    delete_count = sum(1 for c in changes if c["rule"].startswith("P6"))
    print(
        f"[bpmn_patcher] applied: rename={rename_count} re-lane={relane_count} "
        f"insert={insert_count} delete={delete_count}"
    )
    return patched, changes


# ---------------------------------------------------------------------------
# Excel reader
# ---------------------------------------------------------------------------

def _read_excel_rows(path: str) -> List[Dict[str, str]]:
    """Read a Process Discovery Excel and return the data rows (row 7+).

    Column layout matches `transcription_to_excel.save_excel_from_ai_response`:
        A activity, B description, C participant, D it_systems,
        E input_document, F output_document, G templates, H dependency,
        I frequency, J pain_points, K bpmn_id (v1.2.5 round-trip key)
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    rows: List[Dict[str, str]] = []
    for row_i in range(7, ws.max_row + 1):
        def cell(col: int) -> str:
            v = ws.cell(row=row_i, column=col).value
            return ("" if v is None else str(v)).strip()

        activity = cell(1)
        bpmn_id  = cell(11)

        # Skip fully empty rows (allows users to leave trailing blanks).
        if not activity and not bpmn_id:
            continue

        rows.append({
            "activity":        activity,
            "description":     cell(2),
            "participant":     cell(3),
            "it_systems":      cell(4),
            "input_document":  cell(5),
            "output_document": cell(6),
            "templates":       cell(7),
            "dependency":      cell(8),
            "frequency":       cell(9),
            "pain_points":     cell(10),
            "bpmn_id":         bpmn_id,
        })
    return rows


# ---------------------------------------------------------------------------
# Patch operations on the BPMN tree
# ---------------------------------------------------------------------------

def _register_namespaces(xml: str) -> None:
    """Mirror of signavio_normalize._register_namespaces - kept private here
    so the patcher works even if signavio_normalize is unavailable."""
    if 'xmlns="' + BPMN_NS + '"' in xml:
        ET.register_namespace("", BPMN_NS)
    else:
        ET.register_namespace("bpmn", BPMN_NS)
    ET.register_namespace("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI")
    if 'xmlns:omgdc="' in xml:
        ET.register_namespace("omgdc", "http://www.omg.org/spec/DD/20100524/DC")
        ET.register_namespace("omgdi", "http://www.omg.org/spec/DD/20100524/DI")
    else:
        ET.register_namespace("dc", "http://www.omg.org/spec/DD/20100524/DC")
        ET.register_namespace("di", "http://www.omg.org/spec/DD/20100524/DI")
    ET.register_namespace("xsi",      "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("signavio", "http://www.signavio.com")
    ET.register_namespace(
        "i18n",
        "http://www.omg.org/spec/BPMN/non-normative/extensions/i18n/1.0",
    )


def _collect_task_ids(root: ET.Element) -> set:
    """Element IDs eligible to appear in the Excel review (tasks & call activities)."""
    out = set()
    for tag_local in ("task", "userTask", "serviceTask", "scriptTask",
                      "manualTask", "sendTask", "receiveTask",
                      "businessRuleTask", "callActivity", "subProcess"):
        for elem in root.iter(f"{{{BPMN_NS}}}{tag_local}"):
            eid = elem.get("id")
            if eid:
                out.add(eid)
    return out


def _find_lane_for(root: ET.Element, element_id: str) -> str:
    """Name of the lane that currently contains `element_id`. Returns "" if none."""
    for lane in root.iter(f"{{{BPMN_NS}}}lane"):
        for ref in lane.findall(f"{{{BPMN_NS}}}flowNodeRef"):
            if ref.text and ref.text.strip() == element_id:
                return (lane.get("name") or "").strip()
    return ""


def _move_to_lane(
    root: ET.Element,
    element_id: str,
    target_lane_name: str,
    lanes_by_name: Dict[str, ET.Element],
) -> None:
    """Remove element_id from its current lane (if any) and add it to the
    lane named `target_lane_name`. If that lane doesn't exist, create it."""
    # Remove from current lane
    for lane in root.iter(f"{{{BPMN_NS}}}lane"):
        for ref in list(lane.findall(f"{{{BPMN_NS}}}flowNodeRef")):
            if ref.text and ref.text.strip() == element_id:
                lane.remove(ref)

    # Find or create target lane
    target = lanes_by_name.get(target_lane_name)
    if target is None:
        # Create a new lane in the first laneSet we find
        lane_set = next(root.iter(f"{{{BPMN_NS}}}laneSet"), None)
        if lane_set is None:
            # No laneSet at all - create one inside the process
            process = next(root.iter(f"{{{BPMN_NS}}}process"), None)
            if process is None:
                return  # nothing we can do; skip silently
            lane_set = ET.SubElement(process, f"{{{BPMN_NS}}}laneSet", {
                "id": "LaneSet_default",
            })
        new_id = "Lane_" + target_lane_name.replace(" ", "_")[:32]
        target = ET.SubElement(lane_set, f"{{{BPMN_NS}}}lane", {
            "id":   new_id,
            "name": target_lane_name,
        })
        lanes_by_name[target_lane_name] = target

    # Add the flowNodeRef to the target lane
    ref = ET.SubElement(target, f"{{{BPMN_NS}}}flowNodeRef")
    ref.text = element_id


def _insert_task(
    root: ET.Element,
    name: str,
    lane_name: str,
    lanes_by_name: Dict[str, ET.Element],
) -> Optional[str]:
    """Append a new <bpmn:task> to the process and (optionally) a lane.

    Returns the new task's id, or None on failure. Note: the new task has no
    sequence-flow connections - the user will need to wire it up in Signavio.
    The Excel review template doesn't capture flow topology, so this is the
    cleanest behaviour: the new task exists, the user positions it.
    """
    process = next(root.iter(f"{{{BPMN_NS}}}process"), None)
    if process is None:
        return None
    new_id = _fresh_id(root, "Task_")
    task = ET.SubElement(process, f"{{{BPMN_NS}}}task", {
        "id":   new_id,
        "name": name,
    })
    if lane_name:
        _move_to_lane(root, new_id, lane_name, lanes_by_name)
    return new_id


def _delete_element(root: ET.Element, element_id: str) -> bool:
    """Remove the element with @id == element_id, plus any sequenceFlow that
    references it as source or target, plus any flowNodeRef inside lanes.
    Returns True if anything was removed."""
    removed = False

    # 1. Remove the element itself from any parent
    for parent in root.iter():
        for child in list(parent):
            if child.get("id") == element_id:
                parent.remove(child)
                removed = True

    # 2. Remove sequence flows that touched it
    for parent in root.iter():
        for child in list(parent):
            if child.tag == f"{{{BPMN_NS}}}sequenceFlow":
                src = child.get("sourceRef")
                tgt = child.get("targetRef")
                if src == element_id or tgt == element_id:
                    parent.remove(child)
                    removed = True

    # 3. Remove flowNodeRef entries pointing at it
    for lane in root.iter(f"{{{BPMN_NS}}}lane"):
        for ref in list(lane.findall(f"{{{BPMN_NS}}}flowNodeRef")):
            if ref.text and ref.text.strip() == element_id:
                lane.remove(ref)
                removed = True

    return removed


def _fresh_id(root: ET.Element, prefix: str) -> str:
    """Return an id starting with `prefix` that doesn't collide with anything
    already in the tree."""
    used = {e.get("id") for e in root.iter() if e.get("id")}
    n = 1
    while True:
        candidate = f"{prefix}{n}"
        if candidate not in used:
            return candidate
        n += 1
