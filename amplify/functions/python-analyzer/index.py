"""DiagramIQ AWS — Python engine Lambda.

Hosts the desktop app's BPMN engine (diagramiq/, ported verbatim) behind the
HTTP API, so the browser gets the same validation, uplift and Visio import the
Windows build has. Every module is stdlib-only, so this stays a zip Lambda with
no build step.

Routes (all POST, JSON in / JSON out):
  /analyze        { model }                    -> structural checks on the model JSON
  /validate       { xml, categories? }         -> BPMN + Signavio best-practice issues
  /uplift         { xml, processName? }        -> rule-based uplift, Signavio-normalised
  /visio          { fileBase64, processName? } -> .vsdx/.vsd -> BPMN 2.0 XML
  /normalize      { xml, target? }             -> Signavio normalise / Celonis-compatible
  /excel-to-bpmn  { fileBase64, processName? } -> Process Discovery .xlsx -> BPMN
  /bpmn-to-excel  { xml, processName? }        -> BPMN -> editable review .xlsx
  /patch          { xml, fileBase64 }          -> apply the edited .xlsx back onto the BPMN
  /uplift-report  { changes, ... }             -> 3-sheet uplift report .xlsx

Files cross the wire base64-encoded and are staged in /tmp, because the ported
modules are path-based (they were written for a desktop app).
"""
import base64
import json
import os
import sys
import tempfile

# Deps vendored by the Amplify build (see amplify.yml + requirements.txt).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'vendor'))

from diagramiq.bpmn_parser import parse_bpmn_xml
from diagramiq.doc_text import UnreadableDocument, extract_text
from diagramiq.preview_model import model_from_bpmn
from diagramiq.bpmn_validator import validate_bpmn
from diagramiq.local_uplift import uplift_local
from diagramiq.signavio_normalize import make_celonis_compatible, normalize_for_signavio
from diagramiq.signavio_rules import validate_signavio
from diagramiq.visio_to_bpmn import VisioConversionError, build_bpmn_from_visio

# allow-methods and max-age matter now that the function answers its own
# preflight: behind the gateway that was the gateway's job, but a Function URL
# without a CORS configuration forwards OPTIONS straight here, and a preflight
# without allow-methods is rejected by the browser.
CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "access-control-allow-methods": "POST,OPTIONS",
    "access-control-max-age": "86400",
    "content-type": "application/json",
}


def reply(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}


def xml_reply(xml, **extra):
    """Return BPMN plus the structured model, so the browser can render the
    'what the AI understood' preview for every input channel, not just images."""
    return reply(200, {"xml": xml, "model": model_from_bpmn(xml), **extra})


# ── /analyze — structural checks over the model JSON the vision pass returns ──

def analyse(model):
    """Structural checks a reviewer would otherwise do by eye."""
    nodes = model.get("nodes") or []
    flows = model.get("flows") or []
    by_id = {n.get("id"): n for n in nodes if n.get("id")}

    sources = {f.get("from") for f in flows}
    targets = {f.get("to") for f in flows}
    issues = []

    starts = [n["id"] for n in nodes if n.get("type") == "start"]
    ends = [n["id"] for n in nodes if n.get("type") == "end"]
    if not starts:
        issues.append({"code": "no_start", "message": "No start event.", "node_ids": []})
    if not ends:
        issues.append({"code": "no_end", "message": "No end event.", "node_ids": []})

    unreachable = [i for i in by_id if i not in targets and i not in starts]
    if unreachable:
        issues.append({
            "code": "unreachable",
            "message": "Nodes nothing flows into.",
            "node_ids": unreachable,
        })

    dead_ends = [i for i in by_id if i not in sources and i not in ends]
    if dead_ends:
        issues.append({
            "code": "dead_end",
            "message": "Nodes with no outgoing flow.",
            "node_ids": dead_ends,
        })

    dangling = sorted({
        i for f in flows for i in (f.get("from"), f.get("to"))
        if i and i not in by_id
    })
    if dangling:
        issues.append({
            "code": "dangling_flow",
            "message": "Flows referencing unknown nodes.",
            "node_ids": dangling,
        })

    # A gateway that does not branch is usually a mis-read diamond.
    for n in nodes:
        if n.get("type") == "gateway":
            out = [f for f in flows if f.get("from") == n.get("id")]
            if len(out) < 2:
                issues.append({
                    "code": "gateway_single_path",
                    "message": f"Gateway '{n.get('name')}' has {len(out)} outgoing flow(s).",
                    "node_ids": [n.get("id")],
                })

    counts = {
        "lanes": len(model.get("lanes") or []),
        "nodes": len(nodes),
        "flows": len(flows),
        "tasks": sum(1 for n in nodes if n.get("type") == "task"),
        "gateways": sum(1 for n in nodes if n.get("type") == "gateway"),
    }
    return {"counts": counts, "issues": issues}


