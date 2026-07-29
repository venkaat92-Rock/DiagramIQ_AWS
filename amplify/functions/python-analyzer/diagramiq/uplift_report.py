"""Generate an Excel report listing every change the AI Uplift made (v1.2.7).

After click-2 of AI Uplift, the patcher and the advanced-rules pass each
emit a list of dict change-entries. This module renders them into an Excel
file the user can hand to a reviewer.

Layout of the produced workbook
-------------------------------

  Sheet 1 - "Summary"
    Cell A1: Title
    Cell A3: source filename, A4: output filename, A5: total changes
    Below: count by category

  Sheet 2 - "Changes"
    Header row: # | Category | Rule | Element ID | Before | After | Detail
    One row per change, colour-coded by category.

Public API
----------
    save_uplift_report(
        changes:  list[dict],
        out_path: str | Path,
        source_name: str = "",
        output_name: str = "",
    ) -> Path
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Union


_CAT_FILL = {
    # category -> hex fill colour
    "Patcher":         "E8F4FD",   # light blue   - user edits
    "Naming":          "E2EFDA",   # light green  - verb-at-start fixes
    "Gateway pairing": "FCE4D6",   # light orange - structural fixes
    "Layout":          "FFF2CC",   # light yellow - waypoint straightening
    "Cleanup":         "F2F2F2",   # grey         - orphan removal etc.
}


def save_uplift_report(
    changes: List[Dict],
    out_path: Union[str, Path],
    source_name: str = "",
    output_name: str = "",
    compliance_results: Optional[Dict[str, Dict[str, str]]] = None,
    modeller_inputs:    Optional[List[Dict[str, str]]] = None,
    only_sheets:        Optional[List[str]] = None,
) -> Path:
    """Write the uplift change report to `out_path`. Returns the path.

    `compliance_results` (when provided) powers the "BPMN Checklist" sheet -
    every Auspost rule with the AI verdict (Verified / Not Verified /
    Not Applicable / Pending).

    `modeller_inputs` (when provided) powers the new "Required Inputs from
    Modeller" sheet - the semantic gaps in the BPMN that only a process
    expert can fill in (missing system references, ambiguous gateway
    conditions, undocumented decision criteria, etc.). Source format is
    the list returned by `ai_modeller_inputs.get_modeller_inputs`.
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    out = Path(out_path)
    wb = openpyxl.Workbook()

    # ---- Sheet 1: Summary ----
    s1 = wb.active
    s1.title = "Summary"

    title_font = Font(name="Calibri", size=18, bold=True, color="1F4E78")
    label_font = Font(name="Calibri", size=10, bold=True, color="4F4F4F")
    body_font  = Font(name="Calibri", size=11)
    s1["A1"].value = "DiagramIQ AI Uplift - Change Report"
    s1["A1"].font  = title_font
    s1.merge_cells("A1:D1")

    s1["A3"].value = "Source BPMN"
    s1["A3"].font  = label_font
    s1["B3"].value = source_name or "-"
    s1["B3"].font  = body_font

    s1["A4"].value = "Output BPMN"
    s1["A4"].font  = label_font
    s1["B4"].value = output_name or "-"
    s1["B4"].font  = body_font

    s1["A5"].value = "Total changes"
    s1["A5"].font  = label_font
    s1["B5"].value = len(changes)
    s1["B5"].font  = Font(name="Calibri", size=11, bold=True)

    # Counts by category
    by_cat: Dict[str, int] = {}
    for c in changes:
        cat = c.get("category") or "Other"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    s1["A7"].value = "Changes by category"
    s1["A7"].font  = label_font
    row_i = 8
    for cat in sorted(by_cat):
        s1.cell(row=row_i, column=1, value=cat).font = body_font
        s1.cell(row=row_i, column=2, value=by_cat[cat]).font = Font(
            name="Calibri", size=11, bold=True)
        s1.cell(row=row_i, column=1).fill = PatternFill(
            "solid", fgColor=_CAT_FILL.get(cat, "F2F2F2"))
        s1.cell(row=row_i, column=2).fill = PatternFill(
            "solid", fgColor=_CAT_FILL.get(cat, "F2F2F2"))
        row_i += 1

    s1.column_dimensions["A"].width = 24
    s1.column_dimensions["B"].width = 60

    # ---- Sheet 2: Changes ----
    s2 = wb.create_sheet("Changes")

    headers = ["#", "Category", "Rule", "Element ID", "Before", "After", "Detail"]
    hdr_fill = PatternFill("solid", fgColor="1A2942")
    hdr_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    hdr_align = Alignment(wrap_text=True, horizontal="center", vertical="center")
    for col_i, h in enumerate(headers, start=1):
        c = s2.cell(row=1, column=col_i, value=h)
        c.fill = hdr_fill
        c.font = hdr_font
        c.alignment = hdr_align
    s2.row_dimensions[1].height = 26

    body_align = Alignment(wrap_text=True, vertical="top")
    for row_i, ch in enumerate(changes, start=2):
        cat   = ch.get("category") or ""
        fill  = PatternFill("solid", fgColor=_CAT_FILL.get(cat, "FFFFFF"))
        vals = [
            row_i - 1,
            cat,
            ch.get("rule") or "",
            ch.get("element_id") or "",
            ch.get("before") or "",
            ch.get("after") or "",
            ch.get("detail") or "",
        ]
        for col_i, v in enumerate(vals, start=1):
            cell = s2.cell(row=row_i, column=col_i, value=v)
            cell.alignment = body_align
            cell.font = body_font
            if col_i in (2,):  # category column gets the fill
                cell.fill = fill

    widths = [5, 18, 35, 28, 40, 40, 50]
    for i, w in enumerate(widths, start=1):
        s2.column_dimensions[get_column_letter(i)].width = w

    s2.freeze_panes = "A2"

    # ---- Sheet 3 (v1.2.15): BPMN Checklist (the Auspost rule scorecard) ----
    _add_compliance_sheet(wb, compliance_results)

    # Surface compliance pass/fail counts on the Summary sheet too.
    if compliance_results is not None:
        _stamp_compliance_summary(s1, compliance_results)

    # ---- Sheet 4 (v1.2.16): Required Inputs from Modeller ----
    _add_modeller_inputs_sheet(wb, modeller_inputs)

    # ---- v1.2.17: optionally keep only a subset of sheets ----
    # only_sheets may contain any of: "summary", "changes", "checklist",
    # "modeller". When given, all other sheets are removed. Used by the
    # transcription -> uplift flow to emit just the BPMN Checklist and
    # Required Inputs from Modeller tabs.
    if only_sheets:
        keep_titles = set()
        title_map = {
            "summary":   "Summary",
            "changes":   "Changes",
            "checklist": "BPMN Checklist",
            "modeller":  "Required Inputs from Modeller",
        }
        for key in only_sheets:
            t = title_map.get(key.strip().lower())
            if t:
                keep_titles.add(t)
        if keep_titles:
            for ws in list(wb.worksheets):
                if ws.title not in keep_titles and len(wb.worksheets) > 1:
                    wb.remove(ws)
            # make the first surviving sheet active
            wb.active = 0

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out))
    return out


