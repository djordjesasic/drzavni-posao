"""Per-episode step bookkeeping: every step records its status in episode_steps.

A step counts as done only for the video it was produced from, so a re-pointed
episode is processed again. Steps can be interrupted and resumed at any time.

Invalidation rule: when a step actually runs and finishes as done, every later step
in STEPS that already has a row for the episode is set back to 'pending', because
its input was just rebuilt (e.g. a forced transcribe wipes the speaker labels).
A skipped step changes nothing. Timestamps (started_at, updated_at) are UTC.

Exception: a step whose info dict has UNCHANGED ("unchanged") set to True reports that
it ran but its output is identical to what it found (e.g. a forced transcribe that
reused every chunk). run_step honours that, and leaves later steps alone, only if the
step was already done for this video when run_step started: otherwise the output it
found may come from a run that died before it could invalidate the later steps. The
key is stored in info like any other.

Concurrency: another process (dp enrich) may set a running step back to 'pending'
because its input changed meanwhile. The finishing run then keeps 'pending' instead of
marking the step done, so the next run picks the new input up.
"""

import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterable
from typing import Any

log = logging.getLogger(__name__)

STEPS = ("download", "prepare", "transcribe", "speakers", "index")
# Data each step reads, so it may only run when these are part of the same call or done
# for the current video (after a re-point the old video's files are still on disk).
# index does not need speakers: without speaker names it is still useful.
REQUIRES: dict[str, tuple[str, ...]] = {
    "download": (),
    "prepare": ("download",),
    # Reads only prepare's output (audio.wav, chunks.json). After a re-point prepare is
    # not done for the new video, so the old audio cannot be used by mistake.
    "transcribe": ("prepare",),
    "speakers": ("prepare", "transcribe"),
    "index": ("transcribe",),
}
UNCHANGED = "unchanged"  # reserved info key, see the module docstring


def is_done(conn: sqlite3.Connection, episode_id: int, step: str, video_id: str) -> bool:
    row = conn.execute(
        "SELECT status, video_id FROM episode_steps WHERE episode_id=? AND step=?",
        (episode_id, step),
    ).fetchone()
    return bool(row and row["status"] == "done" and row["video_id"] == video_id)


def will_run(
    conn: sqlite3.Connection, episode_id: int, step: str, video_id: str, force: bool
) -> bool:
    """Whether run_step will really execute this step; the single place for that rule,
    so the YouTube pause and the 18:55-20:00 wait agree with what actually runs."""
    return force or not is_done(conn, episode_id, step, video_id)


def invalidate(conn: sqlite3.Connection, episode_id: int, steps: Iterable[str]) -> None:
    """Set the existing rows of these steps to 'pending'. Does not commit: the caller
    owns the transaction, so the change lands together with whatever caused it."""
    steps = list(steps)
    if not steps:
        return
    conn.execute(
        f"""UPDATE episode_steps SET status='pending', updated_at=datetime('now')
            WHERE episode_id=? AND step IN ({", ".join("?" * len(steps))})""",
        (episode_id, *steps),
    )


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


RETRY_FIRST_S = 600  # a failed step waits 10 min, then twice as long after each failure
RETRY_MAX_S = 24 * 3600
MAX_ATTEMPTS = 5  # after this many failures in a row the worker leaves the step alone


def _schedule_retry(conn: sqlite3.Connection, episode_id: int, step: str) -> None:
    """Count the failure and set when the worker may try the step again (UTC)."""
    with conn:
        conn.execute(
            "UPDATE episode_steps SET attempts=attempts+1 WHERE episode_id=? AND step=?",
            (episode_id, step),
        )
        attempts = conn.execute(
            "SELECT attempts FROM episode_steps WHERE episode_id=? AND step=?",
            (episode_id, step),
        ).fetchone()[0]
        delay = min(RETRY_FIRST_S * 2 ** (attempts - 1), RETRY_MAX_S)
        conn.execute(
            "UPDATE episode_steps SET retry_after=datetime('now', ?) WHERE episode_id=? AND step=?",
            (f"+{delay} seconds", episode_id, step),
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
    if not will_run(conn, episode_id, step, video_id, force):
        return None
    was_done = is_done(conn, episode_id, step, video_id)  # see UNCHANGED in the docstring
    _set(
        conn,
        episode_id,
        step,
        status="running",
        video_id=video_id,
        error=None,
        started_at=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        elapsed_s=None,
        info=None,
    )
    t0 = time.monotonic()
    try:
        info = fn()
    except Exception as e:
        _set(
            conn,
            episode_id,
            step,
            status="failed",
            error=f"{type(e).__name__}: {e}"[:2000],
            elapsed_s=round(time.monotonic() - t0, 2),
        )
        _schedule_retry(conn, episode_id, step)
        raise
    except BaseException:
        # Stopped from outside (worker stop, Ctrl-C): not the step's fault, so it is
        # neither failed nor delayed; the next run picks it up again.
        _set(conn, episode_id, step, status="pending", error="interrupted")
        raise
    elapsed = round(time.monotonic() - t0, 2)
    later = STEPS[STEPS.index(step) + 1 :] if step in STEPS else ()
    with conn:  # one transaction: never done here while downstream still looks done
        # Only a row still 'running' becomes done; see "Concurrency" in the docstring.
        cur = conn.execute(
            """UPDATE episode_steps
               SET status=CASE WHEN status='running' THEN 'done' ELSE status END,
                   elapsed_s=?, info=?, updated_at=datetime('now'),
                   attempts=0, retry_after=NULL
               WHERE episode_id=? AND step=?""",
            (elapsed, json.dumps(info, ensure_ascii=False), episode_id, step),
        )
        if cur.rowcount == 0:
            # catalog-build re-pointed the episode meanwhile and dropped its step rows;
            # this output belongs to the old video, so record nothing and invalidate
            # nothing (there is nothing left to invalidate).
            msg = f"episode {episode_id} was re-pointed while {step} ran; result discarded"
            log.warning(msg)
            raise RuntimeError(msg)
        status = conn.execute(
            "SELECT status FROM episode_steps WHERE episode_id=? AND step=?",
            (episode_id, step),
        ).fetchone()["status"]
        # The output was rebuilt either way, so later steps are invalidated even if this
        # step itself was set back to pending meanwhile.
        if not (was_done and info.get(UNCHANGED) is True):
            invalidate(conn, episode_id, later)
    if status != "done":
        log.warning(
            "episode %s %s finished, but was set to %r meanwhile; left so",
            episode_id,
            step,
            status,
        )
        return info
    log.info("episode %s %s done in %.1f s", episode_id, step, elapsed)
    return info
