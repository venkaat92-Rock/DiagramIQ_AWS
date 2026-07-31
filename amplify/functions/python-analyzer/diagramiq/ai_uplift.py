"""AI-powered BPMN uplift — supports Anthropic, Google Gemini, Ollama, and Built-in."""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable, List, Optional

from .bpmn_validator import ValidationIssue
from .local_uplift import uplift_local

try:
    from .auspost_conventions import AI_PROMPT_BLOCK as _AUSPOST_BLOCK
except ImportError:
    _AUSPOST_BLOCK = ""

UPLIFT_SYSTEM = (_AUSPOST_BLOCK + "\n\n" if _AUSPOST_BLOCK else "") + """You are a BPMN 2.0 expert. You receive a BPMN 2.0 XML document and a list of validation issues.
Return an improved BPMN 2.0 XML that:
1. Fixes all ERROR-level validation issues
2. Addresses WARNING-level issues where possible
3. ONLY rename elements that have generic names like 'Task 1', 'Gateway 2', 'Node 3'
4. PRESERVE all existing meaningful names — do NOT rename tasks, events, or gateways that already have descriptive names
5. PRESERVE the process name attribute exactly as it appears in the input XML
6. Adds missing start/end events if needed
7. Fixes gateway splits and joins according to BPMN 2.0 best practices
8. Do NOT split gateways that serve as both join and split — this is valid BPMN
9. REMOVE orphan tasks - any task, event, or gateway that has NO incoming AND
   NO outgoing sequence flow is disconnected and MUST be deleted along with
   its corresponding bpmndi:BPMNShape entry. Do not try to "rescue" them by
   inventing new flows.

CRITICAL LAYOUT REQUIREMENT — The output MUST include a complete bpmndi:BPMNDiagram section with:
- bpmndi:BPMNShape for EVERY element with dc:Bounds (x, y, width, height)
- bpmndi:BPMNEdge for EVERY sequenceFlow with at least two di:waypoint entries
- PRESERVE existing shape positions (x, y coordinates) from the input XML wherever possible
- Only compute new positions for elements you add (missing start/end events, new gateways)
- Place new elements near their connected neighbours in the existing layout
- Standard sizes: startEvent/endEvent 36x36, exclusiveGateway 50x50, task 120x80
- Required namespaces on definitions: xmlns:bpmndi, xmlns:dc, xmlns:di

This DI section is MANDATORY — without it the file cannot be imported into Signavio or Camunda.

IMPORTANT: Return ONLY the raw BPMN 2.0 XML starting with <?xml. No explanation, no markdown, no code fences."""


def _strip_fences(text: str) -> str:
    """Strip Markdown fences AND run the Signavio-compatibility normaliser
    so the AI's raw output is always Signavio-importable.

    v1.2.3 - the Markdown-fence stripper alone wasn't enough; AI providers
    sometimes prepend explanatory prose ("Here's the uplifted BPMN:") or
    emit BPMN that is XML-valid but missing required structural attributes
    (targetNamespace, BPMNDiagram, BPMNShape entries, ...). The Signavio
    normaliser fixes all of those so what reaches the user is always a
    file Signavio's importer accepts.
    """
    text = text.strip()
    # Quick fence strip first to keep a fast path for the common case.
    if "```xml" in text:
        text = text.split("```xml", 1)[1].split("```", 1)[0].strip()
    elif text.startswith("```"):
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    # Then run the comprehensive normaliser. It is idempotent and safe to
    # run on already-clean files.
    try:
        from .signavio_normalize import normalize_for_signavio
        text = normalize_for_signavio(text)
    except Exception as exc:
        # Never let normalisation break the upload - fall back to the raw
        # fence-stripped text if anything goes wrong.
        print(f"[normalize] could not normalise BPMN: {exc}", file=__import__("sys").stderr)

    return text


def _build_user_message(
    xml_content: str,
    issues: List[ValidationIssue],
    process_name: str,
    signavio_categories: str = "",
) -> str:
    issues_text = (
        "\n".join(str(i) for i in issues) if issues
        else "No validation issues found — apply general BPMN 2.0 best practices."
    )
    sig_context = ""
    if signavio_categories:
        sig_context = (
            f"\nSIGNAVIO BEST-PRACTICE FOCUS AREAS: {signavio_categories}\n"
            f"Prioritise fixing issues from these Signavio rule categories. "
            f"Apply SAP Signavio modeling guidelines strictly for these areas.\n"
        )
    return (
        f"Process name: {process_name}\n{sig_context}\n"
        f"VALIDATION ISSUES TO FIX:\n{issues_text}\n\n"
        f"BPMN XML TO IMPROVE:\n{xml_content}"
    )


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def _uplift_bedrock(
    xml_content: str,
    issues: List[ValidationIssue],
    process_name: str,
    api_key: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
    signavio_categories: str = "",
) -> None:
    """AWS port. Non-streaming: the HTTP API buffers the response anyway, so
    streaming would buy nothing and the callbacks stay satisfied."""
    try:
        from .bedrock_provider import call_bedrock
        user_message = _build_user_message(xml_content, issues, process_name, signavio_categories)
        text = call_bedrock(UPLIFT_SYSTEM, user_message, max_tokens=8192)
        if on_chunk:
            on_chunk(text)
        if on_complete:
            on_complete(_strip_fences(text))
    except Exception as exc:
        _handle_error(exc, "Bedrock", on_error)


def _uplift_anthropic(
    xml_content: str,
    issues: List[ValidationIssue],
    process_name: str,
    api_key: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
    signavio_categories: str = "",
) -> None:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        user_message = _build_user_message(xml_content, issues, process_name, signavio_categories)
        collected: list[str] = []

        with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=8192,
            thinking={"type": "adaptive"},
            system=UPLIFT_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            for text in stream.text_stream:
                collected.append(text)
                if on_chunk:
                    on_chunk(text)

        result = _strip_fences("".join(collected))
        if on_complete:
            on_complete(result)

    except Exception as exc:
        _handle_error(exc, "Anthropic", on_error)


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