# ── route handlers ───────────────────────────────────────────────────────────

def route_validate(body):
    """BPMN structural validation + Signavio best-practice rules."""
    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    model = parse_bpmn_xml(xml)

    core = [{
        "source": "bpmn",
        "rule_id": i.rule_id,
        "severity": i.severity.value,
        "category": i.category,
        "element_id": i.element_id,
        "element_name": i.element_name,
        "message": i.message,
    } for i in validate_bpmn(model)]

    sig = [{
        "source": "signavio",
        "rule_id": i.rule_id,
        "severity": i.severity.value,
        "category": i.category,
        "element_id": i.element_id,
        "element_name": i.element_name,
        "message": i.message,
    } for i in validate_signavio(model, body.get("categories"))]

    issues = core + sig
    return reply(200, {
        "issues": issues,
        "summary": {
            "total": len(issues),
            "errors": sum(1 for i in issues if i["severity"] == "ERROR"),
            "warnings": sum(1 for i in issues if i["severity"] == "WARNING"),
            "info": sum(1 for i in issues if i["severity"] == "INFO"),
        },
    })


def route_uplift(body):
    """Rule-based uplift — naming, structure, layout. No AI, no API key."""
    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    process_name = body.get("processName") or ""

    # uplift_local is callback-driven (it streams into the desktop UI); collect
    # the callbacks into locals rather than reworking the upstream module.
    out = {}
    log = []
    uplift_local(
        xml_content=xml,
        issues=validate_bpmn(parse_bpmn_xml(xml)),
        process_name=process_name,
        on_chunk=log.append,
        on_complete=lambda result: out.__setitem__("xml", result),
        on_error=lambda msg: out.__setitem__("error", msg),
    )
    if "error" in out:
        return reply(502, {"error": out["error"]})
    return xml_reply(out.get("xml", ""), log="".join(log))


def route_visio(body):
    """Visio .vsdx/.vsd -> BPMN 2.0 XML."""
    b64 = body.get("fileBase64")
    if not b64:
        return reply(400, {"error": "fileBase64 is required."})
    suffix = ".vsd" if str(body.get("filename", "")).lower().endswith(".vsd") else ".vsdx"

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        tmp.write(base64.b64decode(b64))
        tmp.close()
        xml = build_bpmn_from_visio(tmp.name, body.get("processName") or "")
        return xml_reply(xml)
    except VisioConversionError as err:
        return reply(400, {"error": str(err)})
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def route_normalize(body):
    """Signavio normalisation, or the Celonis-compatible strip."""
    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    if body.get("target") == "celonis":
        return xml_reply(make_celonis_compatible(normalize_for_signavio(xml)))
    return xml_reply(normalize_for_signavio(xml))


def route_analyze(body):
    model = body.get("model")
    if not isinstance(model, dict):
        return reply(400, {"error": "model is required."})
    return reply(200, analyse(model))


# ── Excel round-trip (Phase 2) ───────────────────────────────────────────────

