"""AI-driven gateway insertion for BPMN (v1.2.23 - 2-step loop-back rework).

A linear transcript-generated process often hides real decision points
("Validate order details" -> passed OR retry; "Attempt delivery" ->
delivered OR retry). For each decision this module inserts a MERGE (XOR
join) gateway BEFORE the decision task and a DECISION (XOR split) gateway
AFTER it; the positive branch continues forward while the negative branch
loops back to the merge - i.e. the negative outcome RE-RUNS the previous
step (the rework loop the modeller asked for). The selected AI provider
is used only to locate the decision tasks and phrase the question; the
deterministic heuristic does the same with no key. The caller re-runs the
layout fixer + signavio_normalize so the new gateways are positioned and
connected cleanly (the loop edge is routed over the top via its
`_loopback` flow-id suffix).

Because gateway insertion is structural and risky, the AI only returns
edit *intentions*; the deterministic applier in `apply_gateways()`
validates every referenced id before changing anything and silently
drops edits that don't line up.

Public API
----------
    suggest_gateways(xml, provider, api_key) -> list[dict]
    apply_gateways(xml, edits) -> (new_xml, applied_count)
    insert_gateways(xml, provider, api_key) -> (new_xml, applied_count)
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"

_TASK_TAGS = {"task", "userTask", "serviceTask", "scriptTask", "manualTask",
              "sendTask", "receiveTask", "businessRuleTask",
              "callActivity", "subProcess"}


_GATEWAY_SYSTEM_PROMPT = """You are a BPMN 2.0 modelling expert.

You receive a BPMN process as a list of tasks and the sequence flows
between them. The process was generated from a transcript and is
currently LINEAR (one straight chain). Identify the genuine DECISION
POINTS where an exclusive gateway (XOR) should split the flow.

For each decision you find, return one JSON object:
  - "after_task_id":  the id of the task AFTER which the gateway goes
  - "question":       the gateway label, phrased as a question (e.g.
                       "Delivery successful?")
  - "outcomes": a list of 2-3 objects, each:
        { "label": "<past-tense outcome, e.g. 'Delivered'>",
          "target_task_id": "<existing task id this branch flows to>" }

Rules:
  - Only use task ids that exist in the input.
  - One outcome's target should be the natural "happy path" successor;
    other outcomes route to exception / alternative tasks that already
    exist in the process.
  - Do NOT invent new tasks. Only branch to existing ones.
  - Only flag REAL decisions (delivery success/failure, validation
    pass/fail, in-stock/out-of-stock). If the process is genuinely
    linear with no decisions, return [].