def _uplift_gemini(
    xml_content: str,
    issues: List[ValidationIssue],
    process_name: str,
    api_key: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
    signavio_categories: str = "",
) -> None:
    try:
        import google.generativeai as genai

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=UPLIFT_SYSTEM,
        )

        user_message = _build_user_message(xml_content, issues, process_name, signavio_categories)
        collected: list[str] = []

        response = model.generate_content(
            user_message,
            generation_config=genai.GenerationConfig(max_output_tokens=8192),
            stream=True,
        )

        for chunk in response:
            text = getattr(chunk, "text", None)
            if text:
                collected.append(text)
                if on_chunk:
                    on_chunk(text)

        result = _strip_fences("".join(collected))
        if on_complete:
            on_complete(result)

    except Exception as exc:
        _handle_error(exc, "Gemini", on_error)


# ---------------------------------------------------------------------------
# Ollama  (free, local — no API key required)
# ---------------------------------------------------------------------------

def _uplift_ollama(
    xml_content: str,
    issues: List,
    process_name: str,
    model: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
    signavio_categories: str = "",
) -> None:
    """Stream BPMN uplift via a locally-running Ollama instance (http://localhost:11434)."""
    user_message = _build_user_message(xml_content, issues, process_name, signavio_categories)
    payload = {
        "model": model or "llama3.2",
        "messages": [
            {"role": "system", "content": UPLIFT_SYSTEM},
            {"role": "user",   "content": user_message},
        ],
        "stream": True,
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        collected: list[str] = []
        with urllib.request.urlopen(req, timeout=300) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    obj  = json.loads(line)
                    text = obj.get("message", {}).get("content", "")
                    if text:
                        collected.append(text)
                        if on_chunk:
                            on_chunk(text)
                    if obj.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue
        result = _strip_fences("".join(collected))
        if on_complete:
            on_complete(result)
    except Exception as exc:
        _handle_error(exc, "Ollama", on_error)


# ---------------------------------------------------------------------------
# Error normaliser
# ---------------------------------------------------------------------------

def _handle_error(exc: Exception, provider: str, on_error: Optional[Callable[[str], None]]) -> None:
    msg = str(exc)
    if provider == "Ollama" and ("connection refused" in msg.lower()
                                  or "10061" in msg or "urlopen" in msg.lower()):
        msg = ("Ollama is not running.\n\n"
               "Start it by opening a terminal and running:  ollama serve\n"
               "Then make sure a model is pulled, e.g.:  ollama pull llama3.2")
    elif "api_key" in msg.lower() or "api key" in msg.lower() or "invalid" in msg.lower():
        msg = f"Invalid {provider} API key. Please check your key in Settings."
    elif "quota" in msg.lower() or "rate" in msg.lower() or "limit" in msg.lower():
        msg = f"{provider} rate limit reached. Please wait and try again."
    elif "network" in msg.lower() or "connect" in msg.lower():
        msg = f"Network error connecting to {provider}. Check your internet connection."
    else:
        msg = f"{provider} AI Uplift failed: {exc}"
    if on_error:
        on_error(msg)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def uplift_with_streaming(
    xml_content: str,
    issues: List[ValidationIssue],
    process_name: str = "Process",
    provider: str = "gemini",
    api_key: Optional[str] = None,
    signavio_categories: str = "",
    on_chunk: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[str], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> None:
    """Dispatch to the selected AI provider and stream the uplifted BPMN XML."""
    # v1.2.1 - deterministic pre-pass: strip orphan tasks before sending to AI.
    # This guarantees orphan removal regardless of which provider is used and
    # avoids burning tokens / context on disconnected nodes.
    try:
        from .bpmn_cleanup import strip_orphans
        xml_content, removed = strip_orphans(xml_content)
        if removed and on_chunk:
            on_chunk(f"<!-- pre-uplift: removed {removed} orphan flow node(s) -->\n")
    except Exception:
        # Never let cleanup crash the uplift; the AI prompt still tells the
        # model to remove orphans as a fallback.
        pass

    if provider == "bedrock":
        # AWS port: the Lambda's IAM role authorises Bedrock, so no key.
        _uplift_bedrock(xml_content, issues, process_name, "", on_chunk, on_complete, on_error,
                        signavio_categories=signavio_categories)

    elif provider == "anthropic":
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            if on_error:
                on_error("Anthropic API key not set. Add it in Settings.")
            return
        _uplift_anthropic(xml_content, issues, process_name, key, on_chunk, on_complete, on_error,
                          signavio_categories=signavio_categories)

    elif provider == "gemini":
        key = api_key or os.environ.get("GOOGLE_API_KEY", "")
        if not key:
            if on_error:
                on_error("Google Gemini API key not set. Add it in Settings.")
            return
        _uplift_gemini(xml_content, issues, process_name, key, on_chunk, on_complete, on_error,
                       signavio_categories=signavio_categories)

    elif provider == "ollama":
        # No API key needed — Ollama runs locally for free.
        model = api_key or os.environ.get("OLLAMA_MODEL", "llama3.2")
        _uplift_ollama(xml_content, issues, process_name, model, on_chunk, on_complete, on_error,
                       signavio_categories=signavio_categories)

    elif provider == "local":
        # Built-in rule-based engine — no external dependencies, no network.
        uplift_local(xml_content, issues, process_name, on_chunk, on_complete, on_error)

    else:
        if on_error:
            on_error(f"Unknown provider '{provider}'.")