def _stage(b64, suffix):
    """Write a base64 payload to /tmp and return its path."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.write(base64.b64decode(b64))
    tmp.close()
    return tmp.name


def _drop(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def route_excel_to_bpmn(body):
    """Process Discovery .xlsx -> BPMN 2.0 XML."""
    from diagramiq.excel_to_bpmn import build_bpmn_from_excel

    b64 = body.get("fileBase64")
    if not b64:
        return reply(400, {"error": "fileBase64 is required."})
    path = _stage(b64, ".xlsx")
    try:
        return xml_reply(build_bpmn_from_excel(path, body.get("processName") or ""))
    finally:
        _drop(path)


def route_bpmn_to_excel(body):
    """BPMN -> the editable review .xlsx (step 1 of the uplift workflow)."""
    from diagramiq.bpmn_to_excel import bpmn_to_process_dict
    from diagramiq.transcription_to_excel import save_excel_from_ai_response

    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    name = body.get("processName") or "Uplifted Process"
    out = os.path.join(tempfile.gettempdir(), "review.xlsx")
    try:
        save_excel_from_ai_response(bpmn_to_process_dict(xml, default_process_name=name), out)
        with open(out, "rb") as fh:
            return reply(200, {
                "fileBase64": base64.b64encode(fh.read()).decode(),
                "filename": f"{name}.uplift_review.xlsx",
            })
    finally:
        _drop(out)


def route_patch(body):
    """Apply the user's edited review .xlsx back onto the original BPMN."""
    from diagramiq.bpmn_patcher import patch_bpmn_with_excel

    xml, b64 = body.get("xml"), body.get("fileBase64")
    if not xml or not b64:
        return reply(400, {"error": "xml and fileBase64 are required."})
    path = _stage(b64, ".xlsx")
    try:
        patched, changes = patch_bpmn_with_excel(xml, path)
        return xml_reply(patched, changes=changes)
    finally:
        _drop(path)


def route_uplift_report(body):
    """The 3-sheet uplift report: changes, BPMN Checklist, Modeller Inputs."""
    from diagramiq.uplift_report import save_uplift_report

    changes = body.get("changes")
    if not isinstance(changes, list):
        return reply(400, {"error": "changes (list) is required."})
    out = os.path.join(tempfile.gettempdir(), "uplift_report.xlsx")
    try:
        save_uplift_report(
            changes=changes,
            out_path=out,
            source_name=body.get("sourceName") or "",
            output_name=body.get("outputName") or "",
            compliance_results=body.get("complianceResults"),
            modeller_inputs=body.get("modellerInputs"),
            only_sheets=body.get("onlySheets"),
        )
        with open(out, "rb") as fh:
            return reply(200, {
                "fileBase64": base64.b64encode(fh.read()).decode(),
                "filename": "uplift_report.xlsx",
            })
    finally:
        _drop(out)


# ── AI passes on Bedrock (Phase 3) ───────────────────────────────────────────
#
# The ported modules take (provider, api_key). In AWS the Lambda's IAM role
# authorises Bedrock, so provider is always "bedrock" and the key is unused —
# the arguments stay in place to keep these files diffable against upstream.

def route_ai_uplift(body):
    """AI uplift of a whole BPMN, with the deterministic orphan pre-pass."""
    from diagramiq.ai_uplift import uplift_with_streaming

    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})

    out, log = {}, []
    uplift_with_streaming(
        xml_content=xml,
        issues=validate_bpmn(parse_bpmn_xml(xml)),
        process_name=body.get("processName") or "Process",
        provider="bedrock",
        api_key=None,
        signavio_categories=body.get("signavioCategories") or "",
        on_chunk=log.append,
        on_complete=lambda r: out.__setitem__("xml", r),
        on_error=lambda m: out.__setitem__("error", m),
    )
    if "error" in out:
        return reply(502, {"error": out["error"]})
    return xml_reply(out.get("xml", ""), log="".join(log))


def route_ai_gateways(body):
    """Suggest gateways the diagram is missing."""
    from diagramiq.ai_gateway_insert import suggest_gateways

    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    return reply(200, {"suggestions": suggest_gateways(xml, "bedrock", "")})


def route_ai_layout(body):
    """Waypoint-only layout cleanup (shape moves are rejected upstream)."""
    from diagramiq.ai_layout_cleanup import clean_layout_with_ai

    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    return xml_reply(clean_layout_with_ai(xml, "bedrock", ""))


