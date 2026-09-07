"""Bedrock provider for the ported AI passes.

The desktop app talks to Anthropic, Gemini or Ollama with a user-supplied API
key. In AWS there is no key to supply — the Lambda's IAM role authorises
Bedrock — so each ai_* module gains a `bedrock` branch that lands here.

boto3 ships with the Lambda runtime, so this needs nothing vendored.

The `api_key` argument the ported dispatchers pass through is ignored; it is
kept in the signatures so those files stay diffable against upstream.
"""
from __future__ import annotations

import os
import threading

_client = None
_lock = threading.Lock()

DEFAULT_MODEL = os.environ.get(
    "MODEL_ID", "us.anthropic.claude-opus-4-5-20251101-v1:0"
)


def _get_client():
    """Lazily build the Bedrock client and reuse it across warm invocations."""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import boto3
                _client = boto3.client("bedrock-runtime")
    return _client


def call_bedrock(
    system_prompt: str,
    user_msg: str,
    max_tokens: int = 8000,
    model_id: str | None = None,
) -> str:
    """Single-turn Converse call. Returns the concatenated text blocks.

    Mirrors what `_call_anthropic` returns in the ported modules, so their
    downstream JSON parsing is unchanged.
    """
    resp = _get_client().converse(
        modelId=model_id or DEFAULT_MODEL,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_msg}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(b.get("text", "") for b in blocks)


def call_bedrock_with_images(
    system_prompt: str,
    user_msg: str,
    images: list | None = None,
    max_tokens: int = 8000,
    model_id: str | None = None,
) -> str:
    """Same call, with document figures attached.

    `images` is a list of (bytes, format) where format is one Converse accepts:
    png, jpeg, gif or webp. Images go first in the content list — a diagram is
    context for the instruction that follows it, not an afterthought — and an
    empty list makes this identical to call_bedrock.
    """
    content = []
    for data, fmt in (images or []):
        content.append({"image": {"format": fmt, "source": {"bytes": data}}})
    content.append({"text": user_msg})

    resp = _get_client().converse(
        modelId=model_id or DEFAULT_MODEL,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": content}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    return "".join(b.get("text", "") for b in blocks)
