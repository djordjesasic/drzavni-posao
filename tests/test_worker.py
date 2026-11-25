import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_enrich import _settings  # tests/ is on sys.path (pytest rootdir mode)
from test_pipeline import _db, _episode

from dp import pipeline, worker
from dp.transcribe import steps


def _hwmon(root: Path, sensors: list[tuple[str, str, int]]) -> None:
    for i, (name, label, milli) in enumerate(sensors):
        h = root / f"hwmon{i}"
        h.mkdir(parents=True)
        (h / "name").write_text(name + "\n")
        (h / "temp1_input").write_text(f"{milli}\n")
        if label:
            (h / "temp1_label").write_text(label + "\n")


def test_read_temps_picks_the_sensors_and_the_hottest_ram_module(tmp_path: Path) -> None:
    _hwmon(
        tmp_path,
        [("acpitz", "", 53000), ("nvme", "Composite", 65850), ("k10temp", "Tctl", 53500),
         ("amdgpu", "edge", 61000), ("spd5118", "", 65500), ("spd5118", "", 67250)],
    )  # fmt: skip
    assert worker.read_temps(tmp_path) == {"nvme": 65.85, "cpu": 53.5, "gpu": 61.0, "ram": 67.25}
    assert worker.read_temps(tmp_path / "missing") == {}


def test_too_hot_has_hysteresis() -> None:
    lim = worker.Limits(cpu_c=90, gpu_c=95, nvme_c=80, margin_c=5)
    assert not worker.too_hot({"cpu": 89}, lim, was_hot=False)
    assert worker.too_hot({"cpu": 90}, lim, was_hot=False)
    assert worker.too_hot({"cpu": 86}, lim, was_hot=True)  # still within the margin
    assert not worker.too_hot({"cpu": 84.9}, lim, was_hot=True)
    assert worker.too_hot({"gpu": 50, "nvme": 81}, lim, was_hot=False)
    assert not worker.too_hot({}, lim, was_hot=True)  # no sensors: never blocks
    assert worker.too_hot({"ram": 80}, lim, was_hot=False)
    assert not worker.too_hot({"ram": 74.9}, lim, was_hot=True)


def test_wait_until_cool_waits_mid_episode_until_cooled(tmp_path: Path) -> None:
    hw = tmp_path / "hw"
    _hwmon(hw, [("spd5118", "", 99000)])
    temp = hw / "hwmon0" / "temp1_input"
    h = worker.Health(tmp_path / "t.sqlite", worker.Limits(), hw)
    h.state = "working"
    readings = iter([90000, 76000, 74000])
    sleeps: list[float] = []

    def sleep(sec: float) -> None:
        sleeps.append(sec)
        temp.write_text(f"{next(readings)}\n")

    h.wait_until_cool(sleep)
    assert sleeps == [worker.HOT_WAIT_S] * 3  # 76 is within the margin: not yet
    assert (h.hot, h.state) == (False, "working")
    h.wait_until_cool(sleep)  # cool: returns at once
    assert len(sleeps) == 3


def _done(conn: sqlite3.Connection, eid: int, vid: str, steps_: tuple[str, ...]) -> None:
    for st in steps_:
        conn.execute(
            "INSERT OR REPLACE INTO episode_steps (episode_id, step, status, video_id) "
            "VALUES (?, ?, 'done', ?)",
            (eid, st, vid),
        )
    conn.commit()