def route_ai_naming(body):
    """Verb-first naming review over task names."""
    from diagramiq.ai_naming_check import check_task_names_with_ai

    names = body.get("taskNames")
    if not isinstance(names, dict):
        return reply(400, {"error": "taskNames (object of id -> name) is required."})
    return reply(200, {"fixes": check_task_names_with_ai(names, "bedrock", "")})


def route_ai_compliance(body):
    """Audit against the 76 Auspost rules — powers the BPMN Checklist tab.

    The catalogue rides along with the verdicts: the audit is keyed by rule id
    alone, and an id without its rule text is unreadable in the browser.
    """
    from diagramiq.ai_compliance_check import build_rules_payload, check_compliance_with_ai

    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    catalogue = build_rules_payload()
    # Optional slice: the browser fans the catalogue out over several calls so
    # each generation stays short. Without it, all 76 rules go in one call.
    try:
        offset = max(0, int(body.get("ruleOffset") or 0))
        limit = int(body.get("ruleLimit") or 0)
    except (TypeError, ValueError):
        return reply(400, {"error": "ruleOffset and ruleLimit must be integers."})
    subset = catalogue[offset:offset + limit] if limit > 0 else catalogue

    rules = [{"id": r["id"], "name": r["name"], "category": r["category"],
              "severity": r["severity"], "kind": r["kind"]}
             for r in subset]
    return reply(200, {
        "results": check_compliance_with_ai(xml, "bedrock", "", rules=subset),
        "rules": rules,
        "ruleTotal": len(catalogue),
    })


def route_ai_modeller_inputs(body):
    """Semantic gaps only a process expert can fill."""
    from diagramiq.ai_modeller_inputs import get_modeller_inputs

    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    return reply(200, {"inputs": get_modeller_inputs(xml, "bedrock", "")})


def route_notes(body):
    """⬆ Notes: .txt / .md / .docx / .pdf -> Process Discovery data.

    Returns the discovery model for the in-browser review grid, plus the .xlsx
    so the reviewer can export it. The file is read server-side, so Word and PDF
    work the same way plain text does.
    """
    from diagramiq.transcription_to_excel import (
        _parse_ai_json,
        build_excel_from_transcription,
        save_excel_from_ai_response,
    )

    text = body.get("text")
    if not text:
        b64 = body.get("fileBase64")
        if not b64:
            return reply(400, {"error": "text or fileBase64 is required."})
        try:
            text = extract_text(base64.b64decode(b64), body.get("filename") or "notes.txt")
        except UnreadableDocument as err:
            return reply(400, {"error": str(err)})

    name = body.get("processName") or "Discovered Process"
    out = {}
    build_excel_from_transcription(
        text=text,
        process_name=name,
        provider="bedrock",
        api_key="",
        on_complete=lambda raw: out.__setitem__("raw", raw),
        on_error=lambda m: out.__setitem__("error", m),
    )
    if "error" in out:
        return reply(502, {"error": out["error"]})

    parsed = _parse_ai_json(out.get("raw", ""))
    parsed.setdefault("process_name", name)
    path = os.path.join(tempfile.gettempdir(), "discovery.xlsx")
    try:
        save_excel_from_ai_response(parsed, path)
        with open(path, "rb") as fh:
            return reply(200, {
                "discovery": parsed,
                "fileBase64": base64.b64encode(fh.read()).decode(),
                "filename": f"{name}.discovery.xlsx",
            })
    finally:
        _drop(path)


def route_discovery_to_bpmn(body):
    """The review grid's Approve: edited discovery data -> BPMN, plus the
    matching .xlsx so Export reflects the edits rather than the original."""
    from diagramiq.excel_to_bpmn import build_bpmn_from_excel
    from diagramiq.transcription_to_excel import save_excel_from_ai_response

    discovery = body.get("discovery")
    if not isinstance(discovery, dict):
        return reply(400, {"error": "discovery (object) is required."})
    name = body.get("processName") or discovery.get("process_name") or "Discovered Process"

    path = os.path.join(tempfile.gettempdir(), "approved.xlsx")
    try:
        save_excel_from_ai_response(discovery, path)
        xml = build_bpmn_from_excel(path, name)
        with open(path, "rb") as fh:
            return xml_reply(xml,
                             fileBase64=base64.b64encode(fh.read()).decode(),
                             filename=f"{name}.discovery.xlsx")
    finally:
        _drop(path)


