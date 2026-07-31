"""AI-driven naming-convention enforcement for BPMN tasks (v1.2.14).

Background
----------
The deterministic verb-at-start rule in `bpmn_advanced_rules` and the
`_BPMN_VERBS` dictionary in `local_uplift` together catch most bad task
names ("Order place", "Purchase order check", etc.). But the dictionary
will always miss something: domain-specific verbs the user encounters,
new compound names, ambiguous noun-verb words, etc.

This module adds a complementary AI pass that runs AFTER the rule
engine. It sends the LIST of remaining task names (and their element
IDs) to the user's selected AI provider, asks for verb-first rewrites,
and applies the suggestions back to the BPMN. The AI sees only names,
not the full XML, so it's fast and cheap (a few hundred tokens at most).

Sanity guards
-------------
The AI's response is parsed strictly:
  - Must be a JSON object mapping known element IDs to strings
  - Suggestions are silently dropped if they invent words not present in
    the original (computed via word-set diff)
  - If the API call fails or returns garbage, the function falls back
    to no changes - never blocks the uplift

Public API
----------
    check_task_names_with_ai(
        task_names: dict[str, str],
        provider:   str,
        api_key:    str,
    ) -> dict[str, str]                # id -> new_name (only changed)
"""
from __future__ import annotations

import json
import re
import sys
from typing import Dict


_NAMING_SYSTEM_PROMPT = """You are a BPMN 2.0 naming-convention enforcer.

You will receive a list of TASK names from a BPMN diagram (as a JSON object
mapping element IDs to current names).

For each task, decide if it follows the standard convention:
  * TASKS must START with an ACTION VERB in present tense
  * Format: "[Verb] [object]"  (e.g. "Approve invoice", "Send notification",
    "Check purchase order")

If a name doesn't start with a verb but a verb appears LATER in the name,
REORDER it so the verb is first. If the name has no clear action verb at
all, LEAVE IT ALONE - don't invent words. If the name is already correct,
LEAVE IT ALONE.

CRITICAL OUTPUT RULES:
  * Return ONLY a single JSON object. No commentary, no markdown fences.
  * The JSON object maps element ID -> CORRECTED name string.
  * INCLUDE only the IDs whose names you're actually CHANGING.
  * If no tasks need changing, return exactly: {}
  * The new name must use ONLY words that appeared in the original
    (re-cased, reordered, or with minor connectives like "the"/"a"/"of"
    is fine - but never invent unrelated words).
  * Keep names concise (max 7 words).

Examples of corrections you SHOULD make:
  "purchase order check"          -> "Check purchase order"
  "Order list review"             -> "Review order list"
  "Status update by user"         -> "Update status by user"
  "Vendor information collect"    -> "Collect vendor information"

Examples you should LEAVE UNCHANGED:
  "Approve invoice"               (already verb-first)
  "Specify and quantify requirements" (already verb-first)
  "Information"                   (single noun, no verb to extract)
  "Inventory levels"              (no verb anywhere)

Return ONLY the JSON object.
"""


def check_task_names_with_ai(
    task_names: Dict[str, str],
    provider:   str,
    api_key:    str,
) -> Dict[str, str]:
    """Ask the AI provider to rewrite any task names that don't follow the
    verb-first convention. Returns a dict of changed names only.
    """
    if not task_names or provider == "local" or (not api_key and provider != "bedrock"):
        return {}

    payload = json.dumps(task_names, indent=2, ensure_ascii=False)
    user_msg = "Task names to review:\n" + payload

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
        print(f"[ai-naming] provider call failed: {exc}", file=sys.stderr)
        return {}

    fixes = _parse_json_object(text)

    # Drop suggestions that are not for known IDs.
    cleaned: Dict[str, str] = {}
    for tid, new_name in fixes.items():
        if tid not in task_names:
            continue
        if not isinstance(new_name, str) or not new_name.strip():
            continue
        new_name = new_name.strip()
        if new_name == task_names[tid]:
            continue
        # Veto if the AI invented words not in the original.
        if _has_invented_words(task_names[tid], new_name):
            continue
        cleaned[tid] = new_name

    return cleaned


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------


def _call_bedrock(user_msg: str, api_key: str = "") -> str:
    """AWS port: the Lambda's IAM role authorises Bedrock, so no key is used."""
    from .bedrock_provider import call_bedrock
    return call_bedrock(_NAMING_SYSTEM_PROMPT, user_msg, max_tokens=4000)

def _call_anthropic(user_msg: str, api_key: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    resp = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        system=_NAMING_SYSTEM_PROMPT,
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
        system_instruction=_NAMING_SYSTEM_PROMPT,
    )
    resp = model.generate_content(
        user_msg,
        generation_config=genai.GenerationConfig(max_output_tokens=4000),
    )
    return resp.text or ""


def _call_ollama(user_msg: str, api_key: str) -> str:
    import urllib.request
    model = api_key or "llama3.2"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _NAMING_SYSTEM_PROMPT},
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
    obj = json.loads(body)
    return obj.get("message", {}).get("content", "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_object(text: str) -> Dict[str, str]:
    """Locate the FIRST top-level JSON object in `text` and parse it."""
    if not text:
        return {}
    # Strip markdown fences in case the AI ignored the no-fence rule.
    text = re.sub(r"^```[a-zA-Z]*\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text)
    # Find a balanced {...} block.
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
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
    if not isinstance(result, dict):
        return {}
    return {str(k): str(v) for k, v in result.items()}


def _has_invented_words(original: str, suggested: str) -> bool:
    """Return True if `suggested` contains a word not in `original` (after
    lower-casing and stripping punctuation).

    Connectives like "the", "a", "of", "to", "for", "and", "or", "with",
    "from" are exempt - the AI is allowed to add small linkers.
    """
    _CONNECTIVES = {"the", "a", "an", "of", "to", "for", "and", "or",
                    "with", "from", "by", "on", "in", "at", "as", "into"}

    def words(s: str) -> set:
        cleaned = re.sub(r"[^a-zA-Z0-9\s]", " ", s).lower()
        return {w for w in cleaned.split() if w}

    orig_words = words(original)
    sugg_words = words(suggested)
    new_words = sugg_words - orig_words - _CONNECTIVES
    return bool(new_words)
