"""Claude (Anthropic SDK) backend for enrichment.

The registry and schema are the same for every episode, so they go in their own
cached block: after the first request they are read from cache at ~10% of the price.
The same request parameters serve one direct call (chat) and the Message Batches API
(dp.enrich.batch), so both produce the same descriptions.
"""

from typing import Any

import anthropic

from dp.enrich import prompt
from dp.enrich.client import Reply


def params(
    cfg: dict[str, Any],
    system: str,
    stable: str,
    episode: str,
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """messages.create parameters. cfg is a [enrich.models.<name>] table with
    api = "anthropic". `images`: extra content blocks (frames) after the transcript."""
    p: dict[str, Any] = {
        "model": cfg["model"],
        "max_tokens": int(cfg.get("max_tokens", 16000)),
        "system": system,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": episode},
                    *(images or []),
                ],
            }
        ],
    }
    # Structured output: the reply is always one JSON object of the description schema.
    p["output_config"] = {"format": {"type": "json_schema", "schema": prompt.JSON_SCHEMA}}
    if effort := cfg.get("effort"):
        p["output_config"]["effort"] = effort
    return p


def reply(msg: Any) -> Reply:
    """A finished Message as a Reply; a refusal or a cut-off reply raises."""
    if msg.stop_reason == "refusal":
        category = msg.stop_details.category if msg.stop_details else None
        raise RuntimeError(f"model refused (category {category})")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("reply cut off at max_tokens")
    text = "".join(b.text for b in msg.content if b.type == "text")
    u = msg.usage
    cache_read = u.cache_read_input_tokens or 0
    cache_write = u.cache_creation_input_tokens or 0
    return Reply(
        content=text,
        tokens_in=u.input_tokens + cache_read + cache_write,
        tokens_out=u.output_tokens,
        tokens_cached=cache_read,
    )


def chat(
    cfg: dict[str, Any],
    system: str,
    stable: str,
    episode: str,
    images: list[dict[str, Any]] | None = None,
) -> Reply:
    """One Messages API call; the key comes from ANTHROPIC_API_KEY (.env on the mini PC)."""
    return chat_params(cfg, params(cfg, system, stable, episode, images))


def chat_params(cfg: dict[str, Any], p: dict[str, Any]) -> Reply:
    """One Messages API call with ready parameters (see params)."""
    client = anthropic.Anthropic(timeout=float(cfg.get("timeout_s", 600)))
    return reply(client.messages.create(**p))
