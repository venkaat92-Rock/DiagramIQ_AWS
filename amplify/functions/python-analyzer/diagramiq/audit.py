"""What each person did, kept where it can be read back.

One row per request: who, which route, when, how long, and whether it worked —
plus a small, deliberately shallow summary of the input. Never the document
itself. A process map can be commercially sensitive and the point of this table
is accountability, not a second copy of the customer's material.

Writing the log must never break the work it describes, so every failure here
is swallowed and logged. A missing AUDIT_TABLE makes the whole module a no-op,
which is what keeps local runs and the test suite from needing DynamoDB.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone

TABLE = os.environ.get("AUDIT_TABLE", "")
RETENTION_DAYS = int(os.environ.get("AUDIT_RETENTION_DAYS", "400"))

# Keys worth keeping from a request body. Everything else — file contents,
# document text, the model itself — is left behind on purpose.
SUMMARY_KEYS = ("filename", "processName", "mode", "modelId", "kind", "limit")
MAX_VALUE = 160

_table = None


def _handle():
    global _table
    if _table is None and TABLE:
        import boto3
        _table = boto3.resource("dynamodb").Table(TABLE)
    return _table


def summarise(body) -> dict:
    """The parts of a request that are safe to keep, plus its size."""
    out: dict = {}
    if not isinstance(body, dict):
        return out
    for key in SUMMARY_KEYS:
        value = body.get(key)
        if isinstance(value, (str, int, float, bool)) and str(value):
            out[key] = str(value)[:MAX_VALUE]
    for key in ("fileBase64", "xml", "text", "imageB64"):
        if body.get(key):
            out[f"{key}Bytes"] = len(str(body[key]))
    if isinstance(body.get("discovery"), dict):
        out["steps"] = len(body["discovery"].get("steps") or [])
    return out


def record(identity, route: str, status: int, started: float,
           detail: dict | None = None, error: str = "") -> None:
    table = _handle()
    if table is None:
        return
    now = datetime.now(timezone.utc)
    item = {
        "pk": f"USER#{getattr(identity, 'sub', 'anonymous') or 'anonymous'}",
        "sk": f"{now.isoformat()}#{uuid.uuid4().hex[:6]}",
        "day": now.date().isoformat(),
        "ts": now.isoformat(),
        "actor": getattr(identity, "label", "anonymous"),
        "actorSub": getattr(identity, "sub", "anonymous"),
        "action": route,
        "surface": "engine",
        "status": status,
        "ms": int((time.time() - started) * 1000),
        "detail": detail or {},
        "expiresAt": int(time.time()) + RETENTION_DAYS * 86400,
    }
    if error:
        item["error"] = error[:MAX_VALUE]
    try:
        table.put_item(Item=item)
    except Exception as err:                       # noqa: BLE001 - never fatal
        print(f"audit write failed for {route}: {type(err).__name__}: {err}")
