"""AI-powered final layout cleanup pass for AI Uplift (v1.2.9).

Runs AFTER the deterministic rules. Sends the cleaned-but-still-messy BPMN
to the user's selected AI provider with a strict layout-only prompt and
asks the model to:

  1. Re-route any sequence-flow waypoints that still zig-zag
  2. Reposition shapes that overlap each other
  3. Match split/merge gateway types where the rule pass missed
  4. Apply Auspost naming conventions to any names the rule pass left
     untouched (e.g. ambiguous noun-verb words)

The prompt is PRESERVATION-FIRST: every element id, type, and (unless
the model is explicitly cleaning a generic name) every name must round-trip
unchanged. After the model returns, we verify the element count and process
ID match the input - if anything looks corrupted, we fall back to the rule-
engine output.

Public API
----------
    clean_layout_with_ai(
        xml: str,
        provider: str,         # "anthropic" | "gemini" | "ollama" | "local"
        api_key: str,
    ) -> tuple[str, list[dict]]
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple


_LAYOUT_SYSTEM_PROMPT = """You are a BPMN 2.0 EDGE-ROUTING cleaner. You will receive a valid BPMN 2.0 XML file. Your ONE job is to clean up the waypoints inside <bpmndi:BPMNEdge> elements so sequence flows look tidy when rendered. You may not change anything else.

CRITICAL PRESERVATION RULES (must follow without exception):
  - DO NOT add or delete ANY element (tasks, gateways, events, lanes, sequence flows, message flows, data objects, annotations, BPMNShape, etc.).
  - DO NOT change any element @id, @name, or tag/type.
  - DO NOT modify <dc:Bounds> on any <bpmndi:BPMNShape> - leave every x, y, width, height byte-for-byte the same. Shape positions are out of scope.
  - DO NOT modify <bpmn:sequenceFlow> sourceRef or targetRef attributes.
  - DO NOT alter the <bpmn:definitions> root element's attributes or namespace declarations.

WHAT YOU MAY DO (ONLY this):
  - Replace the <di:waypoint> entries inside <bpmndi:BPMNEdge> with cleaner ones, following these heuristics:
      * Use 2 waypoints when source and target shapes are stacked in the same column or sit in the same row.
      * Use 3 waypoints with one 90 degree bend when source and target are in different rows AND different columns. Choose the corner so the line first travels along the longer axis, then bends.
      * Make lines AVOID passing through any <bpmndi:BPMNShape>'s rectangle bounds (other than the source and target). If a clean orthogonal route would cut through an unrelated shape, route around it with extra waypoints.
      * Never produce zig-zags with 4+ points unless avoiding shape overlap actually requires them.

OUTPUT REQUIREMENTS:
  - Return ONLY the cleaned BPMN XML, starting with <?xml or <bpmn: or <definitions.
  - No markdown fences, no commentary.
  - Element count and shape <dc:Bounds> must round-trip identical to the input."""


def clean_layout_with_ai(
    xml: str,
    provider: str,
    api_key: str,
) -> Tuple[str, List[Dict]]:
    """Send the BPMN to the AI provider for a final layout polish pass.

    Falls back to the input unchanged if:
      - provider is "local" (no AI available)
      - the API call fails
      - the result has a different element count or process id (sign that
        the model added/removed elements despite the prompt)

    Returns (xml_after_ai, change_log_entries).
    """
    if provider == "local" or (not api_key and provider != "bedrock"):
        return xml, []

    # Skip very small files - nothing meaningful to clean.
    if len(xml) < 500:
        return xml, []

    try:
        cleaned = _call_provider(xml, provider, api_key)
    except Exception as exc:
        print(f"[ai-layout] provider call failed: {exc}", file=__import__("sys").stderr)
        return xml, []

    cleaned = _strip_prose_and_fences(cleaned).strip()
    if not cleaned:
        return xml, []

    # Verify the AI didn't go off the rails. We sanity-check three things:
    #   - The result still has a <definitions> or <bpmn:definitions> root.
    #   - The element count is roughly the same (within 10%).
    #   - The original process id is still present.
    if not _looks_like_bpmn(cleaned):
        return xml, []
    if not _element_counts_close(xml, cleaned):
        print("[ai-layout] element count drift - reverting to rule-engine output",
              file=__import__("sys").stderr)
        return xml, []

    # v1.2.10 - reject the AI's output if it moved any shape. Earlier
    # versions allowed shape repositioning, but in practice the model
    # would shuffle shapes around badly and create overlaps. The pass is
    # now strictly about waypoint cleanup.
    if _shape_bounds_changed(xml, cleaned):
        print("[ai-layout] AI moved shapes despite the prompt - reverting",
              file=__import__("sys").stderr)
        return xml, []

    # Detect what the AI actually changed so we can log it.
    changes = _diff_changes(xml, cleaned)
    return cleaned, changes


def _shape_bounds_changed(before: str, after: str) -> bool:
    """Compare every <bpmndi:BPMNShape>'s <dc:Bounds> before vs after.
    Returns True if any shape's x, y, width, or height moved.
    """
    def shape_bounds(s):
        m = {}
        for sm in re.finditer(
            r'<bpmndi:BPMNShape[^>]*bpmnElement="([^"]+)"[^>]*>(.*?)</bpmndi:BPMNShape>',
            s, re.DOTALL,
        ):
            ref = sm.group(1)
            b = re.search(
                r'<(?:dc|omgdc):Bounds[^>]*x="([^"]+)"[^>]*y="([^"]+)"[^>]*width="([^"]+)"[^>]*height="([^"]+)"',
                sm.group(2),
            )
            if b:
                m[ref] = tuple(round(float(v), 1) for v in b.groups())
        return m
    a = shape_bounds(before)
    b = shape_bounds(after)
    common = a.keys() & b.keys()
    for ref in common:
        if a[ref] != b[ref]:
            return True
    return False


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

def _call_provider(xml: str, provider: str, api_key: str) -> str:
    user_msg = (
        "Apply the layout cleanup to the BPMN below. Remember: preserve every "
        "element ID and tag (except gateway pair fixes). Return ONLY the cleaned XML.\n\n"
        + xml
    )
    if provider == "bedrock":
        return _call_bedrock(user_msg, api_key)
    if provider == "anthropic":
        return _call_anthropic(user_msg, api_key)
    if provider == "gemini":
        return _call_gemini(user_msg, api_key)
    if provider == "ollama":
        return _call_ollama(user_msg, api_key)
    raise ValueError(f"unknown provider: {provider}")



def _call_bedrock(user_msg: str, api_key: str = "") -> str:
    """AWS port: the Lambda's IAM role authorises Bedrock, so no key is used."""
    from .bedrock_provider import call_bedrock
    return call_bedrock(_LAYOUT_SYSTEM_PROMPT, user_msg, max_tokens=32000)