def route_discovery_xlsx(body):
    """Export the current (possibly edited) review grid as .xlsx."""
    from diagramiq.transcription_to_excel import save_excel_from_ai_response

    discovery = body.get("discovery")
    if not isinstance(discovery, dict):
        return reply(400, {"error": "discovery (object) is required."})
    name = body.get("processName") or discovery.get("process_name") or "Process Discovery"
    path = os.path.join(tempfile.gettempdir(), "export.xlsx")
    try:
        save_excel_from_ai_response(discovery, path)
        with open(path, "rb") as fh:
            return reply(200, {
                "fileBase64": base64.b64encode(fh.read()).decode(),
                "filename": f"{name}.discovery.xlsx",
            })
    finally:
        _drop(path)


def route_excel_to_discovery(body):
    """⬆ Excel: read a Process Discovery workbook back into review-grid data,
    so an uploaded sheet opens in the same editable popup as a transcript."""
    from diagramiq.excel_to_bpmn import parse_process_discovery_excel

    b64 = body.get("fileBase64")
    if not b64:
        return reply(400, {"error": "fileBase64 is required."})
    path = _stage(b64, ".xlsx")
    try:
        pd = parse_process_discovery_excel(path)
    finally:
        _drop(path)

    # The parser and the Excel writer use different field names; the workbook
    # is the interchange format, so map the parser's names onto the writer's.
    steps = [{
        "activity": st.activity or "",
        "description": st.description or "",
        "participant": st.participant or "",
        "it_systems": st.it_systems or "",
        "input_document": st.input_doc or "",
        "output_document": st.output_doc or "",
        "templates": st.templates or "",
        "dependency": st.dependency or "",
        "frequency": st.frequency or "",
        "pain_points": st.pain_points or "",
        # Carried through untouched: the patcher matches rows to elements by
        # this, and a row that loses it comes back as a brand-new task.
        "bpmn_id": st.bpmn_id or "",
    } for st in (pd.steps or [])]

    return reply(200, {"discovery": {
        "process_name": pd.process_name or "",
        "trigger_event": pd.trigger_event or "",
        "successful_outcome": pd.success_outcome or "",
        "unsuccessful_outcome": pd.failure_outcome or "",
        "steps": steps,
    }})


def route_model(body):
    """BPMN -> preview model, for a diagram the browser already holds."""
    xml = body.get("xml")
    if not xml:
        return reply(400, {"error": "xml is required."})
    return reply(200, {"model": model_from_bpmn(xml)})

ROUTES = {
    "/analyze": route_analyze,
    "/validate": route_validate,
    "/uplift-report": route_uplift_report,  # before /uplift — endswith matching
    "/uplift": route_uplift,
    "/visio": route_visio,
    "/normalize": route_normalize,
    "/excel-to-bpmn": route_excel_to_bpmn,
    "/bpmn-to-excel": route_bpmn_to_excel,
    "/patch": route_patch,
    "/ai-uplift": route_ai_uplift,
    "/ai-gateways": route_ai_gateways,
    "/ai-layout": route_ai_layout,
    "/ai-naming": route_ai_naming,
    "/ai-compliance": route_ai_compliance,
    "/ai-modeller-inputs": route_ai_modeller_inputs,
    "/notes": route_notes,
    "/discovery-to-bpmn": route_discovery_to_bpmn,
    "/discovery-xlsx": route_discovery_xlsx,
    "/excel-to-discovery": route_excel_to_discovery,
    "/model": route_model,
}


def handler(event, context):
    if (event.get("requestContext", {}).get("http", {}).get("method")) == "OPTIONS":
        return reply(200, {})

    raw = event.get("rawPath") or event.get("path") or ""
    route = next((fn for path, fn in ROUTES.items() if raw.endswith(path)), None)
    if route is None:
        return reply(404, {"error": f"Unknown route: {raw}"})

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return reply(400, {"error": "Invalid JSON body."})

    try:
        return route(body)
    except Exception as err:  # surface actionable errors to the UI
        return reply(500, {"error": f"{type(err).__name__}: {err}"})
