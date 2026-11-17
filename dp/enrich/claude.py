"""Claude (Anthropic SDK) backend for enrichment.

The registry and schema are the same for every episode, so they go in their own
cached block: after the first request they are read from cache at ~10% of the price.
"""

from typing import Any

import anthropic

from dp.enrich.client import Reply


def chat(
    cfg: dict[str, Any],
    system: str,
    stable: str,
    episode: str,
    images: list[dict[str, Any]] | None = None,
) -> Reply:
    """One Messages API call. cfg is a [enrich.models.<name>] table with api = "anthropic";
    the key comes from ANTHROPIC_API_KEY (.env on the mini PC). `images`: extra content
    blocks (frames) after the transcript."""
    client = anthropic.Anthropic(timeout=float(cfg.get("timeout_s", 600)))
    kwargs: dict[str, Any] = {}
    if effort := cfg.get("effort"):
        kwargs["output_config"] = {"effort": effort}
    msg = client.messages.create(
        model=cfg["model"],
        max_tokens=int(cfg.get("max_tokens", 16000)),
        system=system,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": episode},
                    *(images or []),
                ],
            }
        ],
        **kwargs,
    )
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
