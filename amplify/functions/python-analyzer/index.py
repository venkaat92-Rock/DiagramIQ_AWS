"""DiagramIQ AWS — Python engine Lambda.

Hosts the desktop app's BPMN engine (diagramiq/, ported verbatim) behind the
HTTP API, so the browser gets the same validation, uplift and Visio import the
Windows build has. Every module is stdlib-only, so this stays a zip Lambda with
no build step.

Routes (all POST, JSON in / JSON out):
  /analyze   { model }                  -> structural checks on the AI's model JSON
  /validate  { xml, categories? }       -> BPMN + Signavio best-practice issues
  /uplift    { xml, processName? }      -> rule-based uplift, Signavio-normalised
  /visio     { fileBase64, processName? }-> .vsdx/.vsd -> BPMN 2.0 XML
  /normalize { xml, target? }           -> Signavio normalise, or Celonis-compatible
"""
import base64
import json
import os
import tempfile

from diagramiq.bpmn_parser import parse_bpmn_xml
from diagramiq.bpmn_validator import validate_bpmn
from diagramiq.local_uplift import uplift_local
from diagramiq.signavio_normalize import make_celonis_compatible, normalize_for_signavio
from diagramiq.signavio_rules import validate_signavio
from diagramiq.visio_to_bpmn import VisioConversionError, build_bpmn_from_visio

CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "content-type": "application/json",
}


def reply(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}


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
    return reply(200, {"xml": out.get("xml", ""), "log": "".join(log)})


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
        return reply(200, {"xml": xml})
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
        return reply(200, {"xml": make_celonis_compatible(normalize_for_signavio(xml))})
    return reply(200, {"xml": normalize_for_signavio(xml)})


def route_analyze(body):
    model = body.get("model")
    if not isinstance(model, dict):
        return reply(400, {"error": "model is required."})
    return reply(200, analyse(model))


ROUTES = {
    "/analyze": route_analyze,
    "/validate": route_validate,
    "/uplift": route_uplift,
    "/visio": route_visio,
    "/normalize": route_normalize,
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
