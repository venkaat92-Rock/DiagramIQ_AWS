"""The two upload buttons are two passes, and this proves which is which.

Bedrock is stubbed, so what these assertions check is the thing that actually
regressed when a transcript and an SOP shared one route: which prompt each one
sends, with which token ceiling, and with which model.

Run:  python3 test/route_split.py     (from the repository root)
"""
import base64
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent / 'amplify/functions/python-analyzer'
sys.path[:0] = [str(ROOT), str(ROOT / 'vendor')]

from diagramiq import bedrock_provider                              # noqa: E402
from diagramiq.document_to_process import DOCUMENT_SYSTEM           # noqa: E402
from diagramiq.transcription_to_excel import TRANSCRIPTION_SYSTEM   # noqa: E402

DISCOVERY = json.dumps({
    "process_name": "P", "trigger_event": "t", "successful_outcome": "s",
    "unsuccessful_outcome": "", "steps": [
        {"activity": "Raise request", "participant": "Requester"},
        {"activity": "Approve request", "participant": "Finance"},
    ]})

sent = []
fail_next = []


def _record(system, user, images=None, max_tokens=8000, model_id=None):
    call = {"system": system, "user": user, "max_tokens": max_tokens, "model_id": model_id}
    if images is not None:
        call["images"] = images
    sent.append(call)
    if fail_next:
        fail_next.pop()
        raise RuntimeError("ThrottlingException: Too many requests")
    return DISCOVERY


bedrock_provider.call_bedrock = lambda s, u, max_tokens=8000, model_id=None: _record(
    s, u, None, max_tokens, model_id)
bedrock_provider.call_bedrock_with_images = lambda s, u, images=None, max_tokens=8000, \
    model_id=None: _record(s, u, images or [], max_tokens, model_id)

import index                                                        # noqa: E402

fails = []


def ok(name, cond, detail=''):
    print(('PASS  ' if cond else 'FAIL  ') + name + (f'  — {detail}' if detail and not cond else ''))
    if not cond:
        fails.append(name)


def call(path, **extra):
    body = dict({"processName": "Procurement", "modelId": MODEL}, **extra)
    res = index.handler({"rawPath": path, "body": json.dumps(body)}, None)
    return res, json.loads(res["body"])


MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
TRANSCRIPT = base64.b64encode(
    b"So the requester raises it, and then finance approve it, usually same day.").decode()

# ── /notes: the transcription pass ───────────────────────────────────────────
sent.clear()
res, body = call("/notes", fileBase64=TRANSCRIPT, filename="call.txt")
ok('/notes answers 200', res["statusCode"] == 200, res["body"][:160])
ok('/notes sends the transcription prompt, not the SOP one',
   bool(sent) and sent[0]["system"] == TRANSCRIPTION_SYSTEM != DOCUMENT_SYSTEM)
ok('/notes keeps the 32000-token ceiling', bool(sent) and sent[0]["max_tokens"] == 32000,
   sent[0]["max_tokens"] if sent else 'no call')
ok('/notes passes the model chosen in the UI', bool(sent) and sent[0]["model_id"] == MODEL,
   sent[0]["model_id"] if sent else 'no call')
ok('/notes attaches no figures — a transcript has none', bool(sent) and "images" not in sent[0])
ok('/notes returns the grid and the workbook',
   bool(body.get("discovery", {}).get("steps")) and bool(body.get("fileBase64")))
ok('/notes labels which pass read it', body.get("source", {}).get("mode") == "transcript",
   body.get("source"))

# ── /sop: the document pass ──────────────────────────────────────────────────
SOP = pathlib.Path('samples/SOP-PR-014 Purchase Requisition to Purchase Order.docx')
sop_b64 = base64.b64encode(SOP.read_bytes()).decode()

sent.clear()
res, body = call("/sop", fileBase64=sop_b64, filename=SOP.name)
ok('/sop answers 200', res["statusCode"] == 200, res["body"][:160])
ok('/sop sends the document prompt', bool(sent) and sent[0]["system"] == DOCUMENT_SYSTEM)
ok('/sop passes the model chosen in the UI', bool(sent) and sent[0]["model_id"] == MODEL)
ok('/sop reads the tables and figures out of the Word file',
   body.get("source", {}).get("tables", 0) >= 4 and body.get("source", {}).get("figures", 0) >= 1,
   body.get("source"))

# The table fallback still covers the document route, and only that route.
sent.clear()
fail_next.append(1)
res, body = call("/sop", fileBase64=sop_b64, filename=SOP.name)
ok('/sop falls back to the procedure table when the AI fails',
   res["statusCode"] == 200 and body.get("source", {}).get("mode") == "table",
   body.get("source") or body.get("error"))
ok('and says the reading is the lesser one',
   'failed' in (body.get("source", {}).get("degraded") or ''),
   body.get("source", {}).get("degraded"))

# ── CORS belongs to the endpoint ─────────────────────────────────────────────
ok('the handler returns no CORS headers of its own',
   not any(k.lower().startswith('access-control') for k in res["headers"]),
   list(res["headers"]))

print('\n' + ('ALL PASS' if not fails else f'{len(fails)} FAILURE(S)'))
sys.exit(1 if fails else 0)
