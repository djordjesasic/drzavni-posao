"""Describe one episode with one configured model and store the result."""

import json
import logging
import sqlite3
import time
from typing import Any

from dp import pipeline
from dp.config import Settings
from dp.db import active_video_id
from dp.enrich import claude, client, frames, prompt
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


def not_ready(conn: sqlite3.Connection, episode_id: int) -> tuple[str | None, str | None]:
    """(video_id, None) when the episode can be described, else (video_id or None, why).
    The result is stored with this video_id, so the segments it reads must be this video's
    transcript (not a leftover of the video the episode pointed to before), with the speaker
    tags the prompt is built from. Not in the catalog (a failed row would violate the
    foreign key), retired or without a video: no result could ever be valid."""
    video_id = active_video_id(conn, episode_id)
    if video_id is None:
        return None, f"episode {episode_id} not in the catalog (missing, retired or without video)"
    for step in ("transcribe", "speakers"):
        if not pipeline.is_done(conn, episode_id, step, video_id):
            why = f"episode {episode_id}: step {step!r} is not done for the current video"
            return video_id, why
    return video_id, None


def claude_request(
    s: Settings, conn: sqlite3.Connection, episode_id: int, cfg: dict[str, Any]
) -> dict[str, Any]:
    """messages.create parameters for an anthropic model, frames included when the model
    has frames_every_s. Raises when the episode has no transcript (or no video)."""
    system, stable, episode = parts(s, conn, episode_id)
    if not cfg.get("frames_every_s"):
        return claude.params(cfg, system, stable, episode)
    path = frames.video_path(s.data_dir, episode_id)
    if not path.exists():
        raise FileNotFoundError(f"no video for episode {episode_id}: {path}")
    shots = frames.extract(path, float(cfg["frames_every_s"]))
    return claude.params(
        cfg, system + prompt.FRAMES_SYSTEM, stable, episode, frames.to_blocks(shots)
    )


def parts(s: Settings, conn: sqlite3.Connection, episode_id: int) -> tuple[str, str, str]:
    bg_path = s.seed_dir / "background.md"
    background = bg_path.read_text(encoding="utf-8") if bg_path.exists() else ""
    return prompt.build_parts(conn, episode_id, background, prompt.load_system(s.seed_dir))


def store(
    s: Settings,
    conn: sqlite3.Connection,
    episode_id: int,
    model: str,
    video_id: str,
    reply: client.Reply | None,
    data: dict[str, Any] | None,
    error: str | None,
    elapsed: float | None,
    status: str = "done",
) -> None:
    """Save one result. A failure never overwrites a paid result, not even a stale one or
    one of an older video. `status` 'stale' saves a result whose transcript changed while
    it was being made (a batch result collected later)."""
    key = (episode_id, model, prompt.PROMPT_VERSION)
    with conn:
        paid = conn.execute(
            "SELECT 1 FROM enrichment WHERE episode_id=? AND model=? AND prompt_version=? "
            "AND status IN ('done', 'stale')",
            key,
        ).fetchone()
        if error and paid:
            log.warning("episode %s %s: keeping the existing result", episode_id, model)
            return
        conn.execute(
            """INSERT OR REPLACE INTO enrichment (episode_id, model, prompt_version, status,
                 data, raw, error, tokens_in, tokens_out, elapsed_s, video_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                *key,
                "failed" if error else status,
                json.dumps(data, ensure_ascii=False) if data is not None else None,
                reply.content if reply else None,
                error,
                reply.tokens_in if reply else None,
                reply.tokens_out if reply else None,
                elapsed,
                video_id,
            ),
        )
        if not error and model in s.index_enrich_models:
            # The description feeds the index; without this the step stays 'done' and
            # the new text never reaches it.
            pipeline.invalidate(conn, episode_id, ["index"])


def enrich_episode(
    s: Settings, conn: sqlite3.Connection, episode_id: int, model: str, force: bool = False
) -> dict[str, Any] | None:
    """Run the model on the episode; None if a result for this prompt version exists."""
    cfg = s.enrich_models.get(model)
    if cfg is None:
        raise ValueError(f"unknown model {model!r}; configured: {sorted(s.enrich_models)}")
    video_id, why = not_ready(conn, episode_id)
    if video_id is None or why:
        log.warning("%s", why)
        return {"model": model, "error": why}
    if valid_enrichment(conn, episode_id, model, video_id) and not force:
        return None
    t0 = time.monotonic()
    data: dict[str, Any] | None = None
    reply: client.Reply | None = None
    error = None
    try:
        # Inside the try: an episode without a transcript raises here and must not abort
        # the whole run.
        if cfg.get("api") == "anthropic":
            req = claude_request(s, conn, episode_id, cfg)
            t0 = time.monotonic()
            reply = claude.chat_params(cfg, req)
        else:
            msgs = prompt.to_messages(parts(s, conn, episode_id))
            if cfg.get("local"):
                wait_if_paused(s.pause_start, s.pause_end)
            t0 = time.monotonic()
            reply = client.chat(cfg, msgs, schema=prompt.JSON_SCHEMA)
        data = client.parse_json(reply.content)
    except Exception as e:  # stored, the run goes on with the next episode
        error = f"{type(e).__name__}: {e}"[:2000]
    elapsed = round(time.monotonic() - t0, 1)
    store(s, conn, episode_id, model, video_id, reply, data, error, elapsed)
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