# ---------------------------------------------------------------------------
# BPMN checklist sheet (v1.2.15 - renamed from 'Compliance Checklist' in v1.2.16)
# ---------------------------------------------------------------------------

_STATUS_FILL = {
    "Verified":       "C6EFCE",   # green
    "Not Verified":   "FFC7CE",   # red
    "Not Applicable": "E0E0E0",   # grey
    "Pending":        "FFF2CC",   # yellow - AI didn't run
}
_STATUS_FONT = {
    "Verified":       "006100",
    "Not Verified":   "9C0006",
    "Not Applicable": "595959",
    "Pending":        "9C5700",
}


def _add_compliance_sheet(wb, compliance_results):
    """Build the third sheet listing every Auspost rule with its AI verdict.

    If `compliance_results` is None or empty, every rule shows status
    "Pending" with an explanatory note (AI verification was skipped or
    unavailable). The sheet is always written so the user has a complete
    picture of every rule the uplift is meant to satisfy.
    """
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    try:
        from ai_compliance_check import build_rules_payload
        rules = build_rules_payload()
    except Exception:
        rules = []
    if not rules:
        return

    s3 = wb.create_sheet("BPMN Checklist")

    title_font = Font(name="Calibri", size=14, bold=True, color="1F4E78")
    s3["A1"].value = "Auspost BPMN 2.0 Checklist"
    s3["A1"].font  = title_font
    s3.merge_cells("A1:F1")

    if compliance_results is None or not compliance_results:
        s3["A2"].value = (
            "AI verification was skipped (provider is local-only OR the API call "
            "was unavailable). All rules below default to 'Pending'. To get real "
            "verdicts, set an Anthropic / Gemini key in Settings and re-run "
            "AI Uplift."
        )
        s3["A2"].font = Font(name="Calibri", size=10, italic=True, color="9C5700")
        s3.merge_cells("A2:F2")
        s3.row_dimensions[2].height = 32

    # Header
    headers = ["#", "Rule ID", "Category / Severity", "Rule Description",
               "AI Status", "AI Notes"]
    hdr_fill = PatternFill("solid", fgColor="1A2942")
    hdr_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    hdr_align = Alignment(wrap_text=True, horizontal="center", vertical="center")
    HDR_ROW = 4
    for col_i, h in enumerate(headers, start=1):
        c = s3.cell(row=HDR_ROW, column=col_i, value=h)
        c.fill = hdr_fill
        c.font = hdr_font
        c.alignment = hdr_align
    s3.row_dimensions[HDR_ROW].height = 28

    body_align = Alignment(wrap_text=True, vertical="top")
    body_font  = Font(name="Calibri", size=10)
    bold_font  = Font(name="Calibri", size=10, bold=True)

    counts = {"Verified": 0, "Not Verified": 0, "Not Applicable": 0, "Pending": 0}
    row_i = HDR_ROW + 1
    for i, rule in enumerate(rules, start=1):
        rid = rule["id"]
        verdict = (compliance_results or {}).get(rid)
        status = (verdict or {}).get("status") or "Pending"
        notes  = (verdict or {}).get("notes")  or "(AI verdict not available)"
        counts[status] = counts.get(status, 0) + 1

        cat_str = rule.get("category", "")
        if rule.get("severity"):
            cat_str = f"{cat_str}  ({rule['severity']})"

        s3.cell(row=row_i, column=1, value=i).font = body_font
        s3.cell(row=row_i, column=2, value=rid).font = bold_font
        s3.cell(row=row_i, column=3, value=cat_str).font = body_font
        s3.cell(row=row_i, column=4, value=f"{rule['name']} — {rule['description']}").font = body_font
        status_cell = s3.cell(row=row_i, column=5, value=status)
        status_cell.fill = PatternFill("solid", fgColor=_STATUS_FILL[status])
        status_cell.font = Font(name="Calibri", size=10, bold=True,
                                color=_STATUS_FONT[status])
        status_cell.alignment = Alignment(horizontal="center", vertical="center")
        s3.cell(row=row_i, column=6, value=notes).font = body_font

        for col_i in range(1, 7):
            s3.cell(row=row_i, column=col_i).alignment = (
                Alignment(horizontal="center", vertical="center")
                if col_i in (1, 5) else body_align
            )
        row_i += 1

    # Footer summary row
    summary_row = row_i + 1
    s3.cell(row=summary_row, column=1, value="").font = body_font
    s3.cell(row=summary_row, column=2, value="TOTAL").font = bold_font
    s3.cell(row=summary_row, column=3, value=f"{len(rules)} rules").font = bold_font
    s3.cell(row=summary_row, column=4,
            value=f"Verified: {counts['Verified']} | "
                  f"Not Verified: {counts['Not Verified']} | "
                  f"Not Applicable: {counts['Not Applicable']} | "
                  f"Pending: {counts['Pending']}").font = bold_font
    s3.merge_cells(start_row=summary_row, start_column=4,
                   end_row=summary_row, end_column=6)

    widths = [5, 14, 28, 60, 16, 50]
    for i, w in enumerate(widths, start=1):
        s3.column_dimensions[get_column_letter(i)].width = w
    s3.freeze_panes = f"A{HDR_ROW + 1}"


