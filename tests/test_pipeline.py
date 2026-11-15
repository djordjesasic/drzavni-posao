import argparse
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dp import cli, pipeline
from dp.catalog.classify import build_episodes
from dp.db import connect, migrate


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    return conn


def _episode(conn: sqlite3.Connection, eid: int, vid: str | None, **cols: Any) -> None:
    if vid is not None:
        conn.execute(
            "INSERT INTO videos (video_id, title, playlists, raw) VALUES (?, 't', '[]', '{}')",
            (vid,),
        )
    conn.execute(
        "INSERT INTO episodes (id, video_id, title, kind, season, duration_s)"
        " VALUES (?, ?, 't', 'episode', ?, ?)",
        (eid, vid, cols.get("season"), cols.get("duration_s")),
    )
    conn.commit()


def _status(conn: sqlite3.Connection, eid: int = 1) -> dict[str, str]:
    return {
        r["step"]: r["status"]
        for r in conn.execute("SELECT step, status FROM episode_steps WHERE episode_id=?", (eid,))
    }


def _run_all(conn: sqlite3.Connection, steps: tuple[str, ...], force: bool = False) -> None:
    for step in steps:
        pipeline.run_step(conn, 1, step, "v1", lambda: {}, force=force)


def test_rerun_invalidates_later_steps(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, pipeline.STEPS)
    assert set(_status(conn).values()) == {"done"}

    pipeline.run_step(conn, 1, "transcribe", "v1", lambda: {}, force=True)
    assert _status(conn) == {
        "download": "done",
        "prepare": "done",
        "transcribe": "done",
        "speakers": "pending",
        "index": "pending",
    }
    assert not pipeline.is_done(conn, 1, "speakers", "v1")


def test_skipped_step_invalidates_nothing(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, pipeline.STEPS)
    assert pipeline.run_step(conn, 1, "transcribe", "v1", lambda: {}) is None
    assert set(_status(conn).values()) == {"done"}


def test_failed_step_invalidates_nothing_and_missing_rows_are_not_created(
    tmp_path: Path,
) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, ("download", "prepare", "transcribe"))

    def boom() -> dict[str, Any]:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        pipeline.run_step(conn, 1, "prepare", "v1", boom, force=True)
    assert _status(conn) == {"download": "done", "prepare": "failed", "transcribe": "done"}

    pipeline.run_step(conn, 1, "prepare", "v1", lambda: {}, force=True)
    assert _status(conn) == {"download": "done", "prepare": "done", "transcribe": "pending"}


