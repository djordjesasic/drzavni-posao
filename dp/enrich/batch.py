"""Descriptions through the Message Batches API: half the price, results within 24 h.

submit sends the episodes still without a description (frames included for a vision
model) and records every batch in enrich_batches; collect stores the results of ended
batches exactly like a direct call would (dp.enrich.run.store). Both are safe to repeat:
an episode already in a submitted batch is not sent again, and storing a result twice
writes the same row.
"""

import json
import logging
import sqlite3
from typing import Any

from dp import pipeline
from dp.config import Settings
from dp.db import active_video_id
from dp.enrich import claude, client, frames, prompt
from dp.enrich import run as enrich_run

log = logging.getLogger(__name__)

# The API takes up to 256 MB per batch; frames make a request ~1-2 MB, so stay well below.
MAX_BATCH_BYTES = 150 * 1024 * 1024


def _api() -> Any:
    import anthropic

    return anthropic.Anthropic()


def _cfg(s: Settings, model: str) -> dict[str, Any]:
    cfg = s.enrich_models.get(model)
    if cfg is None or cfg.get("api") != "anthropic":
        raise ValueError(f"{model!r} is not a configured anthropic model")
    return cfg


def pending(conn: sqlite3.Connection, model: str) -> set[int]:
    """Episodes in batches of this model that were sent but not collected yet."""
    out: set[int] = set()
    for (eps,) in conn.execute(
        "SELECT episodes FROM enrich_batches WHERE model=? AND status='submitted'", (model,)
    ):
        out |= {int(e) for e in json.loads(eps)}
    return out


def candidates(
    s: Settings,
    conn: sqlite3.Connection,
    model: str,
    limit: int | None = None,
    only: list[int] | None = None,
) -> list[int]:
    """Episodes (lowest first) ready for a description of this model and without a valid
    one (a stale one is redone), not waiting in a batch, with a video if the model uses
    frames."""
    cfg = _cfg(s, model)
    waiting = pending(conn, model)
    ids = only or [
        r[0] for r in conn.execute(
            "SELECT e.id FROM episodes e WHERE e.retired=0 AND e.video_id IS NOT NULL "
            "ORDER BY e.id"
        )
    ]  # fmt: skip
    out = []
    for eid in ids:
        if eid in waiting:
            continue
        vid, why = enrich_run.not_ready(conn, eid)
        if vid is None or why:
            continue
        if enrich_run.valid_enrichment(conn, eid, model, vid):
            continue
        if cfg.get("frames_every_s") and not frames.video_path(s.data_dir, eid).exists():
            continue
        out.append(eid)
        if limit and len(out) >= limit:
            break
    return out


def submit(
    s: Settings, conn: sqlite3.Connection, model: str, ids: list[int], api: Any = None
) -> list[str]:
    """Send the episodes in as few batches as the size limit allows; returns batch ids."""
    cfg = _cfg(s, model)
    api = api or _api()
    sent: list[str] = []
    requests: list[dict[str, Any]] = []
    videos: dict[str, str] = {}
    size = 0

    def flush() -> None:
        nonlocal requests, videos, size
        if not requests:
            return
        batch = api.messages.batches.create(requests=requests)
        with conn:
            conn.execute(
                "INSERT INTO enrich_batches (id, model, prompt_version, episodes) "
                "VALUES (?, ?, ?, ?)",
                (batch.id, model, prompt.PROMPT_VERSION, json.dumps(videos)),
            )
        log.info("batch %s: %d episodes, %.0f MB", batch.id, len(requests), size / 2**20)
        sent.append(batch.id)
        requests, videos, size = [], {}, 0

    for eid in ids:
        vid, why = enrich_run.not_ready(conn, eid)
        if vid is None or why:
            log.warning("episode %s skipped: %s", eid, why)
            continue
        try:
            p = enrich_run.claude_request(s, conn, eid, cfg)
        except Exception as e:  # no transcript or video: leave it for later
            log.warning("episode %s skipped: %s: %s", eid, type(e).__name__, e)
            continue
        n = len(json.dumps(p))
        if requests and size + n > MAX_BATCH_BYTES:
            flush()
        requests.append({"custom_id": f"ep-{eid}", "params": p})
        videos[str(eid)] = vid
        size += n
    flush()
    return sent