def _stamp_compliance_summary(s1, compliance_results):
    """Append the AI compliance counts to the Summary sheet so the
    headline numbers are visible without flipping tabs."""
    from openpyxl.styles import Font, PatternFill

    counts = {"Verified": 0, "Not Verified": 0, "Not Applicable": 0}
    for v in compliance_results.values():
        st = v.get("status")
        if st in counts:
            counts[st] += 1
    total = sum(counts.values())

    label_font = Font(name="Calibri", size=10, bold=True, color="4F4F4F")
    body_font  = Font(name="Calibri", size=11, bold=True)

    next_row = s1.max_row + 2
    s1.cell(row=next_row, column=1,
            value="BPMN checklist audit").font = label_font
    next_row += 1
    for status in ("Verified", "Not Verified", "Not Applicable"):
        s1.cell(row=next_row, column=1, value=status).font = Font(
            name="Calibri", size=10)
        c = s1.cell(row=next_row, column=2,
                    value=f"{counts[status]} / {total}")
        c.font = body_font
        c.fill = PatternFill("solid", fgColor=_STATUS_FILL[status])
        next_row += 1


# ---------------------------------------------------------------------------
# Sheet 4 (v1.2.16): Required Inputs from Modeller
# ---------------------------------------------------------------------------

_CATEGORY_FILL = {
    "Missing data":              "FFF2CC",   # yellow
    "Ambiguous flow":            "FCE4D6",   # light orange
    "Missing role":              "DDEBF7",   # light blue
    "Missing system":            "E2EFDA",   # light green
    "Missing documentation":     "F2F2F2",   # grey
    "Missing decision criteria": "FCE4D6",   # light orange
    "Compliance gap":            "FFC7CE",   # red
    "Missing input/output":      "DDEBF7",   # light blue
    "Edge case":                 "FCE4D6",   # light orange
    "SLA/Frequency":             "FFF2CC",   # yellow
}


