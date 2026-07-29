"""Transcription → Excel pipeline for DiagramIQ.

Reads unstructured text (.txt, .docx, .pdf), sends it to an AI provider
(Anthropic, Gemini, or Ollama) which extracts process steps, then writes
the result to a Process Discovery Excel template compatible with
excel_to_bpmn.build_bpmn_from_excel().
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# ── System prompt ─────────────────────────────────────────────────────────────

try:
    from .auspost_conventions import AI_PROMPT_BLOCK as _AUSPOST_BLOCK
except ImportError:
    _AUSPOST_BLOCK = ""

TRANSCRIPTION_SYSTEM = (_AUSPOST_BLOCK + "\n\n" if _AUSPOST_BLOCK else "") + """You are a business process analyst. Extract a structured process \
definition from unstructured text (meeting notes, transcripts, or process documentation).

OUTPUT FORMAT: Respond with ONLY a single valid JSON object — no explanation, no markdown, \
no extra text. Use this exact structure:

{
  "process_name": "<short name>",
  "trigger_event": "<what starts the process>",
  "successful_outcome": "<what success looks like>",
  "unsuccessful_outcome": "<what failure looks like, or empty string>",
  "steps": [
    {
      "activity": "<Verb Object, max 5 words>",
      "description": "<max 15 words>",
      "participant": "<role, max 4 words>",
      "it_systems": "<system name or empty>",
      "input_document": "<doc name or empty>",
      "output_document": "<doc name or empty>",
      "templates": "<template name or empty>",
      "dependency": "<condition or empty>",
      "frequency": "<per request/daily/etc or empty>",
      "pain_points": "<issue or empty>"
    }
  ]
}