def status(conn: sqlite3.Connection, api: Any = None) -> list[dict[str, Any]]:
    """Every batch: what we sent and, for those not collected yet, the API's progress
    (request_counts: processing, succeeded, errored, canceled, expired)."""
    out = []
    for r in conn.execute(
        "SELECT id, model, status, submitted_at, collected_at, episodes FROM enrich_batches "
        "ORDER BY submitted_at"
    ):
        row: dict[str, Any] = {
            "id": r["id"], "model": r["model"], "episodes": len(json.loads(r["episodes"])),
            "submitted_utc": r["submitted_at"], "status": r["status"],
        }  # fmt: skip
        if r["status"] == "submitted":
            api = api or _api()
            b = api.messages.batches.retrieve(r["id"])
            row["api"] = b.processing_status  # in_progress | canceling | ended
            row["counts"] = dict(b.request_counts)
        else:
            row["collected_utc"] = r["collected_at"]
        out.append(row)
    return out


def collect(s: Settings, conn: sqlite3.Connection, api: Any = None) -> dict[str, int]:
    """Store the results of every ended batch; counts what happened."""
    api = api or _api()
    counts = {"waiting_batches": 0, "done": 0, "stale": 0, "failed": 0, "repointed": 0}
    rows = conn.execute(
        "SELECT id, model, prompt_version, episodes, submitted_at FROM enrich_batches "
        "WHERE status='submitted' ORDER BY submitted_at"
    ).fetchall()
    for row in rows:
        if api.messages.batches.retrieve(row["id"]).processing_status != "ended":
            counts["waiting_batches"] += 1
            continue
        if row["prompt_version"] != prompt.PROMPT_VERSION:
            log.warning("batch %s was made for another prompt version; ignored", row["id"])
        else:
            videos = json.loads(row["episodes"])
            summary = {"done": 0, "stale": 0, "failed": 0, "repointed": 0}
            for res in api.messages.batches.results(row["id"]):
                outcome = _store_one(s, conn, row, videos, res)
                summary[outcome] += 1
                counts[outcome] += 1
            log.info("batch %s collected: %s", row["id"], summary)
        with conn:
            conn.execute(
                "UPDATE enrich_batches SET status='collected', collected_at=datetime('now') "
                "WHERE id=?",
                (row["id"],),
            )
    for k, v in counts.items():
        if k != "waiting_batches" and v:
            log.info("%s: %d", k, v)
    return counts


def _store_one(
    s: Settings, conn: sqlite3.Connection, row: sqlite3.Row, videos: dict[str, str], res: Any
) -> str:
    eid = int(res.custom_id.removeprefix("ep-"))
    vid = videos[str(eid)]
    if active_video_id(conn, eid) != vid:
        return "repointed"  # made from a video the episode no longer points to
    reply: client.Reply | None = None
    data = error = None
    if res.result.type == "succeeded":
        try:
            reply = claude.reply(res.result.message)
            data = client.parse_json(reply.content)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:2000]
    else:
        detail = getattr(res.result, "error", None)
        error = f"batch result {res.result.type}: {detail}"[:2000]
    # The transcript was rewritten after the batch was sent: keep the paid result, but as
    # stale, so it is made again (the index uses it meanwhile).
    changed = conn.execute(
        "SELECT 1 FROM episode_steps WHERE episode_id=? AND step='transcribe' AND updated_at > ?",
        (eid, row["submitted_at"]),
    ).fetchone()
    status = "stale" if changed or not pipeline.is_done(conn, eid, "transcribe", vid) else "done"
    enrich_run.store(s, conn, eid, row["model"], vid, reply, data, error, None, status)
    return "failed" if error else status
