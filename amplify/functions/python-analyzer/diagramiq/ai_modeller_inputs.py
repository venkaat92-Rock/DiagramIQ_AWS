"""AI identifies what information the process modeller still needs to
provide for the uplifted BPMN to be production-ready (v1.2.16).

This complements the BPMN Checklist (was Compliance Checklist) sheet.
Where the checklist asks "does the diagram pass each rule?", this module
asks the wider question: "what's missing from the diagram that only a
process expert can fill in?".

Typical gaps the AI flags:
  * Task descriptions that say what but not how
  * Gateway conditions that are too generic ("Yes" / "No")
  * Decision criteria nobody documented
  * IT system references missing on tasks that obviously hit a system
  * Input / output documents not specified
  * Roles still showing 'Unassigned' or vague generic role names
  * Compliance triggers (controls, risks, regulations) not linked
  * Frequency / SLA expectations unspecified
  * Edge cases / exception paths not modelled

Public API
----------
    get_modeller_inputs(
        xml: str,
        provider: str,
        api_key: str,
    ) -> list[dict]      # each dict has the fields below
"""
from __future__ import annotations

import json
import re
import sys
from typing import Dict, List


_MODELLER_INPUTS_SYSTEM_PROMPT = """You are a senior BPMN 2.0 process consultant.

You will receive an uplifted BPMN 2.0 XML file. Identify what information
the **process modeller** still needs to provide for the diagram to be
production-ready. You are NOT auditing rule compliance - that's a
separate sheet. You are looking for SEMANTIC gaps that only a process
expert can fill in.

Look for gaps in:
  * Task descriptions / step-by-step documentation
  * Gateway conditions that are too generic
  * Decision criteria / outcome rules
  * IT system references (which systems support which tasks)
  * Input / output documents per task
  * Role assignments still 'Unassigned' or vague
  * Compliance elements (controls, risks, regulations)
  * Frequency / SLA expectations
  * Edge cases / exception handling paths

For EACH gap, return one JSON object with these keys:
  - "category": one of these exact strings:
      "Missing data", "Ambiguous flow", "Missing role",
      "Missing system", "Missing documentation",
      "Missing decision criteria", "Compliance gap",
      "Missing input/output", "Edge case", "SLA/Frequency"
  - "element_id":         the BPMN element @id where the gap exists, or "" if process-wide
  - "element_name":       the element's @name, or "" if process-wide
  - "what_missing":       1-2 sentences describing what info is needed
  - "why_it_matters":     1 sentence explaining the impact if this stays unclear
  - "suggested_question": the specific question to ask the modeller (1 sentence)

Order entries by priority (most critical first). Aim for 5-15 entries -
flag the gaps that genuinely matter, not exhaustive nit-picks.

CRITICAL OUTPUT RULES:
  * Return ONLY a single JSON array. No commentary, no markdown fences.
  * Format: [{"category": "...", "element_id": "...", ...}, ...]
  * If the BPMN is genuinely complete (no important gaps), return: []
  * Every entry MUST have all six fields. Use empty string "" if a field
    doesn't apply, never null.
"""


def get_modeller_inputs(
    xml: str,
    provider: str,
    api_key: str,
    *,
    max_xml_chars: int = 60000,
) -> List[Dict[str, str]]:
    """Run the modeller-input scan. Returns [] if skipped or on failure;
    the report writer treats [] as 'AI scan not available' and renders
    a friendly note in the sheet.
    """
    if provider == "local" or (not api_key and provider != "bedrock"):
        return []

    xml_for_scan = xml if len(xml) <= max_xml_chars else (
        xml[:max_xml_chars] + f"\n<!-- ... truncated, {len(xml) - max_xml_chars} chars -->"
    )

    user_msg = (
        "BPMN to analyse:\n```xml\n" + xml_for_scan + "\n```\n\n"
        "Return ONLY the JSON array of modeller-input gaps."
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
            return []
    except Exception as exc:
        print(f"[ai-modeller-inputs] provider call failed: {exc}", file=sys.stderr)
        return []

    raw = _parse_json_array(text)
    cleaned: List[Dict[str, str]] = []
    allowed_cats = {
        "Missing data", "Ambiguous flow", "Missing role",
        "Missing system", "Missing documentation",
        "Missing decision criteria", "Compliance gap",
        "Missing input/output", "Edge case", "SLA/Frequency",
    }
    for item in raw:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category") or "").strip()
        if cat not in allowed_cats:
            cat = "Missing data"
        cleaned.append({
            "category":           cat,
            "element_id":         str(item.get("element_id")         or "").strip()[:200],
            "element_name":       str(item.get("element_name")       or "").strip()[:200],
            "what_missing":       str(item.get("what_missing")       or "").strip()[:500],
            "why_it_matters":     str(item.get("why_it_matters")     or "").strip()[:500],
            "suggested_question": str(item.get("suggested_question") or "").strip()[:500],
        })
    return cleaned


# ---------------------------------------------------------------------------
# Provider dispatch (mirrors ai_compliance_check.py)
# ---------------------------------------------------------------------------


def _call_bedrock(user_msg: str, api_key: str = "") -> str:
    """AWS port: the Lambda's IAM role authorises Bedrock, so no key is used."""
    from .bedrock_provider import call_bedrock
    return call_bedrock(_MODELLER_INPUTS_SYSTEM_PROMPT, user_msg, max_tokens=8000)

def _call_anthropic(user_msg: str, api_key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=8000,
        system=_MODELLER_INPUTS_SYSTEM_PROMPT,
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
        system_instruction=_MODELLER_INPUTS_SYSTEM_PROMPT,
    )
    resp = model.generate_content(
        user_msg,
        generation_config=genai.GenerationConfig(max_output_tokens=8000),
    )
    return resp.text or ""


def _call_ollama(user_msg: str, api_key: str) -> str:
    import urllib.request
    model = api_key or "llama3.2"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _MODELLER_INPUTS_SYSTEM_PROMPT},
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
# JSON array parsing
# ---------------------------------------------------------------------------

def _parse_json_array(text: str) -> List[Dict]:
    """Locate the first top-level JSON array in `text` and parse it.
    Returns [] on any failure."""
    if not text:
        return []
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text)
    start = text.find("[")
    if start < 0:
        return []
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
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return []
    try:
        result = json.loads(text[start:end])
    except json.JSONDecodeError:
        return []
    return result if isinstance(result, list) else []