RULES:
1. Keep ALL string values SHORT — descriptions max 15 words, activity max 5 words
2. Activity names must be verb-object imperative: Validate form, Send notification
3. If participant not stated, infer from context or use Unknown
4. Merge very similar consecutive actions into one step
5. Note failures/conditions in the dependency field
6. Output ONLY the JSON object — nothing else"""


# ── File reader ───────────────────────────────────────────────────────────────

def read_transcription(file_path: str) -> str:
    """Extract text content from .txt, .docx, or .pdf file."""
    path = Path(file_path)
    ext  = path.suffix.lower()

    if ext == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")

    elif ext == ".docx":
        try:
            import docx  # python-docx
        except ImportError:
            raise ValueError(
                "python-docx is required to read Word documents.\n"
                "Install it with:  pip install python-docx"
            )
        try:
            doc   = docx.Document(str(path))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            # Also extract table cells
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        if cell.text.strip():
                            parts.append(cell.text.strip())
            return "\n".join(parts)
        except Exception as exc:
            raise ValueError(f"Could not read Word document: {exc}") from exc

    elif ext == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            raise ValueError(
                "pdfplumber is required to read PDF files.\n"
                "Install it with:  pip install pdfplumber"
            )
        try:
            pages = []
            with pdfplumber.open(str(path)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
            if not pages:
                raise ValueError("No readable text found in the PDF. The file may be image-only.")
            return "\n".join(pages)
        except ValueError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "encrypt" in msg or "password" in msg:
                raise ValueError(
                    "This PDF is password-protected. Remove the password and try again."
                ) from exc
            raise ValueError(f"Could not read PDF: {exc}") from exc

    else:
        raise ValueError(f"Unsupported file type '{ext}'. Please upload a .txt, .docx, or .pdf file.")


# ── JSON parser ───────────────────────────────────────────────────────────────

def _recover_truncated_json(fragment: str) -> dict | None:
    """Attempt to salvage a truncated JSON response by keeping complete steps only."""
    # Find everything up to the last complete step object: look for the last "},\n    {" or "}]"
    # Strategy: find the last '}' that ends a step, close off the JSON, and parse.
    steps_start = fragment.find('"steps"')
    if steps_start == -1:
        return None

    # Extract the metadata fields from the top of the object
    meta: dict = {}
    for key in ("process_name", "trigger_event", "successful_outcome", "unsuccessful_outcome"):
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)"', fragment)
        meta[key] = m.group(1) if m else ""

    # Find all complete step objects by scanning for balanced braces within the steps array
    arr_start = fragment.find("[", steps_start)
    if arr_start == -1:
        meta["steps"] = []
        return meta

    steps: list[dict] = []
    depth = 0
    obj_start = -1
    i = arr_start + 1
    while i < len(fragment):
        ch = fragment[i]
        if ch == "{" and depth == 0:
            obj_start = i
            depth = 1
        elif ch == "{":
            depth += 1
        elif ch == "}" and depth == 1:
            # Complete step object found
            try:
                step = json.loads(fragment[obj_start:i + 1])
                steps.append(step)
            except json.JSONDecodeError:
                pass
            depth = 0
            obj_start = -1
        elif ch == "}" and depth > 1:
            depth -= 1
        i += 1

    if not steps:
        return None

    meta["steps"] = steps
    return meta


def _parse_ai_json(raw: str) -> dict:
    """Parse JSON from AI response, stripping any accidental fences."""
    text = raw.strip()

    # Strip markdown fences
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif text.startswith("```"):
        text = text.split("```", 1)[1].split("```", 1)[0].strip()

    # Find outermost { ... }
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(
            "AI did not return a valid JSON object.\n"
            "Try again — the model may have added extra commentary."
        )

    fragment = text[start:end]
    try:
        data = json.loads(fragment)
    except json.JSONDecodeError:
        # Response was truncated mid-stream — try to recover complete steps.
        # Find the last fully-closed step object before the truncation point.
        recovered = _recover_truncated_json(fragment)
        if recovered is None:
            raise ValueError(
                "AI response was cut off before producing valid JSON.\n"
                "The document may be too long. Try splitting it into smaller sections."
            )
        data = recovered

    # Ensure required keys exist
    for key in ("process_name", "trigger_event", "successful_outcome",
                "unsuccessful_outcome"):
        if key not in data:
            data[key] = ""
    if "steps" not in data or not isinstance(data["steps"], list):
        data["steps"] = []

    # Normalise each step
    step_keys = ("activity", "description", "participant", "it_systems",
                 "input_document", "output_document", "templates",
                 "dependency", "frequency", "pain_points")
    for step in data["steps"]:
        for k in step_keys:
            if k not in step:
                step[k] = ""

    return data


# ── Excel writer ──────────────────────────────────────────────────────────────

def save_excel_from_ai_response(ai_output: dict, output_path: str) -> None:
    """Write a Process Discovery Excel template from AI-extracted data.

    Output format matches what parse_process_discovery_excel() in
    excel_to_bpmn.py expects:
      Rows 1-4  — metadata (Col A = label, Col B = value)
      Row  5    — blank
      Row  6    — column headers (10 columns A-J)
      Row  7+   — one row per process step
    """
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Process Discovery"

    # ── Metadata rows 1-4 ─────────────────────────────────────────────────
    meta = [
        ("Process Name",       ai_output.get("process_name",        "")),
        ("Trigger Event",       ai_output.get("trigger_event",       "")),
        ("Successful Outcome",  ai_output.get("successful_outcome",  "")),
        ("Unsuccessful Outcome",ai_output.get("unsuccessful_outcome","")),
    ]
    label_font = Font(bold=True)
    for row_i, (label, value) in enumerate(meta, start=1):
        ws.cell(row=row_i, column=1, value=label).font = label_font
        ws.cell(row=row_i, column=2, value=value)

    # Row 5 intentionally blank

    # ── Header row 6 ──────────────────────────────────────────────────────
    HEADERS = [
        "Subprocess / process\nActivity (what)",
        "Brief description (how)",
        "Participant (who)",
        "IT systems",
        "Input document name",
        "Output document name",
        "Templates",
        "Dependency",
        "Frequency",
        "Any pain points? (If Yes/no/VA-NVA)",
        # v1.2.5 - hidden 11th column carries the BPMN element ID for the
        # uplift round-trip. Empty for the transcription flow (new BPMN);
        # populated when the Excel was generated from an existing BPMN so the
        # patcher in bpmn_patcher.py can match each row back to the source.
        "BPMN ID (do not edit)",
    ]
    hdr_fill = PatternFill("solid", fgColor="1A2942")
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_align = Alignment(wrap_text=True, horizontal="center", vertical="center")
    for col_i, hdr in enumerate(HEADERS, start=1):
        cell = ws.cell(row=6, column=col_i, value=hdr)
        cell.fill      = hdr_fill
        cell.font      = hdr_font
        cell.alignment = hdr_align
    ws.row_dimensions[6].height = 32

    # ── Data rows from row 7 ──────────────────────────────────────────────
    steps = ai_output.get("steps", [])
    if not steps:
        # Guarantee at least one placeholder so excel_to_bpmn doesn't fail
        steps = [{
            "activity":        "Review process steps",
            "description":     "No steps were extracted — please fill in manually.",
            "participant":     "Unknown",
            "it_systems":      "",
            "input_document":  "",
            "output_document": "",
            "templates":       "",
            "dependency":      "",
            "frequency":       "",
            "pain_points":     "Steps unclear from notes - please review",
        }]

    data_align = Alignment(wrap_text=True, vertical="top")
    id_font    = Font(color="888888", italic=True)  # de-emphasise the ID col
    for row_i, step in enumerate(steps, start=7):
        vals = [
            step.get("activity",        ""),
            step.get("description",     ""),
            step.get("participant",     ""),
            step.get("it_systems",      ""),
            step.get("input_document",  ""),
            step.get("output_document", ""),
            step.get("templates",       ""),
            step.get("dependency",      ""),
            step.get("frequency",       ""),
            step.get("pain_points",     ""),
            step.get("bpmn_id",         ""),   # v1.2.5 - round-trip key
        ]
        for col_i, val in enumerate(vals, start=1):
            cell           = ws.cell(row=row_i, column=col_i, value=val)
            cell.alignment = data_align
            if col_i == 11:
                cell.font = id_font

    # ── Column widths ─────────────────────────────────────────────────────
    widths = [40, 40, 20, 25, 25, 25, 20, 30, 15, 35, 28]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w

    # Freeze panes below header
    ws.freeze_panes = "A7"

    wb.save(output_path)


# ── Provider functions ────────────────────────────────────────────────────────

def _build_user_message(text: str, process_name: str) -> str:
    hint = f"Process name (from user): {process_name}\n\n" if process_name else ""
    return f"{hint}TRANSCRIPT / NOTES:\n{text}"


def _handle_error(exc: Exception, provider: str,
                  on_error: Optional[Callable[[str], None]]) -> None:
    msg = str(exc)
    low = msg.lower()
    if provider == "Ollama" and ("connection refused" in low or
                                  "10061" in msg or "urlopen" in low):
        msg = ("Ollama is not running.\n\n"
               "Start it with:  ollama serve\n"
               "Then ensure a model is pulled:  ollama pull llama3.2")
    elif "api_key" in low or "api key" in low or "invalid" in low:
        msg = f"Invalid {provider} API key. Please check Settings."
    elif "quota" in low or "rate" in low or "limit" in low:
        msg = f"{provider} rate limit reached. Please wait and try again."
    elif "network" in low or "connect" in low:
        msg = f"Network error connecting to {provider}. Check your internet."
    else:
        msg = f"{provider} analysis failed: {exc}"
    if on_error:
        on_error(msg)


def _call_anthropic(
    text: str, process_name: str, api_key: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
) -> None:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        user_msg = _build_user_message(text, process_name)
        collected: list[str] = []
        with client.messages.stream(
            model="claude-sonnet-4-6",
            # Bumped from 8192 — was truncating mid-step on real-world
            # transcripts, producing malformed JSON the parser couldn't recover.
            max_tokens=32000,
            system=TRANSCRIPTION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        ) as stream:
            for chunk in stream.text_stream:
                collected.append(chunk)
                if on_chunk:
                    on_chunk(chunk)
        if on_complete:
            on_complete("".join(collected))
    except Exception as exc:
        _handle_error(exc, "Anthropic", on_error)


def _call_gemini(
    text: str, process_name: str, api_key: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
) -> None:
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.0-flash",
            system_instruction=TRANSCRIPTION_SYSTEM,
        )
        user_msg  = _build_user_message(text, process_name)
        collected: list[str] = []
        response = model.generate_content(
            user_msg,
            generation_config=genai.GenerationConfig(max_output_tokens=8192),
            stream=True,
        )
        for chunk in response:
            chunk_text = getattr(chunk, "text", None)
            if chunk_text:
                collected.append(chunk_text)
                if on_chunk:
                    on_chunk(chunk_text)
        if on_complete:
            on_complete("".join(collected))
    except Exception as exc:
        _handle_error(exc, "Gemini", on_error)


def _call_ollama(
    text: str, process_name: str, model: str,
    on_chunk: Optional[Callable[[str], None]],
    on_complete: Optional[Callable[[str], None]],
    on_error: Optional[Callable[[str], None]],
) -> None:
    user_msg = _build_user_message(text, process_name)
    payload  = {
        "model": model or "llama3.2",
        "messages": [
            {"role": "system", "content": TRANSCRIPTION_SYSTEM},
            {"role": "user",   "content": user_msg},
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
                    txt  = obj.get("message", {}).get("content", "")
                    if txt:
                        collected.append(txt)
                        if on_chunk:
                            on_chunk(txt)
                    if obj.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue
        if on_complete:
            on_complete("".join(collected))
    except Exception as exc:
        _handle_error(exc, "Ollama", on_error)


# ── Public entry point ────────────────────────────────────────────────────────

def build_excel_from_transcription(
    text: str,
    process_name: str,
    provider: str,
    api_key: str,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_complete: Optional[Callable[[str], None]] = None,
    on_error: Optional[Callable[[str], None]] = None,
) -> None:
    """Analyse transcript text with AI and stream the raw JSON response.

    The caller (main.py) is responsible for calling _parse_ai_json() and
    save_excel_from_ai_response() in the on_complete branch.
    """
    if provider == "local":
        if on_error:
            on_error(
                "The Built-in provider cannot analyse transcriptions.\n\n"
                "Please select Gemini, Anthropic, or Ollama in ⚙ Settings."
            )
        return

    if provider == "anthropic":
        _call_anthropic(text, process_name, api_key, on_chunk, on_complete, on_error)
    elif provider == "gemini":
        _call_gemini(text, process_name, api_key, on_chunk, on_complete, on_error)
    elif provider == "ollama":
        model = api_key or os.environ.get("OLLAMA_MODEL", "llama3.2")
        _call_ollama(text, process_name, model, on_chunk, on_complete, on_error)
    else:
        if on_error:
            on_error(f"Unknown provider '{provider}'.")