def test_started_at_is_utc(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    seen: dict[str, str] = {}

    def fn() -> dict[str, Any]:
        row = conn.execute("SELECT started_at, updated_at FROM episode_steps").fetchone()
        seen.update(dict(row))
        return {}

    pipeline.run_step(conn, 1, "download", "v1", fn)
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", seen["started_at"])
    utc_now = conn.execute("SELECT datetime('now')").fetchone()[0]
    a = conn.execute("SELECT strftime('%s', ?)", (seen["started_at"],)).fetchone()[0]
    b = conn.execute("SELECT strftime('%s', ?)", (utc_now,)).fetchone()[0]
    assert abs(int(a) - int(b)) < 5  # same clock as datetime('now'), whatever the TZ
    assert (
        abs(
            int(a)
            - int(conn.execute("SELECT strftime('%s', ?)", (seen["updated_at"],)).fetchone()[0])
        )
        < 5
    )


def test_repoint_deletes_derived_data_but_keeps_enrichment(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    title = "DRŽAVNI POSAO [HQ] - Ep.1: A (01.10.2014.)"
    other = "DRŽAVNI POSAO [HQ] - Ep.2: B (02.10.2014.)"
    for vid, t, dur in (("a", title, 500), ("b", other, 500)):
        conn.execute(
            "INSERT INTO videos (video_id, title, duration_s, playlists, raw)"
            " VALUES (?, ?, ?, '[]', '{}')",
            (vid, t, dur),
        )
    conn.commit()
    build_episodes(conn)
    for eid in (1, 2):
        conn.execute(
            "INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin, text_fold)"
            " VALUES (?, 0, 0, 1, 't', 't', 't')",
            (eid,),
        )
        cur = conn.execute(
            "INSERT INTO windows (episode_id, kind, start_ms, end_ms, text, embedding)"
            " VALUES (?, 'transcript', 0, 1, 'abc def', x'00')",
            (eid,),
        )
        conn.execute(
            "INSERT INTO windows_fts (rowid, text_fold) VALUES (?, 'abc def')", (cur.lastrowid,)
        )
        conn.execute(
            "INSERT INTO enrichment (episode_id, model, prompt_version, status)"
            " VALUES (?, 'm', 1, 'done')",
            (eid,),
        )
        conn.execute(
            "INSERT INTO episode_steps (episode_id, step, status) VALUES (?, 'index', 'done')",
            (eid,),
        )
    conn.commit()

    # A longer upload of episode 1 wins: its derived data belongs to the old video.
    conn.execute(
        "INSERT INTO videos (video_id, title, duration_s, playlists, raw)"
        " VALUES ('a2', ?, 900, '[]', '{}')",
        (title,),
    )
    conn.commit()
    assert build_episodes(conn).steps_reset == [1]

    def count(table: str, eid: int) -> int:
        return conn.execute(f"SELECT count(*) FROM {table} WHERE episode_id=?", (eid,)).fetchone()[
            0
        ]

    for table in ("segments", "windows", "episode_steps"):
        assert (count(table, 1), count(table, 2)) == (0, 1), table
    assert count("enrichment", 1) == 1
    assert conn.execute("SELECT count(*) FROM windows_fts").fetchone()[0] == 1


def _run_args(**kw: Any) -> argparse.Namespace:
    return argparse.Namespace(
        steps=kw.get("steps", "download,prepare"),
        episodes=kw.get("episodes", "1,2,3"),
        limit=0,
        force=kw.get("force", False),
        rebuild_index=kw.get("rebuild_index", False),
    )


def _patched_run(
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    fail: set[int] = frozenset(),  # type: ignore[assignment]
) -> list[int]:
    pauses: list[int] = []
    monkeypatch.setattr(cli, "_db", lambda: conn)

    def fake_run_episode(
        s: Any, c: Any, eid: int, steps: list[str], force: bool = False, **kw: Any
    ) -> dict:
        if eid in fail:
            raise RuntimeError("HTTP 429")
        if "download" in steps:
            pipeline.run_step(c, eid, "download", f"v{eid}", lambda: {}, force=force)
        return {}

    monkeypatch.setattr(cli.transcribe_steps, "run_episode", fake_run_episode)
    monkeypatch.setattr(cli.transcribe_steps, "polite_pause", lambda s: pauses.append(1))
    return pauses


def test_pause_after_failed_download_but_not_after_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path)
    for i in (1, 2, 3):
        _episode(conn, i, f"v{i}")
    pipeline.run_step(conn, 2, "download", "v2", lambda: {})  # episode 2 already downloaded
    pauses = _patched_run(conn, monkeypatch, fail={1})
    with pytest.raises(SystemExit):
        cli.cmd_run(_run_args())
    # 1 failed download -> pause; 2 skipped -> no pause; 3 is last -> no pause.
    assert len(pauses) == 1


def test_no_pause_without_download_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    for i in (1, 2, 3):
        _episode(conn, i, f"v{i}")
    pauses = _patched_run(conn, monkeypatch)
    cli.cmd_run(_run_args(steps="prepare"))
    assert pauses == []


def test_pause_between_successful_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path)
    for i in (1, 2, 3):
        _episode(conn, i, f"v{i}")
    pauses = _patched_run(conn, monkeypatch)
    cli.cmd_run(_run_args())
    assert len(pauses) == 2


def test_pilot_select_handles_null_season_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")  # season and duration_s are NULL
    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(cli.pilot, "parse_owner", lambda p: [])
    monkeypatch.setattr(cli.pilot, "select", lambda c, o: [SimpleNamespace(grp="g", episode_id=1)])
    monkeypatch.setattr(cli.pilot, "store", lambda c, chosen: None)
    cli.cmd_pilot_select(argparse.Namespace())
    out = capsys.readouterr().out
    assert "s-" in out and "1 pilot episodes stored" in out


def test_unchanged_step_invalidates_nothing(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, pipeline.STEPS)
    info = pipeline.run_step(
        conn, 1, "transcribe", "v1", lambda: {pipeline.UNCHANGED: True}, force=True
    )
    assert info == {"unchanged": True}
    assert set(_status(conn).values()) == {"done"}
    # Only an explicit True counts; False (or a missing key) still invalidates.
    pipeline.run_step(conn, 1, "transcribe", "v1", lambda: {pipeline.UNCHANGED: False}, force=True)
    assert _status(conn)["speakers"] == _status(conn)["index"] == "pending"


