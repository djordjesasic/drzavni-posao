"""Describe one episode with one configured model and store the result."""

import json
import logging
import sqlite3
import time
from typing import Any

from dp import pipeline
from dp.config import Settings
from dp.db import active_video_id
from dp.enrich import claude, client, prompt
from dp.enrich.store import valid_enrichment
from dp.schedule import wait_if_paused

log = logging.getLogger(__name__)


def _shape_problems(data: dict[str, Any]) -> list[str]:
    """Fields whose type differs from the schema (a string where a list is expected,
    string items among objects, ...). Indexing tolerates them; this only makes them visible."""
    bad = []
    for key, spec in prompt.SCHEMA.items():
        v = data.get(key)
        if v is None:
            continue
        if isinstance(spec, list):
            if not isinstance(v, list) or (
                isinstance(spec[0], dict) and not all(isinstance(x, dict) for x in v)
            ):
                bad.append(key)
        elif isinstance(spec, dict):
            if not isinstance(v, dict):
                bad.append(key)
        elif not isinstance(v, str):
            bad.append(key)
    stories = data.get("stories")
    if isinstance(stories, list) and any(
        isinstance(x, dict) and isinstance(x.get("about"), str) for x in stories
    ):
        bad.append("stories.about")
    return bad


def check(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    """Light validation: missing top-level keys, character ids not in the registry and
    fields of the wrong shape."""
    known = {r[0] for r in conn.execute("SELECT id FROM characters")}
    missing = [k for k in prompt.SCHEMA if k not in data]
    chars = data.get("characters")
    bad_ids = sorted(
        {
            c["id"]
            for c in (chars if isinstance(chars, list) else [])
            if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"] not in known
        }
    )
    return {"missing_keys": missing, "unknown_ids": bad_ids, "bad_shape": _shape_problems(data)}


def enrich_episode(
    s: Settings, conn: sqlite3.Connection, episode_id: int, model: str, force: bool = False
) -> dict[str, Any] | None:
    """Run the model on the episode; None if a result for this prompt version exists."""
    cfg = s.enrich_models.get(model)
    if cfg is None:
        raise ValueError(f"unknown model {model!r}; configured: {sorted(s.enrich_models)}")
    key = (episode_id, model, prompt.PROMPT_VERSION)
    # Not in the catalog (a failed row would violate the foreign key), retired or without a
    # video: no result could ever be valid, so do not pay for the call.
    video_id = active_video_id(conn, episode_id)
    if video_id is None:
        error = f"episode {episode_id} not in the catalog (missing, retired or without video)"
        log.warning("%s", error)
        return {"model": model, "error": error}
    if valid_enrichment(conn, episode_id, model, video_id) and not force:
        return None
    # The result is stored with this video_id, so the segments it reads must be this
    # video's transcript (not a leftover of the video the episode pointed to before),
    # with the speaker tags the prompt is built from.
    for step in ("transcribe", "speakers"):
        if not pipeline.is_done(conn, episode_id, step, video_id):
            error = f"episode {episode_id}: step {step!r} is not done for the current video"
            log.warning("%s", error)
            return {"model": model, "error": error}
    t0 = time.monotonic()
    data: dict[str, Any] | None = None
    reply: client.Reply | None = None
    error = None
    try:
        # Inside the try: an episode without a transcript raises here and must not abort
        # the whole batch.
        bg_path = s.seed_characters.parent / "background.md"
        background = bg_path.read_text(encoding="utf-8") if bg_path.exists() else ""
        parts = prompt.build_parts(conn, episode_id, background)
        if cfg.get("local"):
            wait_if_paused(s.pause_start, s.pause_end)
        t0 = time.monotonic()
        if cfg.get("api") == "anthropic":
            reply = claude.chat(cfg, *parts)
        else:
            reply = client.chat(cfg, prompt.to_messages(parts), schema=prompt.JSON_SCHEMA)
        data = client.parse_json(reply.content)
    except Exception as e:  # stored, the run goes on with the next episode
        error = f"{type(e).__name__}: {e}"[:2000]
    elapsed = round(time.monotonic() - t0, 1)
    with conn:
        if (
            error
            and conn.execute(
                "SELECT 1 FROM enrichment WHERE episode_id=? AND model=? AND prompt_version=? "
                "AND status IN ('done', 'stale')",
                key,
            ).fetchone()
        ):
            # A failure never overwrites a paid result, not even a stale one or one of an
            # older video.
            log.warning("episode %s %s: keeping the existing result", episode_id, model)
        else:
            conn.execute(
                """INSERT OR REPLACE INTO enrichment (episode_id, model, prompt_version, status,
                     data, raw, error, tokens_in, tokens_out, elapsed_s, video_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    *key,
                    "failed" if error else "done",
                    json.dumps(data, ensure_ascii=False) if data is not None else None,
                    reply.content if reply else None,
                    error,
                    reply.tokens_in if reply else None,
                    reply.tokens_out if reply else None,
                    elapsed,
                    video_id,
                ),
            )
            if not error and model == s.index_enrich_model:
                # The description feeds the index; without this the step stays 'done' and
                # the new text never reaches it.
                pipeline.invalidate(conn, episode_id, ["index"])
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