CRITICAL: return ONLY a JSON array, no prose, no markdown fences.
"""


def _summarise(xml: str) -> Tuple[str, Dict[str, str]]:
    """Return a compact task+flow summary for the AI, plus id->name map."""
    root = ET.fromstring(xml)
    names: Dict[str, str] = {}
    tasks = []
    task_tags = {"task","userTask","serviceTask","scriptTask","manualTask",
                 "sendTask","receiveTask","businessRuleTask","callActivity","subProcess"}
    for el in root.iter():
        lt = el.tag.split("}")[-1]
        eid = el.get("id")
        if eid and lt in task_tags:
            nm = el.get("name", "")
            names[eid] = nm
            tasks.append({"id": eid, "name": nm})
    flows = []
    for sf in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        flows.append({"source": sf.get("sourceRef"), "target": sf.get("targetRef")})
    summary = json.dumps({"tasks": tasks, "flows": flows}, indent=2)
    return summary, names


def suggest_gateways(xml: str, provider: str, api_key: str) -> List[Dict]:
    if provider == "local" or (not api_key and provider != "bedrock"):
        return []
    try:
        summary, _ = _summarise(xml)
    except ET.ParseError:
        return []
    user = "Process:\n```json\n" + summary + "\n```\nReturn the JSON array."
    try:
        if provider == "bedrock":
            text = _call_bedrock(user, api_key)
        elif provider == "anthropic":
            text = _call_anthropic(user, api_key)
        elif provider == "gemini":
            text = _call_gemini(user, api_key)
        elif provider == "ollama":
            text = _call_ollama(user, api_key)
        else:
            return []
    except Exception as exc:
        print(f"[ai-gateway] provider call failed: {exc}", file=sys.stderr)
        return []
    return _parse_json_array(text)


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _is_gateway(el) -> bool:
    return el is not None and _local(el.tag).endswith("Gateway")


def _add_io(node: ET.Element, kind: str, fid: str) -> None:
    """Append an <incoming>/<outgoing> child referencing flow id `fid`."""
    e = ET.SubElement(node, f"{{{BPMN_NS}}}{kind}")
    e.text = fid


def _replace_io(node: ET.Element, kind: str, old: str, new: str) -> None:
    """Repoint an existing <incoming>/<outgoing> child from `old` to `new`."""
    if node is None:
        return
    for c in node.findall(f"{{{BPMN_NS}}}{kind}"):
        if c.text and c.text.strip() == old:
            c.text = new
            return
    _add_io(node, kind, new)


def apply_gateways(xml: str, edits: List[Dict]) -> Tuple[str, int]:
    """Apply loop-back decision-gateway edits. Returns (new_xml, count).

    For each decision task D in a chain  ..A -> D -> C..  this inserts:

        A --> [M] --> D --> [G] --(positive: happy_label)--> C
                       ^                |
                       +--(negative: neg_label)-------------+

      * [M] is a MERGE (XOR join) gateway placed *before* D - the rework
        loop re-enters here, so a failed outcome re-runs D ("the negative
        loop of the gateway goes to the previous step").
      * [G] is the DECISION (XOR split) gateway placed *after* D, labelled
        with the question.

    Only the BPMN *process* structure is created here (nodes, flows, lane
    refs, incoming/outgoing). signavio_normalize adds the BPMNShape/Edge
    and bpmn_layout_fixer positions + routes them (the loop edge is routed
    specially via its `_loopback` id suffix).
    """
    if not edits:
        return xml, 0
    if 'xmlns="' + BPMN_NS + '"' in xml:
        ET.register_namespace("", BPMN_NS)
    else:
        ET.register_namespace("bpmn", BPMN_NS)
    for pfx, uri in (("bpmndi", "http://www.omg.org/spec/BPMN/20100524/DI"),
                     ("dc", "http://www.omg.org/spec/DD/20100524/DC"),
                     ("di", "http://www.omg.org/spec/DD/20100524/DI"),
                     ("xsi", "http://www.w3.org/2001/XMLSchema-instance")):
        ET.register_namespace(pfx, uri)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return xml, 0

    proc = root.find(f"{{{BPMN_NS}}}process")
    if proc is None:
        return xml, 0

    existing_ids = {el.get("id") for el in root.iter() if el.get("id")}
    node_by_id = {el.get("id"): el for el in proc.iter() if el.get("id")}
    # first sequenceFlow into / out of each node
    in_flow: Dict[str, ET.Element] = {}
    out_flow: Dict[str, ET.Element] = {}
    for sf in proc.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        out_flow.setdefault(sf.get("sourceRef"), sf)
        in_flow.setdefault(sf.get("targetRef"), sf)

    SF = f"{{{BPMN_NS}}}sequenceFlow"
    GW = f"{{{BPMN_NS}}}exclusiveGateway"

    applied = 0
    gw_n = 1

    def _next_gw_id() -> str:
        nonlocal gw_n
        while f"Gateway_{gw_n}" in existing_ids:
            gw_n += 1
        gid = f"Gateway_{gw_n}"
        existing_ids.add(gid)
        gw_n += 1
        return gid

    def _lane_of(node_id: str):
        for lane in proc.iter(f"{{{BPMN_NS}}}lane"):
            for r in lane.findall(f"{{{BPMN_NS}}}flowNodeRef"):
                if r.text and r.text.strip() == node_id:
                    return lane
        return None

    for edit in edits:
        D = edit.get("decision_task_id")
        if D not in existing_ids:
            continue
        f_in_D  = in_flow.get(D)     # P -> D
        f_out_D = out_flow.get(D)    # D -> C
        if f_in_D is None or f_out_D is None:
            continue
        # idempotency / no double-gate: skip if D already feeds a gateway.
        if _is_gateway(node_by_id.get(f_out_D.get("targetRef"))):
            continue

        question = (edit.get("question") or "Decision?").strip()
        happy    = (edit.get("happy_label") or "Yes").strip()
        neg      = (edit.get("neg_label") or "No").strip()

        # 2-STEP loop: the MERGE sits before the PREDECESSOR task of D, so the
        # negative branch re-runs predecessor + decision (the "2 steps behind"
        # the modeller asked for). Fall back to 1 step (merge before D) when
        # there is no eligible predecessor task (D first, or its predecessor is
        # already a gateway).
        P = f_in_D.get("sourceRef")
        P_node = node_by_id.get(P)
        f_in_loop = in_flow.get(P)
        if (P_node is not None and _local(P_node.tag) in _TASK_TAGS
                and f_in_loop is not None
                and not _is_gateway(node_by_id.get(f_in_loop.get("sourceRef")))):
            loop_target = P            # 2-step (merge before predecessor)
        else:
            loop_target = D            # 1-step fallback (merge before D)
            f_in_loop = f_in_D

        mid = _next_gw_id()
        gid = _next_gw_id()
        M = ET.SubElement(proc, GW, {"id": mid, "gatewayDirection": "Converging"})
        G = ET.SubElement(proc, GW, {"id": gid, "name": question,
                                     "gatewayDirection": "Diverging"})

        f_in_loop_id = f_in_loop.get("id")
        f_out_id     = f_out_D.get("id")
        f_M    = f"{mid}_{loop_target}"
        f_DG   = f"{D}_{gid}"
        f_loop = f"{gid}_loopback"

        # Y -> M (reuse the loop-target's incoming flow id), then M -> target
        f_in_loop.set("targetRef", mid)
        ET.SubElement(proc, SF, {"id": f_M, "sourceRef": mid, "targetRef": loop_target})
        # D -> G (new), then G -> C (reuse D's outgoing flow id; positive path)
        f_out_D.set("sourceRef", gid)
        f_out_D.set("name", happy)
        ET.SubElement(proc, SF, {"id": f_DG, "sourceRef": D, "targetRef": gid})
        # G -> M loop back (negative outcome re-runs the previous step(s))
        ET.SubElement(proc, SF, {"id": f_loop, "sourceRef": gid,
                                 "targetRef": mid, "name": neg})

        # incoming/outgoing children
        _add_io(M, "incoming", f_in_loop_id)
        _add_io(M, "incoming", f_loop)
        _add_io(M, "outgoing", f_M)
        _add_io(G, "incoming", f_DG)
        _add_io(G, "outgoing", f_out_id)
        _add_io(G, "outgoing", f_loop)
        _replace_io(node_by_id.get(loop_target), "incoming", f_in_loop_id, f_M)
        _replace_io(node_by_id.get(D), "outgoing", f_out_id, f_DG)

        # merge -> loop-target's lane ; decision -> D's lane (may differ)
        lane_m = _lane_of(loop_target)
        if lane_m is not None:
            ET.SubElement(lane_m, f"{{{BPMN_NS}}}flowNodeRef").text = mid
        lane_g = _lane_of(D)
        if lane_g is not None:
            ET.SubElement(lane_g, f"{{{BPMN_NS}}}flowNodeRef").text = gid

        # register the new gateways so a later edit on an adjacent task sees
        # them (flows are mutated in place, so cached flow elements stay valid)
        node_by_id[mid] = M
        node_by_id[gid] = G
        applied += 1

    if applied == 0:
        return xml, 0

    new_xml = ET.tostring(root, encoding="unicode")
    if not new_xml.lstrip().startswith("<?xml"):
        new_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + new_xml
    return new_xml, applied


def insert_gateways(xml: str, provider: str, api_key: str) -> Tuple[str, int]:
    """AI-first, heuristic-fallback decision-gateway insertion.

    Both paths produce the SAME loop-back edit shape consumed by
    apply_gateways:
        {"decision_task_id", "question", "happy_label", "neg_label"}
    The AI is used only to *find* the decision tasks and phrase the
    question; the negative branch always loops back to retry the task, so
    the AI's outcome targets are mapped to (happy_label, neg_label) only.
    """
    edits: List[Dict] = []
    for s in suggest_gateways(xml, provider, api_key):
        D = s.get("after_task_id")
        if not D:
            continue
        # Branches are always Yes/No (the modeller's convention); only the
        # AI's question is kept. If the AI gives no question, derive one.
        q = (s.get("question") or "").strip()
        edits.append({
            "decision_task_id": D,
            "question": q,
            "happy_label": "Yes",
            "neg_label": "No",
        })
    if not edits:
        edits = _heuristic_gateways(xml)
    return apply_gateways(xml, edits)


# ---- deterministic heuristic (no AI / no key needed) ----

_DECISION_KW = ("attempt", "validate", "check", "verify", "review", "assess",
                "confirm", "approve", "evaluate", "inspect", "decide",
                "determine", "screen", "authorise", "authorize", "qualify")
_EXCEPTION_KW = ("exception", "handle", "reject", "cancel", "fail", "escalate",
                 "error", "retry", "redeliver", "return", "rework", "dispute",
                 "rollback", "abort")


def _decision_labels(nlow: str) -> Tuple[str, str, str]:
    """(question, positive-branch, negative-branch). The question is an
    "Is ...?" yes/no question carrying an embedded newline so Signavio renders
    it on ~2 lines (matching the modeller's hand-tuned reference); the branches
    are Yes / No - "No" is the loop-back that re-runs the previous step(s)."""
    if "deliver" in nlow or "attempt" in nlow:
        q = "Is delivery\nsuccessful?"
    elif "validat" in nlow:
        q = "Is order\nvalidated?"
    elif "approv" in nlow:
        q = "Is it\napproved?"
    elif "verif" in nlow:
        q = "Is it\nverified?"
    elif "inspect" in nlow:
        q = "Is inspection\npassed?"
    elif "check" in nlow:
        q = "Is check\npassed?"
    elif "assess" in nlow or "evaluat" in nlow:
        q = "Is assessment\npassed?"
    elif "qualif" in nlow:
        q = "Is it\nqualified?"
    elif "confirm" in nlow:
        q = "Is it\nconfirmed?"
    elif "review" in nlow:
        q = "Is review\npassed?"
    else:
        q = "Is it\ncompleted?"
    return q, "Yes", "No"


def _tokens(name: str) -> set:
    """Whole-word tokens of a name, lower-cased (so 'confirmation' does not
    match the keyword 'confirm')."""
    return set(re.findall(r"[a-z]+", (name or "").lower()))


def _has_kw(name: str, kws) -> bool:
    toks = _tokens(name)
    # match keyword as a whole word, or as a stem prefix for -ing/-ed/-ion
    for k in kws:
        if k in toks:
            return True
        for t in toks:
            if t.startswith(k) and len(t) - len(k) <= 3 and t != k + "ation":
                return True
    return False


def _heuristic_gateways(xml: str) -> List[Dict]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    task_tags = {"task","userTask","serviceTask","scriptTask","manualTask",
                 "sendTask","receiveTask","businessRuleTask","callActivity","subProcess"}
    names: Dict[str, str] = {}
    tag: Dict[str, str] = {}
    for el in root.iter():
        eid = el.get("id")
        if eid:
            names[eid] = (el.get("name") or "").lower()
            tag[eid] = el.tag.split("}")[-1]
    succ: Dict[str, str] = {}
    for sf in root.iter(f"{{{BPMN_NS}}}sequenceFlow"):
        s, t = sf.get("sourceRef"), sf.get("targetRef")
        if s and t:
            succ.setdefault(s, t)   # linear: first successor

    # task ids in flow order (follow succ from the start event / first task)
    ordered = _flow_order(root, succ, tag, task_tags)

    edits: List[Dict] = []
    used: set = set()
    for D in ordered:
        if D in used or not _has_kw(names[D], _DECISION_KW):
            continue
        # D must sit in a chain (have both a predecessor and a successor) so
        # a merge can go before it and a decision after it.
        has_pred = any(t == D for (_, t) in
                       ((sf.get("sourceRef"), sf.get("targetRef"))
                        for sf in root.iter(f"{{{BPMN_NS}}}sequenceFlow")))
        if not has_pred or not succ.get(D):
            continue
        q, happy, neg = _decision_labels(names[D])
        edits.append({
            "decision_task_id": D,
            "question": q,
            "happy_label": happy,
            "neg_label": neg,
        })
        used.add(D)
        if len(edits) >= 3:
            break
    return edits


def _flow_order(root, succ, tag, task_tags) -> List[str]:
    """Return task ids roughly in flow order by walking sequence flows from
    the start event. Falls back to document order for anything unreached."""
    start = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "startEvent":
            start = el.get("id"); break
    order: List[str] = []
    seen: set = set()
    cur = succ.get(start) if start else None
    hops = 0
    while cur and cur not in seen and hops < 200:
        seen.add(cur)
        if tag.get(cur) in task_tags:
            order.append(cur)
        cur = succ.get(cur); hops += 1
    # append any tasks not reached, in document order
    for el in root.iter():
        eid = el.get("id")
        if eid and tag.get(eid) in task_tags and eid not in seen:
            order.append(eid)
    return order


# ---- provider dispatch ----


def _call_bedrock(user_msg: str, api_key: str = "") -> str:
    """AWS port: the Lambda's IAM role authorises Bedrock, so no key is used."""
    from .bedrock_provider import call_bedrock
    return call_bedrock(_GATEWAY_SYSTEM_PROMPT, user_msg, max_tokens=4000)

def _call_anthropic(user, key):
    import anthropic
    c = anthropic.Anthropic(api_key=key)
    r = c.messages.create(model="claude-opus-4-5", max_tokens=4000,
                          system=_GATEWAY_SYSTEM_PROMPT,
                          messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in r.content if hasattr(b, "text"))


def _call_gemini(user, key):
    import google.generativeai as genai
    genai.configure(api_key=key)
    m = genai.GenerativeModel("gemini-2.0-flash",
                              system_instruction=_GATEWAY_SYSTEM_PROMPT)
    return m.generate_content(user,
        generation_config=genai.GenerationConfig(max_output_tokens=4000)).text or ""


def _call_ollama(user, key):
    import urllib.request
    payload = {"model": key or "llama3.2",
               "messages": [{"role": "system", "content": _GATEWAY_SYSTEM_PROMPT},
                            {"role": "user", "content": user}],
               "stream": False}
    req = urllib.request.Request("http://localhost:11434/api/chat",
        data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode()).get("message", {}).get("content", "")


def _parse_json_array(text: str) -> List[Dict]:
    if not text:
        return []
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text)
    s = text.find("[")
    if s < 0:
        return []
    depth = 0; instr = False; esc = False; e = -1
    for i in range(s, len(text)):
        ch = text[i]
        if esc: esc = False; continue
        if ch == "\\": esc = True; continue
        if ch == '"': instr = not instr; continue
        if instr: continue
        if ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0: e = i + 1; break
    if e < 0:
        return []
    try:
        r = json.loads(text[s:e])
    except json.JSONDecodeError:
        return []
    return r if isinstance(r, list) else []