def test_next_episode_order_and_retry_rules(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    for eid in (1, 2, 3, 4, 5):
        _episode(conn, eid, f"v{eid}")
    conn.execute("UPDATE episodes SET retired=1 WHERE id=5")
    _done(conn, 1, "v1", pipeline.STEPS)  # everything done
    _done(conn, 2, "v2", pipeline.STEPS[:2])
    conn.execute(
        "INSERT INTO episode_steps (episode_id, step, status, video_id, attempts, retry_after) "
        "VALUES (2, 'transcribe', 'failed', 'v2', 1, datetime('now', '+600 seconds'))"
    )
    conn.commit()
    assert worker.next_episode(conn, set()) == 3  # 2 waits for its retry time
    assert worker.next_episode(conn, {3}) == 4
    conn.execute("UPDATE episode_steps SET retry_after=datetime('now', '-1 seconds')")
    assert worker.next_episode(conn, set()) == 2  # due again
    conn.execute("UPDATE episode_steps SET attempts=? WHERE episode_id=2", (pipeline.MAX_ATTEMPTS,))
    assert worker.next_episode(conn, set()) == 3  # given up: left for the owner
    _done(conn, 1, "old", ("index",))  # a step done for another video is not done
    assert worker.next_episode(conn, set()) == 1
    assert worker.next_episode(conn, {1, 2, 3, 4}) is None


def test_failed_step_waits_longer_each_time_and_resets(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")

    def boom() -> dict[str, Any]:
        raise RuntimeError("whisper down")

    def row() -> sqlite3.Row:
        return conn.execute(
            "SELECT status, attempts, (julianday(retry_after) - julianday('now')) * 86400 AS wait"
            " FROM episode_steps WHERE step='download'"
        ).fetchone()

    for attempts, wait in ((1, 600), (2, 1200), (3, 2400)):
        with pytest.raises(RuntimeError):
            pipeline.run_step(conn, 1, "download", "v1", boom)
        r = row()
        assert (r["status"], r["attempts"]) == ("failed", attempts)
        assert wait - 5 < r["wait"] <= wait
    pipeline.run_step(conn, 1, "download", "v1", lambda: {})
    r = row()
    assert (r["status"], r["attempts"], r["wait"]) == ("done", 0, None)


def test_interrupted_step_goes_back_to_pending_without_a_penalty(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")

    def stopped() -> dict[str, Any]:
        raise SystemExit("signal 15")

    with pytest.raises(SystemExit):
        pipeline.run_step(conn, 1, "transcribe", "v1", stopped)
    r = conn.execute("SELECT status, attempts, retry_after FROM episode_steps").fetchone()
    assert tuple(r) == ("pending", 0, None)


class _Health:
    def __init__(self, hot: list[bool]) -> None:
        self.hot_seq, self.hot = hot, False
        self.state, self.episode = "idle", None

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def check(self) -> dict[str, float]:
        self.hot = self.hot_seq.pop(0) if self.hot_seq else False
        return {}


def test_run_processes_in_order_waits_when_hot_and_backs_off_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    conn = _db(tmp_path)
    for eid in (1, 2, 3):
        _episode(conn, eid, f"v{eid}")
    ran: list[int] = []
    sleeps: list[float] = []
    pauses: list[int] = []

    def fake_run_episode(
        s: Any, conn: sqlite3.Connection, eid: int, steps_: list[str], **kw: Any
    ) -> Any:
        ran.append(eid)
        assert callable(kw["before_whisper"])
        if eid == 2:  # YouTube refuses: the download step fails

            def fail() -> dict[str, Any]:
                raise RuntimeError("HTTP Error 403")

            pipeline.run_step(conn, eid, "download", f"v{eid}", fail)
        _done(conn, eid, f"v{eid}", pipeline.STEPS)
        return {st: {} for st in steps_}

    monkeypatch.setattr(steps, "run_episode", fake_run_episode)
    monkeypatch.setattr(steps, "polite_pause", lambda s: pauses.append(1))
    monkeypatch.setattr(worker, "wait_if_paused", lambda *a, **k: 0)
    res = worker.run(s, conn, limit=3, sleep=sleeps.append, health=_Health([True, False]))
    assert ran == [1, 2, 3] and res == {"handled": 3, "failed": 1}
    assert sleeps == [worker.HOT_WAIT_S, worker.DOWNLOAD_BACKOFF_FIRST_S]
    assert len(pauses) == 3  # a pause after every download attempt, failed ones too
    # Nothing left: a limited run stops instead of idling.
    assert worker.run(s, conn, limit=5, sleep=sleeps.append, health=_Health([])) == {
        "handled": 0, "failed": 0,
    }  # fmt: skip


def test_health_thread_logs_to_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path)
    _hwmon(tmp_path / "hw", [("k10temp", "Tctl", 91000)])
    monkeypatch.setattr(worker, "HEALTH_EVERY_S", 0.05)
    h = worker.Health(tmp_path / "t.sqlite", worker.Limits(), tmp_path / "hw")
    h.start()
    assert h.hot
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if conn.execute("SELECT COUNT(*) FROM worker_health").fetchone()[0]:
            break
        time.sleep(0.05)
    h.stop()
    row = conn.execute("SELECT cpu_c, state FROM worker_health").fetchone()
    assert tuple(row) == (91.0, "hot")