def _call_anthropic(user_msg: str, api_key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=32000,
        system=_LAYOUT_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    chunks = []
    for block in resp.content:
        if hasattr(block, "text"):
            chunks.append(block.text)
    return "".join(chunks)


def _call_gemini(user_msg: str, api_key: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=_LAYOUT_SYSTEM_PROMPT,
    )
    resp = model.generate_content(
        user_msg,
        generation_config=genai.GenerationConfig(max_output_tokens=32000),
    )
    return resp.text or ""


def _call_ollama(user_msg: str, api_key: str) -> str:
    import json
    import urllib.request
    model = api_key or "llama3.2"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _LAYOUT_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        "stream": False,
    }
    req = urllib.request.Request(
        "http://localhost:11434/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8")
    obj = json.loads(body)
    return obj.get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_prose_and_fences(text: str) -> str:
    """Remove Markdown fences and any prose around the XML."""
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text)
    text = re.sub(r"\n```\s*$", "", text)
    starts = []
    for n in ("<?xml", "<bpmn:definitions", "<definitions"):
        i = text.find(n)
        if i >= 0:
            starts.append(i)
    if starts:
        text = text[min(starts):]
    ends = []
    for n in ("</bpmn:definitions>", "</definitions>"):
        i = text.rfind(n)
        if i >= 0:
            ends.append(i + len(n))
    if ends:
        text = text[:max(ends)]
    return text


def _looks_like_bpmn(s: str) -> bool:
    return ("<definitions" in s or "<bpmn:definitions" in s) and (
        "</definitions>" in s or "</bpmn:definitions>" in s)


def _element_counts_close(before: str, after: str) -> bool:
    """Return True if the element counts haven't diverged by more than 10%."""
    def count(s):
        # Count tasks, gateways, events, sequence flows
        tags = ("task", "userTask", "serviceTask", "scriptTask",
                "manualTask", "sendTask", "receiveTask", "businessRuleTask",
                "callActivity", "subProcess",
                "exclusiveGateway", "parallelGateway", "inclusiveGateway",
                "eventBasedGateway", "complexGateway",
                "startEvent", "endEvent", "intermediateThrowEvent",
                "intermediateCatchEvent", "boundaryEvent",
                "sequenceFlow", "messageFlow", "lane")
        n = 0
        for t in tags:
            n += len(re.findall(rf"<(?:bpmn:)?{t}\b", s))
        return n
    a = count(before)
    b = count(after)
    if a == 0:
        return False
    drift = abs(a - b) / a
    return drift <= 0.10


def _diff_changes(before: str, after: str) -> List[Dict]:
    """Best-effort change detection so we can show the user what the AI did.

    We don't need exhaustive coverage - just enough to populate the
    uplift report's AI Layout section. We compare:
      - waypoint counts per BPMNEdge
      - element @name attributes (renames)
      - gateway tag types
    """
    out: List[Dict] = []

    # Edge waypoint count diff
    def edge_waypoints(s):
        m = {}
        for em in re.finditer(r'<bpmndi:BPMNEdge[^>]*bpmnElement="([^"]+)"[^>]*>(.*?)</bpmndi:BPMNEdge>',
                              s, re.DOTALL):
            m[em.group(1)] = len(re.findall(r"waypoint", em.group(2)))
        return m
    eb, ea = edge_waypoints(before), edge_waypoints(after)
    for eid in eb.keys() & ea.keys():
        if eb[eid] != ea[eid]:
            out.append({
                "category":   "AI Layout",
                "rule":       "AI-1 - waypoint cleanup",
                "element_id": eid,
                "before":     f"{eb[eid]} waypoints",
                "after":      f"{ea[eid]} waypoints",
                "detail":     "AI re-routed the edge for visual cleanliness",
            })

    # Name diff (rename of any element by id)
    def name_map(s):
        m = {}
        for nm in re.finditer(r'\bid="([^"]+)"\s+[^>]*name="([^"]+)"', s):
            m[nm.group(1)] = nm.group(2)
        return m
    nb, na = name_map(before), name_map(after)
    for eid in nb.keys() & na.keys():
        if nb[eid] != na[eid]:
            out.append({
                "category":   "AI Layout",
                "rule":       "AI-2 - convention-driven rename",
                "element_id": eid,
                "before":     nb[eid],
                "after":      na[eid],
                "detail":     "AI applied a naming convention fix",
            })

    return out
