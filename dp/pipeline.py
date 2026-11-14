"""Per-episode step bookkeeping: every step records its status in episode_steps.

A step counts as done only for the video it was produced from, so a re-pointed
episode is processed again. Steps can be interrupted and resumed at any time.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

STEPS = ("download", "prepare", "transcribe", "speakers", "index")


def is_done(conn: sqlite3.Connection, episode_id: int, step: str, video_id: str) -> bool:
    row = conn.execute(
        "SELECT status, video_id FROM episode_steps WHERE episode_id=? AND step=?",
        (episode_id, step),
    ).fetchone()
    return bool(row and row["status"] == "done" and row["video_id"] == video_id)


def _set(conn: sqlite3.Connection, episode_id: int, step: str, **fields: Any) -> None:
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    updates = ", ".join(f"{k}=excluded.{k}" for k in fields)
    with conn:
        conn.execute(
            f"""INSERT INTO episode_steps (episode_id, step, {cols}, updated_at)
                VALUES (?, ?, {marks}, datetime('now'))
                ON CONFLICT(episode_id, step) DO UPDATE SET {updates},
                  updated_at=datetime('now')""",
            (episode_id, step, *fields.values()),
        )


def run_step(
    conn: sqlite3.Connection,
    episode_id: int,
    step: str,
    video_id: str,
    fn: Callable[[], dict[str, Any]],
    *,
    force: bool = False,
) -> dict[str, Any] | None:
    """Run fn unless the step is already done for this video; None means skipped."""
    if not force and is_done(conn, episode_id, step, video_id):
        return None
    _set(
        conn,
        episode_id,
        step,
        status="running",
        video_id=video_id,
        error=None,
        started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        elapsed_s=None,
        info=None,
    )
    t0 = time.monotonic()
    try:
        info = fn()
    except BaseException as e:
        _set(
            conn,
            episode_id,
            step,
            status="failed",
            error=f"{type(e).__name__}: {e}"[:2000],
            elapsed_s=round(time.monotonic() - t0, 2),
        )
        raise
    elapsed = round(time.monotonic() - t0, 2)
    _set(
        conn,
        episode_id,
        step,
        status="done",
        elapsed_s=elapsed,
        info=json.dumps(info, ensure_ascii=False),
    )
    log.info("episode %s %s done in %.1f s", episode_id, step, elapsed)
    return info
