"""AI-driven compliance audit of an uplifted BPMN against the Auspost rule
set (v1.2.15).

After every uplift completes, this module sends:
  1. The final uplifted BPMN XML
  2. The full list of Auspost convention rules (29 items) and self-review
     checklist items (47 items) - 76 total

...to the user's selected AI provider and asks for a per-rule verdict:
  * "Verified"       - BPMN satisfies the rule
  * "Not Verified"   - BPMN violates the rule
  * "Not Applicable" - rule doesn't apply (e.g. DMN rule on a BPMN-only
                       file, or L3-specific rule on an L4 model)

The verdicts plus a short note land on a "Compliance Checklist" sheet
inside the uplift report Excel. Treat the sheet as a scorecard the
uplift workflow tries to drive toward "no rows say Not Verified".

Public API
----------
    check_compliance_with_ai(
        xml: str,
        provider: str,
        api_key: str,
        *,
        max_xml_chars: int = 60000,
    ) -> dict[str, dict[str, str]]    # rule_id -> {"status": .., "notes": ..}

    build_rules_payload() -> list[dict]   # the rules sent to the AI
"""
from __future__ import annotations

import json
import re
import sys
from typing import Dict, List


_COMPLIANCE_SYSTEM_PROMPT = """You are a BPMN 2.0 compliance auditor for Australia Post / SAP Signavio.

You will receive:
  1. An uplifted BPMN 2.0 XML file
  2. A JSON list of modeling rules (each with id, name, description, desired value)

For EACH rule, decide ONE status:
  * "Verified"       - the BPMN clearly satisfies this rule
  * "Not Verified"   - the BPMN clearly violates this rule
  * "Not Applicable" - the rule does not apply to this BPMN (e.g. a DMN
                       rule on a BPMN-only model, an L3-specific rule on
                       an L4 model, an event-naming rule when no events of
                       that type are present, etc.)

For each rule also write a SHORT (one sentence, max 25 words) note
explaining your verdict. When violation, point at a specific element
where possible. When N/A, briefly say why the rule doesn't apply.

CRITICAL OUTPUT RULES:
  * Return ONLY a single JSON object. No commentary, no markdown fences.
  * Format:  {"<rule_id>": {"status": "...", "notes": "..."}, ...}
  * Status MUST be exactly one of: "Verified", "Not Verified", "Not Applicable".
  * Include EVERY rule from the input list - do not skip any.
  * When in doubt between "Verified" and "Not Applicable", prefer "Not Applicable" -
    only mark "Verified" when you can point at concrete evidence in the BPMN.
"""


def build_rules_payload() -> List[Dict[str, str]]:
    """Build the flat rules list sent to the AI for verification."""
    try:
        from .auspost_conventions import CONVENTIONS, CHECKLIST_ITEMS
    except ImportError:
        return []

    rules: List[Dict[str, str]] = []
    for c in CONVENTIONS:
        rules.append({
            "id":          c.rule_id,
            "kind":        "Modeling Convention",
            "category":    c.category or "general",
            "severity":    c.severity,
            "name":        c.name,
            "description": c.description,
            "desired":     c.desired or "",
        })
    for it in CHECKLIST_ITEMS:
        rules.append({
            "id":          it.item_id,
            "kind":        "Self-Review Checklist",
            "category":    f"{it.check_type} / {it.category}",
            "severity":    "",
            "name":        it.standard,
            "description": it.description,
            "desired":     it.format or "",
        })
    return rules


def check_compliance_with_ai(
    xml: str,
    provider: str,
    api_key: str,
    *,
    max_xml_chars: int = 60000,
    rules: List[Dict[str, str]] | None = None,
) -> Dict[str, Dict[str, str]]:
    """Run the compliance audit. Returns {} if skipped or on failure;
    the report writer treats {} as "AI verification not available" and
    falls back to "Pending" status for every rule.

    `rules` audits a subset instead of the whole catalogue. Scoring all 76 in
    one call means one very long generation: slow enough to run into a caller's
    timeout, and long enough that a single malformed token loses every verdict.
    Callers that want it fast and durable split the catalogue and merge.
    """
    if provider == "local" or (not api_key and provider != "bedrock"):
        return {}

    if rules is None:
        rules = build_rules_payload()
    if not rules:
        return {}

    # Trim very long BPMN strings - the audit only needs structure, not
    # full content. The trim threshold is generous enough for any
    # reasonable single-process BPMN file.
    xml_for_audit = xml if len(xml) <= max_xml_chars else (
        xml[:max_xml_chars] + f"\n<!-- ... truncated, {len(xml)-max_xml_chars} chars -->"
    )

    user_msg = (
        "BPMN XML:\n```xml\n" + xml_for_audit + "\n```\n\n"
        "Rules to verify:\n```json\n"
        + json.dumps(rules, indent=2, ensure_ascii=False) + "\n```\n\n"
        f"Audit all {len(rules)} rules. Output JSON only."
    )

    try:
        if provider == "bedrock":
            text = _call_bedrock(user_msg, api_key)
        elif provider == "anthropic":
            text = _call_anthropic(user_msg, api_key)
        elif provider == "gemini":
            text = _call_gemini(user_msg, api_key)
        elif provider == "ollama":
            text = _call_ollama(user_msg, api_key)
        else:
            return {}
    except Exception as exc:
        print(f"[ai-compliance] provider call failed: {exc}", file=sys.stderr)
        return {}

    parsed = _parse_compliance_json(text)
    valid_ids = {r["id"] for r in rules}
    cleaned: Dict[str, Dict[str, str]] = {}
    for rid, val in parsed.items():
        if rid not in valid_ids:
            continue
        if not isinstance(val, dict):
            continue
        status = str(val.get("status") or "").strip()
        if status not in ("Verified", "Not Verified", "Not Applicable"):
            continue
        notes = str(val.get("notes") or "").strip()[:300]
        cleaned[rid] = {"status": status, "notes": notes}
    return cleaned


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


def _call_bedrock(user_msg: str, api_key: str = "") -> str:
    """AWS port: the Lambda's IAM role authorises Bedrock, so no key is used."""
    from .bedrock_provider import call_bedrock
    return call_bedrock(_COMPLIANCE_SYSTEM_PROMPT, user_msg, max_tokens=16000)

def _call_anthropic(user_msg: str, api_key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=16000,
        system=_COMPLIANCE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    parts = []
    for block in resp.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "".join(parts)


def _call_gemini(user_msg: str, api_key: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name="gemini-2.0-flash",
        system_instruction=_COMPLIANCE_SYSTEM_PROMPT,
    )
    resp = model.generate_content(
        user_msg,
        generation_config=genai.GenerationConfig(max_output_tokens=16000),
    )
    return resp.text or ""


def _call_ollama(user_msg: str, api_key: str) -> str:
    import urllib.request
    model = api_key or "llama3.2"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _COMPLIANCE_SYSTEM_PROMPT},
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
# JSON parsing
# ---------------------------------------------------------------------------

def _parse_compliance_json(text: str) -> Dict[str, Dict[str, str]]:
    if not text:
        return {}
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text)
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {}
    try:
        result = json.loads(text[start:end])
    except json.JSONDecodeError:
        return {}
    return result if isinstance(result, dict) else {}
