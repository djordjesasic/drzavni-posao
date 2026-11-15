"""Phase 2 worker: one process that takes the next episode with work left and runs its
steps, until everything is done. Safe to stop and start at any time.

Rules (CLAUDE.md and PLAN.md, phase 2):
- the daily pause window is respected before every step (run_episode) and here;
- one Whisper request at a time (file lock in dp.transcribe.whisper);
- YouTube: a pause after every download attempt, and a growing extra pause while
  downloads keep failing (403/429/bot check);
- a failed step waits before the next try (pipeline.RETRY_*), and after
  pipeline.MAX_ATTEMPTS failures in a row it is left for the owner;
- the machine's temperatures are logged every minute; while one is too high no new
  episode is started.
"""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dp import pipeline
from dp.config import Settings
from dp.db import connect
from dp.schedule import wait_if_paused
from dp.transcribe import steps as transcribe_steps

log = logging.getLogger(__name__)

HWMON = Path("/sys/class/hwmon")
HEALTH_EVERY_S = 60
HOT_WAIT_S = 300
DOWNLOAD_BACKOFF_FIRST_S = 300  # after a failed download; doubles while they keep failing
DOWNLOAD_BACKOFF_MAX_S = 2 * 3600
IDLE_WAIT_S = 600  # nothing to do: look again later (failed steps become due over time)


@dataclass(frozen=True)
class Limits:
    cpu_c: float = 90.0  # k10temp Tctl
    gpu_c: float = 95.0  # amdgpu edge
    nvme_c: float = 80.0  # nvme Composite
    margin_c: float = 5.0  # hot until every reading is this much below its limit


def read_temps(root: Path = HWMON) -> dict[str, float]:
    """cpu, gpu and nvme temperature in °C from hwmon; missing sensors are left out."""
    wanted = {("k10temp", "Tctl"): "cpu", ("amdgpu", "edge"): "gpu", ("nvme", "Composite"): "nvme"}
    out: dict[str, float] = {}
    for h in sorted(root.glob("hwmon*")):
        try:
            name = (h / "name").read_text().strip()
            for t in sorted(h.glob("temp*_input")):
                label_file = t.with_name(t.name.replace("_input", "_label"))
                label = label_file.read_text().strip() if label_file.exists() else ""
                key = wanted.get((name, label))
                if key and key not in out:
                    out[key] = int(t.read_text()) / 1000
        except (OSError, ValueError):
            continue
    return out


def too_hot(temps: dict[str, float], limits: Limits, was_hot: bool) -> bool:
    """Hot when a reading reaches its limit; once hot, cool by `margin_c` before going on."""
    margin = limits.margin_c if was_hot else 0.0
    caps = {"cpu": limits.cpu_c, "gpu": limits.gpu_c, "nvme": limits.nvme_c}
    return any(temps[k] >= caps[k] - margin for k in caps if k in temps)


class Health:
    """Background thread: logs temperatures every minute to worker_health, keeps `hot`."""

    def __init__(self, db: Path, limits: Limits, root: Path = HWMON) -> None:
        self.db, self.limits, self.root = db, limits, root
        self.hot = False
        self.state = "idle"
        self.episode: int | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)

    def start(self) -> None:
        self.check()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> dict[str, float]:
        temps = read_temps(self.root)
        self.hot = too_hot(temps, self.limits, self.hot)
        return temps

    def _run(self) -> None:
        conn = connect(self.db)
        try:
            while not self._stop.wait(HEALTH_EVERY_S):
                temps = self.check()
                with conn:
                    conn.execute(
                        "INSERT INTO worker_health (cpu_c, gpu_c, nvme_c, state, episode_id) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (temps.get("cpu"), temps.get("gpu"), temps.get("nvme"),
                         "hot" if self.hot else self.state, self.episode),
                    )  # fmt: skip
        finally:
            conn.close()


def next_episode(conn: sqlite3.Connection, skip: set[int]) -> int | None:
    """Lowest active episode id with a step not done for its current video, that has no
    failed step still waiting for its retry time or given up on."""
    done_all = len(pipeline.STEPS)
    marks = ",".join("?" * len(pipeline.STEPS))
    for (eid,) in conn.execute(
        f"""SELECT e.id FROM episodes e
            WHERE e.retired = 0 AND e.video_id IS NOT NULL
              AND (SELECT COUNT(*) FROM episode_steps s
                   WHERE s.episode_id = e.id AND s.status = 'done' AND s.video_id = e.video_id
                     AND s.step IN ({marks})) < ?
              AND NOT EXISTS (SELECT 1 FROM episode_steps s
                   WHERE s.episode_id = e.id AND s.status = 'failed'
                     AND (s.attempts >= ? OR s.retry_after > datetime('now')))
            ORDER BY e.id""",
        (*pipeline.STEPS, done_all, pipeline.MAX_ATTEMPTS),
    ):
        if eid not in skip:
            return int(eid)
    return None


def run(
    s: Settings,
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    limits: Limits = Limits(),  # noqa: B008 – frozen dataclass, safe as a default
    sleep: Any = time.sleep,
    health: Health | None = None,
) -> dict[str, Any]:
    """Process episodes until there is nothing left (or `limit` episodes were handled).
    Returns counts; a long-running worker service calls it with limit=None, forever."""
    health = health or Health(s.db, limits)
    health.start()
    handled = failed = 0
    skip: set[int] = set()  # episodes that failed outside any step, for this run only
    download_backoff = 0.0
    try:
        while limit is None or handled < limit:
            wait_if_paused(s.pause_start, s.pause_end, sleep=sleep)
            health.check()
            if health.hot:
                health.state = "hot"
                log.warning("too hot, waiting %d s", HOT_WAIT_S)
                sleep(HOT_WAIT_S)
                continue
            eid = next_episode(conn, skip)
            if eid is None:
                if limit is not None:
                    break
                health.state = "idle"
                sleep(IDLE_WAIT_S)
                continue
            vid = conn.execute("SELECT video_id FROM episodes WHERE id=?", (eid,)).fetchone()[0]
            downloading = pipeline.will_run(conn, eid, "download", vid, False)
            health.state, health.episode = "working", eid
            try:
                res = transcribe_steps.run_episode(s, conn, eid, list(pipeline.STEPS))
                log.info("episode %s: %s", eid, {k: v is not None for k, v in res.items()})
                if downloading:
                    download_backoff = 0.0
            except Exception as e:
                failed += 1
                log.warning("episode %s failed: %s: %s", eid, type(e).__name__, e)
                row = conn.execute(
                    "SELECT 1 FROM episode_steps WHERE episode_id=? AND status='failed'", (eid,)
                ).fetchone()
                if row is None:  # not a step failure, so no retry time was set
                    skip.add(eid)
                if downloading and not pipeline.is_done(conn, eid, "download", vid):
                    download_backoff = min(
                        max(download_backoff * 2, DOWNLOAD_BACKOFF_FIRST_S),
                        DOWNLOAD_BACKOFF_MAX_S,
                    )
                    log.warning("download failed, waiting %.0f s", download_backoff)
                    sleep(download_backoff)
            finally:
                health.episode = None
            handled += 1
            if downloading:
                transcribe_steps.polite_pause(s)
    finally:
        health.stop()
    return {"handled": handled, "failed": failed}