def test_step_set_to_pending_meanwhile_stays_pending(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, pipeline.STEPS)

    def index_while_enrich_arrives() -> dict[str, Any]:
        # Another process (dp enrich) invalidates the running index step.
        with conn:
            pipeline.invalidate(conn, 1, ["index"])
        return {"windows": 3}

    info = pipeline.run_step(conn, 1, "index", "v1", index_while_enrich_arrives, force=True)
    assert info == {"windows": 3}
    row = conn.execute(
        "SELECT status, info, elapsed_s FROM episode_steps WHERE step='index'"
    ).fetchone()
    assert row["status"] == "pending" and row["elapsed_s"] is not None
    assert json.loads(row["info"]) == {"windows": 3}
    assert not pipeline.is_done(conn, 1, "index", "v1")


def test_invalidate_leaves_commit_to_caller(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, ("download", "prepare", "transcribe"))
    pipeline.invalidate(conn, 1, ["transcribe", "speakers"])  # speakers has no row: none made
    assert _status(conn) == {"download": "done", "prepare": "done", "transcribe": "pending"}
    conn.rollback()
    assert set(_status(conn).values()) == {"done"}
    pipeline.invalidate(conn, 1, [])


def test_will_run_is_the_single_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _episode(conn, 2, None)
    assert pipeline.will_run(conn, 1, "download", "v1", False)
    _run_all(conn, ("download",))
    assert not pipeline.will_run(conn, 1, "download", "v1", False)
    assert pipeline.will_run(conn, 1, "download", "v1", True)
    assert pipeline.will_run(conn, 1, "download", "vX", False)  # other video
    assert cli._download_pending(conn, 1, True) and not cli._download_pending(conn, 1, False)
    assert not cli._download_pending(conn, 2, True)  # no video: never reaches YouTube

    # run_step, the YouTube pause and the pause-window wait all follow will_run.
    monkeypatch.setattr(pipeline, "will_run", lambda *a: False)
    assert pipeline.run_step(conn, 1, "prepare", "v1", lambda: {}) is None
    assert not cli._download_pending(conn, 1, True)


def test_step_row_deleted_meanwhile_raises(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, ("download", "prepare"))

    def transcribe_while_repointed() -> dict[str, Any]:
        # catalog-build re-points the episode and drops its step rows meanwhile.
        with conn:
            conn.execute("DELETE FROM episode_steps WHERE episode_id=1")
        return {}

    with pytest.raises(RuntimeError, match="episode 1 was re-pointed while transcribe ran"):
        pipeline.run_step(conn, 1, "transcribe", "v1", transcribe_while_repointed)
    assert _status(conn) == {}  # nothing recreated
    assert "re-pointed" in caplog.text


def test_run_passes_rebuild_index_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    seen: dict[str, Any] = {}
    cmd_run = cli.cmd_run
    monkeypatch.setattr(cli, "cmd_run", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(sys, "argv", ["dp", "run", "--steps", "index", "--rebuild-index"])
    cli.main()
    assert seen["rebuild_index"] is True and seen["force"] is False
    monkeypatch.setattr(sys, "argv", ["dp", "run"])
    cli.main()
    assert seen["rebuild_index"] is False

    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    got: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "_db", lambda: conn)
    monkeypatch.setattr(cli, "get_settings", lambda: None)
    monkeypatch.setattr(cli.transcribe_steps, "run_episode", lambda *a, **k: got.append(k) or {})
    cmd_run(_run_args(steps="index", episodes="1", rebuild_index=True))
    assert got == [{"force": False, "rebuild_index": True}]


def test_rebuild_index_without_index_step_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _patched_run(conn, monkeypatch)
    touched: list[int] = []
    monkeypatch.setattr(cli.transcribe_steps, "run_episode", lambda *a, **k: touched.append(1))
    with pytest.raises(SystemExit, match="add --steps index"):
        cli.cmd_run(_run_args(steps="download,prepare,transcribe", rebuild_index=True))
    assert touched == []


def test_unchanged_counts_only_if_done_before(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _episode(conn, 1, "v1")
    _run_all(conn, pipeline.STEPS)
    # A run died while transcribe was running: its UNCHANGED must not spare later steps.
    conn.execute("UPDATE episode_steps SET status='running' WHERE step='transcribe'")
    conn.commit()
    info = pipeline.run_step(conn, 1, "transcribe", "v1", lambda: {pipeline.UNCHANGED: True})
    assert info == {"unchanged": True}  # still stored
    assert _status(conn)["speakers"] == _status(conn)["index"] == "pending"
    # Done for another video before: not "done before" for this one either.
    _run_all(conn, ("speakers", "index"))
    conn.execute("UPDATE episode_steps SET video_id='v0' WHERE step='transcribe'")
    conn.commit()
    pipeline.run_step(conn, 1, "transcribe", "v1", lambda: {pipeline.UNCHANGED: True}, force=True)
    assert _status(conn)["speakers"] == "pending"