def _add_modeller_inputs_sheet(wb, modeller_inputs):
    """Build the fourth sheet listing semantic gaps the modeller should
    address. When `modeller_inputs` is None / empty we still create the
    sheet with a friendly note so the user knows the slot exists."""
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    s4 = wb.create_sheet("Required Inputs from Modeller")

    # Title
    title_font = Font(name="Calibri", size=14, bold=True, color="1F4E78")
    s4["A1"].value = "Required Inputs from the Process Modeller"
    s4["A1"].font = title_font
    s4.merge_cells("A1:G1")

    # Sub-title / explanation
    sub_font = Font(name="Calibri", size=10, italic=True, color="595959")
    s4["A2"].value = (
        "These are SEMANTIC gaps the AI spotted in the uplifted BPMN that "
        "only a process expert can fill in. Each row is a specific "
        "question to take back to the modeller. Use this as a follow-up "
        "checklist after each AI Uplift."
    )
    s4["A2"].font = sub_font
    s4["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    s4.merge_cells("A2:G2")
    s4.row_dimensions[2].height = 36

    # If there are no inputs - either AI wasn't run, or genuinely no gaps.
    if modeller_inputs is None:
        s4["A4"].value = (
            "AI scan was skipped (provider is local-only OR the API call "
            "was unavailable). To get a list of modeller follow-up "
            "questions, set an Anthropic / Gemini key in Settings and "
            "re-run AI Uplift."
        )
        s4["A4"].font = Font(name="Calibri", size=10, italic=True, color="9C5700")
        s4["A4"].alignment = Alignment(wrap_text=True, vertical="top")
        s4.merge_cells("A4:G4")
        s4.row_dimensions[4].height = 36
        s4.column_dimensions["A"].width = 120
        return

    if not modeller_inputs:
        s4["A4"].value = (
            "The AI did not find any semantic gaps. The uplifted BPMN "
            "appears to be informationally complete - all tasks have "
            "documentation, gateways have clear conditions, systems and "
            "data objects are identified, and roles are assigned."
        )
        s4["A4"].font = Font(name="Calibri", size=11, bold=True, color="2C5F2D")
        s4["A4"].alignment = Alignment(wrap_text=True, vertical="top")
        s4.merge_cells("A4:G4")
        s4.row_dimensions[4].height = 42
        s4.column_dimensions["A"].width = 120
        return

    # Header
    headers = ["#", "Category", "Element ID", "Element Name",
               "What's Missing", "Why It Matters", "Suggested Question"]
    hdr_fill = PatternFill("solid", fgColor="1A2942")
    hdr_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    hdr_align = Alignment(wrap_text=True, horizontal="center", vertical="center")
    HDR_ROW = 4
    for col_i, h in enumerate(headers, start=1):
        c = s4.cell(row=HDR_ROW, column=col_i, value=h)
        c.fill = hdr_fill
        c.font = hdr_font
        c.alignment = hdr_align
    s4.row_dimensions[HDR_ROW].height = 28

    # Rows
    body_align = Alignment(wrap_text=True, vertical="top")
    body_font  = Font(name="Calibri", size=10)
    bold_font  = Font(name="Calibri", size=10, bold=True)

    cat_counts = {}
    for i, item in enumerate(modeller_inputs, start=1):
        row_i = HDR_ROW + i
        cat = item.get("category") or "Missing data"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1

        s4.cell(row=row_i, column=1, value=i).font = body_font
        cat_cell = s4.cell(row=row_i, column=2, value=cat)
        cat_cell.font = bold_font
        cat_cell.fill = PatternFill(
            "solid",
            fgColor=_CATEGORY_FILL.get(cat, "F2F2F2"),
        )
        s4.cell(row=row_i, column=3, value=item.get("element_id", "")).font = body_font
        s4.cell(row=row_i, column=4, value=item.get("element_name", "")).font = body_font
        s4.cell(row=row_i, column=5, value=item.get("what_missing", "")).font = body_font
        s4.cell(row=row_i, column=6, value=item.get("why_it_matters", "")).font = body_font
        q_cell = s4.cell(row=row_i, column=7, value=item.get("suggested_question", ""))
        q_cell.font = Font(name="Calibri", size=10, italic=True)

        for col_i in range(1, 8):
            cell = s4.cell(row=row_i, column=col_i)
            cell.alignment = (
                Alignment(horizontal="center", vertical="center")
                if col_i == 1 else body_align
            )

    # Summary row
    summary_row = HDR_ROW + len(modeller_inputs) + 2
    s4.cell(row=summary_row, column=1, value="").font = body_font
    s4.cell(row=summary_row, column=2, value="TOTAL").font = bold_font
    s4.cell(row=summary_row, column=3,
            value=f"{len(modeller_inputs)} gaps").font = bold_font
    breakdown = ", ".join(f"{cat}: {n}" for cat, n in sorted(cat_counts.items()))
    s4.cell(row=summary_row, column=4, value=breakdown).font = body_font
    s4.merge_cells(start_row=summary_row, start_column=4,
                   end_row=summary_row, end_column=7)

    widths = [5, 22, 22, 28, 45, 35, 50]
    for i, w in enumerate(widths, start=1):
        s4.column_dimensions[get_column_letter(i)].width = w

    s4.freeze_panes = f"A{HDR_ROW + 1}"
