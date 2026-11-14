"""Describe one episode with one configured model and store the result."""

import json
import logging
import sqlite3
import time
from typing import Any

from dp.config import Settings
from dp.enrich import claude, client, prompt
from dp.schedule import wait_if_paused

log = logging.getLogger(__name__)


def check(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """Light validation: missing top-level keys and character ids not in the registry."""
    known = {r[0] for r in conn.execute("SELECT id FROM characters")}
    missing = [k for k in prompt.SCHEMA if k not in data]
    bad_ids = sorted(
        {
            c["id"]
            for c in data.get("characters") or []
            if isinstance(c, dict) and c.get("id") and c["id"] not in known
        }
    )
    return {"missing_keys": missing, "unknown_ids": bad_ids}


def enrich_episode(
    s: Settings, conn: sqlite3.Connection, episode_id: int, model: str, force: bool = False
) -> dict[str, Any] | None:
    """Run the model on the episode; None if a result for this prompt version exists."""
    cfg = s.enrich_models.get(model)
    if cfg is None:
        raise ValueError(f"unknown model {model!r}; configured: {sorted(s.enrich_models)}")
    key = (episode_id, model, prompt.PROMPT_VERSION)
    done = conn.execute(
        "SELECT 1 FROM enrichment WHERE episode_id=? AND model=? AND prompt_version=? "
        "AND status='done'",
        key,
    ).fetchone()
    if done and not force:
        return None
    bg_path = s.seed_characters.parent / "background.md"
    background = bg_path.read_text(encoding="utf-8") if bg_path.exists() else ""
    parts = prompt.build_parts(conn, episode_id, background)
    if cfg.get("local"):
        wait_if_paused(s.pause_start, s.pause_end)
    t0 = time.monotonic()
    data: dict[str, Any] | None = None
    reply: client.Reply | None = None
    error = None
    try:
        if cfg.get("api") == "anthropic":
            reply = claude.chat(cfg, *parts)
        else:
            reply = client.chat(cfg, prompt.to_messages(parts), schema=prompt.JSON_SCHEMA)
        data = client.parse_json(reply.content)
    except Exception as e:  # stored, the run goes on with the next episode
        error = f"{type(e).__name__}: {e}"[:2000]
    elapsed = round(time.monotonic() - t0, 1)
    with conn:
        conn.execute(
            """INSERT OR REPLACE INTO enrichment (episode_id, model, prompt_version, status,
                 data, raw, error, tokens_in, tokens_out, elapsed_s)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                *key,
                "failed" if error else "done",
                json.dumps(data, ensure_ascii=False) if data is not None else None,
                reply.content if reply else None,
                error,
                reply.tokens_in if reply else None,
                reply.tokens_out if reply else None,
                elapsed,
            ),
        )
    info: dict[str, Any] = {
        "model": model,
        "elapsed_s": elapsed,
        "tokens_in": reply.tokens_in if reply else None,
        "tokens_out": reply.tokens_out if reply else None,
    }
    if reply and reply.tokens_cached is not None:
        info["tokens_cached"] = reply.tokens_cached
    if error:
        info["error"] = error
        log.warning("episode %s %s failed: %s", episode_id, model, error)
    else:
        assert data is not None
        info |= check(conn, data)
    return info
