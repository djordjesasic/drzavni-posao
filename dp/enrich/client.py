"""Minimal client for OpenAI-compatible chat APIs (llama-server, z.ai, ...)."""

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Reply:
    content: str
    tokens_in: int | None  # all input tokens, cached ones included
    tokens_out: int | None
    tokens_cached: int | None = None  # input tokens read from the prompt cache


def chat(
    cfg: dict[str, Any],
    messages: list[dict[str, str]],
    timeout_s: float = 900,
    schema: dict[str, Any] | None = None,
) -> Reply:
    """One chat completion. cfg is a [enrich.models.<name>] table from config.toml.
    With `json_schema = true` in cfg, `schema` is sent as a strict response format."""
    headers = {}
    if key_env := cfg.get("api_key_env"):
        key = os.environ.get(key_env)
        if not key:
            raise RuntimeError(f"{key_env} is not set (.env on the mini PC)")
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": int(cfg.get("max_tokens", 4000)),
        "temperature": float(cfg.get("temperature", 0.2)),
        "response_format": {"type": "json_object"},
        **cfg.get("extra", {}),
    }
    if schema is not None and cfg.get("json_schema"):
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "episode", "strict": True, "schema": schema},
        }
    r = httpx.post(
        cfg["base_url"].rstrip("/") + "/chat/completions",
        headers=headers,
        json=body,
        timeout=timeout_s,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
    d = r.json()
    usage = d.get("usage") or {}
    return Reply(
        content=d["choices"][0]["message"].get("content") or "",
        tokens_in=usage.get("prompt_tokens"),
        tokens_out=usage.get("completion_tokens"),
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_json(text: str) -> dict[str, Any]:
    """The JSON object in a model reply (tolerates a ```json fence or text around it)."""
    text = _FENCE.sub("", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("reply is not a JSON object")
    return data
