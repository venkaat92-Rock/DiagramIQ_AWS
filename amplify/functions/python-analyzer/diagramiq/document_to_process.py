"""SOP / notes document → a Process Discovery definition.

The transcription prompt (transcription_to_excel.TRANSCRIPTION_SYSTEM) was
written for prose: someone talking through how a process runs. An SOP is a
different kind of source and it fails in different ways.

  * The procedure is a numbered clause list, and clause numbering carries the
    order — "5.3 before 5.4" is the sequence, not an accident of layout.
  * Roles are in a responsibilities table, not in the sentence describing the
    step. "Approve request" and "Finance Manager" are in different sections.
  * Decisions hide in thresholds and conditionals: "requests above £5,000",
    "if the supplier is not on the approved list". Each is a gateway.
  * Reference sections — purpose, scope, revision history, related documents,
    definitions — are not steps, and a prompt that does not say so produces
    "Review revision history" as an activity.
  * The flow is often a figure pasted into the document, which says more about
    the branching than the prose around it does.

So this module gets its own prompt, and when the document carries figures it
sends them with the text, so the diagram and the words are read together.
"""
from __future__ import annotations

MAX_TEXT_CHARS = 120_000

try:
    from .auspost_conventions import AI_PROMPT_BLOCK as _CONVENTIONS
except ImportError:
    _CONVENTIONS = ""

DOCUMENT_SYSTEM = (_CONVENTIONS + "\n\n" if _CONVENTIONS else "") + """You are a business process analyst. You are given a source document \
describing how a business process runs — a standard operating procedure, a work \
instruction, meeting notes, or a transcript — and you extract the process it \
describes as structured data.

The text has been converted from the original document. Read these markers:
  * `#`, `##`, `###` are the document's own headings.
  * `- ` lines are list or numbered-clause items, in document order.
  * `| a | b |` blocks are tables, first row is the header.
  * `[Figure N]` marks where a diagram sat in the document. Any images supplied \
with this message are those figures, in order.

HOW TO READ AN SOP
1. The procedure section is the process. Purpose, scope, definitions, \
references, revision history, approvals and document-control sections describe \
the SOP itself, not the work — never turn them into steps.
2. Clause order is step order. Follow the numbering, not the order phrases \
happen to appear on the page.
3. A responsibilities or RACI table tells you the participant for each step. \
Join it to the step by the activity or role named, and use those role names \
rather than inventing your own.
4. A step table (columns like Step / Activity / Responsible / System / Input) \
is the procedure in tabular form. Each row is one step; map its columns onto \
the fields below.
5. Conditions and thresholds are decision points. "Requests above 5,000", "if \
the invoice does not match", "for new suppliers only" each belong in the \
dependency field of the step they govern, in the document's own words \
including the number and the unit. Do not round or generalise a threshold.
6. Systems are usually named in parentheses or in a system column — SAP, \
Coupa, ServiceNow, SharePoint. Put them in it_systems, not in the activity name.
7. Forms, templates and reports named in the text are input_document, \
output_document or templates as the sentence indicates.
8. If a figure shows the flow, use it to settle sequence and branching that the \
text leaves ambiguous. Where figure and text disagree, follow the text and note \
the disagreement in that step's pain_points.
9. Extract only what the document says. If a step's participant, system or \
criteria is genuinely absent, leave the field empty — an empty field is a \
question we will put to the business, and a plausible guess is a wrong answer \
nobody will catch.

OUTPUT FORMAT: Respond with ONLY a single valid JSON object — no explanation, \
no markdown, no extra text. Use this exact structure:

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
      "dependency": "<condition or threshold or empty>",
      "frequency": "<per request/daily/etc or empty>",
      "pain_points": "<issue or empty>"
    }
  ]
}

RULES:
1. Keep ALL string values SHORT — descriptions max 15 words, activity max 5 words
2. Activity names must be verb-object imperative: Validate form, Send notification
3. Merge steps only when they are the same action described twice, never when \
they are two actions by different roles
4. Output ONLY the JSON object — nothing else"""


def _user_message(text: str, figures: list, filename: str) -> str:
    body = text if len(text) <= MAX_TEXT_CHARS else (
        text[:MAX_TEXT_CHARS] + f"\n\n[... truncated, {len(text) - MAX_TEXT_CHARS} "
        "more characters in the source document]"
    )
    lead = [f"Source document: {filename}"]
    if figures:
        names = ", ".join(f"{f.marker} ({f.name})" for f in figures)
        lead.append(
            f"{len(figures)} figure(s) from this document are attached in order: {names}"
        )
    lead.append("Extract the process it describes. Return ONLY the JSON object.")
    return "\n".join(lead) + "\n\n--- DOCUMENT ---\n" + body


def extract_process(doc, filename: str = "document", model_id: str | None = None) -> str:
    """Run the document pass. Returns the model's raw text for JSON parsing.

    `doc` is a doc_text.Document. Figures ride along with the text when the
    document has any, so the same call reads the words and the diagram.
    """
    from .bedrock_provider import call_bedrock_with_images

    figures = list(getattr(doc, "figures", []) or [])
    images = [(f.data, f.fmt) for f in figures]
    return call_bedrock_with_images(
        DOCUMENT_SYSTEM,
        _user_message(doc.text, figures, filename),
        images=images,
        max_tokens=8000,
        model_id=model_id,
    )
