"""DiagramIQ AWS — Python analysis route (POST /analyze).

Complements the Node Bedrock proxy: where that function talks to the model,
this one runs deterministic checks over the structured process model the model
returned. Deployed by the same Amplify pipeline (see amplify/backend.ts).

Request:  { "model": { lanes, nodes, flows } }
Response: { "counts": {...}, "issues": [ {code, message, node_ids} ] }
"""
import json

CORS = {
    "access-control-allow-origin": "*",
    "access-control-allow-headers": "content-type",
    "content-type": "application/json",
}


def reply(status, body):
    return {"statusCode": status, "headers": CORS, "body": json.dumps(body)}


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


def handler(event, context):
    if (event.get("requestContext", {}).get("http", {}).get("method")) == "OPTIONS":
        return reply(200, {})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return reply(400, {"error": "Invalid JSON body."})

    model = body.get("model")
    if not isinstance(model, dict):
        return reply(400, {"error": "model is required."})

    try:
        return reply(200, analyse(model))
    except Exception as err:  # surface actionable errors to the UI
        return reply(500, {"error": f"Analysis failed: {err}"})
