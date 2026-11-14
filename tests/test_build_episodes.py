import json
import sqlite3
from pathlib import Path

from dp.catalog.classify import SPECIAL_ID_BASE, build_episodes
from dp.db import connect, migrate


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    return conn


def _video(
    conn: sqlite3.Connection,
    vid: str,
    title: str,
    playlists: list[str],
    duration_s: int | None = 500,
) -> None:
    conn.execute(
        """INSERT INTO videos (video_id, title, duration_s, playlists, raw)
           VALUES (?, ?, ?, ?, '{}')""",
        (vid, title, duration_s, json.dumps(playlists)),
    )
    conn.commit()


def _special_ids(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["video_id"]: r["id"]
        for r in conn.execute(
            "SELECT id, video_id FROM episodes WHERE kind='special' AND retired=0"
        )
    }


def test_special_ids_are_stable_across_rebuilds(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "e1", "DRŽAVNI POSAO [HQ] - Ep.1: A (01.10.2014.)", ["DRŽAVNI POSAO 3"])
    _video(conn, "s1", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2014.)", [])
    _video(conn, "s2", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2015.)", [])
    build_episodes(conn)
    first = _special_ids(conn)
    assert first == {"s1": SPECIAL_ID_BASE + 1, "s2": SPECIAL_ID_BASE + 2}

    # A special that sorts first appears later: existing ids must not shift.
    _video(conn, "s0", "DRŽAVNI POSAO [HQ] - Specijal (01.01.2014.)", [])
    build_episodes(conn)
    assert _special_ids(conn) == {**first, "s0": SPECIAL_ID_BASE + 3}

    # A special stops being one (videos are never deleted, only retitled/reclassified):
    # the others keep their ids and the build does not fail.
    conn.execute("UPDATE videos SET title='Nešto drugo' WHERE video_id='s1'")
    conn.commit()
    build_episodes(conn)
    assert _special_ids(conn) == {"s2": SPECIAL_ID_BASE + 2, "s0": SPECIAL_ID_BASE + 3}


def test_same_air_date_with_unknown_season_does_not_crash(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "e1", "DRŽAVNI POSAO [HQ] - Ep.1: A (01.10.2014.)", ["DRŽAVNI POSAO 3"])
    _video(conn, "e2", "DRŽAVNI POSAO [HQ] - Ep.2: B (01.10.2014.)", [])  # season unknown
    _video(conn, "s1", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2014.)", [])
    build_episodes(conn)
    season = conn.execute("SELECT season FROM episodes WHERE video_id='s1'").fetchone()[0]
    assert season == 3


def _row(conn: sqlite3.Connection, eid: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT id, video_id, title, retired, retired_video_id FROM episodes WHERE id=?", (eid,)
    ).fetchone()


def _retitle(conn: sqlite3.Connection, vid: str, title: str) -> None:
    conn.execute("UPDATE videos SET title=? WHERE video_id=?", (title, vid))
    conn.commit()


def test_video_moving_to_another_id_retires_the_old_row(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.5: A (01.10.2014.)", ["DRŽAVNI POSAO 3"])
    build_episodes(conn)
    conn.execute(
        "INSERT INTO episode_steps (episode_id, step, status) VALUES (5, 'download', 'done')"
    )
    conn.commit()

    # The title is corrected to another number: no IntegrityError on UNIQUE(video_id).
    _retitle(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.6: A (01.10.2014.)")
    build_episodes(conn)
    assert tuple(_row(conn, 5)) == (5, None, "A", 1, "v1")
    assert tuple(_row(conn, 6)) == (6, "v1", "A", 0, None)


def test_retired_row_is_restored_when_produced_again(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.5: A (01.10.2014.)", ["DRŽAVNI POSAO 3"])
    build_episodes(conn)
    _retitle(conn, "v1", "Nešto drugo")
    build_episodes(conn)
    assert tuple(_row(conn, 5)) == (5, None, "A", 1, "v1")  # kept, not deleted

    _retitle(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.5: B (01.10.2014.)")
    build_episodes(conn)
    assert tuple(_row(conn, 5)) == (5, "v1", "B", 0, None)


def test_special_ids_are_never_reused(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "s1", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2014.)", [])
    _video(conn, "s2", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2015.)", [])
    build_episodes(conn)

    # The highest special is retired; a new special must not take its id.
    _retitle(conn, "s2", "Nešto drugo")
    build_episodes(conn)
    assert tuple(_row(conn, SPECIAL_ID_BASE + 2))[3:] == (1, "s2")
    _video(conn, "s3", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2016.)", [])
    build_episodes(conn)
    assert _special_ids(conn) == {"s1": SPECIAL_ID_BASE + 1, "s3": SPECIAL_ID_BASE + 3}

    # The retired special comes back under its old id (found by retired_video_id).
    _retitle(conn, "s2", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2015.)")
    build_episodes(conn)
    assert _special_ids(conn) == {
        "s1": SPECIAL_ID_BASE + 1,
        "s2": SPECIAL_ID_BASE + 2,
        "s3": SPECIAL_ID_BASE + 3,
    }


def test_duplicate_pick_is_deterministic(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    title = "DRŽAVNI POSAO [HQ] - Ep.{n}: A (01.10.2014.)"
    # Season playlist wins over a longer upload.
    _video(conn, "a1", title.format(n=1), [], 900)
    _video(conn, "a2", title.format(n=1), ["DRŽAVNI POSAO 3"], 500)
    # Then the longer upload.
    _video(conn, "b2", title.format(n=2), [], 900)
    _video(conn, "b1", title.format(n=2), [], 500)
    # Then the smaller video_id.
    _video(conn, "c2", title.format(n=3), [], 500)
    _video(conn, "c1", title.format(n=3), [], 500)
    result = build_episodes(conn)
    picked = dict(conn.execute("SELECT id, video_id FROM episodes").fetchall())
    assert picked == {1: "a2", 2: "b2", 3: "c1"}
    assert set(result.duplicates) == {1, 2, 3}


def test_special_without_duration_is_a_special(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "s1", "Državni posao - Specijal (31.12.2020.)", [], None)
    build_episodes(conn)
    row = conn.execute("SELECT id, title, air_date, kind FROM episodes").fetchone()
    assert tuple(row) == (SPECIAL_ID_BASE + 1, "Specijal", "2020-12-31", "special")


def _add_step(conn: sqlite3.Connection, eid: int) -> None:
    conn.execute(
        "INSERT INTO episode_steps (episode_id, step, status) VALUES (?, 'download', 'done')",
        (eid,),
    )
    conn.commit()


def _steps(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT episode_id FROM episode_steps ORDER BY 1")]


def test_missing_video_retires_its_episode(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.1: A (01.10.2014.)", ["DRŽAVNI POSAO 3"])
    _video(conn, "v2", "DRŽAVNI POSAO [HQ] - Ep.2: B (02.10.2014.)", ["DRŽAVNI POSAO 3"])
    build_episodes(conn)
    conn.execute("UPDATE videos SET missing=1 WHERE video_id='v2'")
    conn.commit()
    r = build_episodes(conn)
    assert r.missing_videos == 1
    assert r.episodes == 1
    assert tuple(_row(conn, 2))[3:] == (1, "v2")


def test_steps_reset_when_duplicate_pick_changes(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    title = "DRŽAVNI POSAO [HQ] - Ep.1: A (01.10.2014.)"
    _video(conn, "a", title, [], 500)
    _video(conn, "b", "DRŽAVNI POSAO [HQ] - Ep.2: B (02.10.2014.)", [])
    build_episodes(conn)
    _add_step(conn, 1)
    _add_step(conn, 2)

    # Same videos: nothing is reset.
    assert build_episodes(conn).steps_reset == []
    assert _steps(conn) == [1, 2]

    # A longer upload of episode 1 now wins: its steps belong to the old video.
    _video(conn, "a2", title, [], 900)
    r = build_episodes(conn)
    assert r.steps_reset == [1]
    assert _steps(conn) == [2]
    assert tuple(_row(conn, 1))[:2] == (1, "a2")


def test_steps_reset_when_retired_id_returns_with_another_video(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    _video(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.5: A (01.10.2014.)", ["DRŽAVNI POSAO 3"])
    _video(conn, "v2", "DRŽAVNI POSAO [HQ] - Ep.6: B (02.10.2014.)", ["DRŽAVNI POSAO 3"])
    build_episodes(conn)
    _add_step(conn, 5)
    _add_step(conn, 6)
    _retitle(conn, "v1", "Nešto drugo")
    _retitle(conn, "v2", "Nešto treće")
    build_episodes(conn)  # 5 and 6 retired, steps kept
    assert _steps(conn) == [5, 6]

    _retitle(conn, "v1", "DRŽAVNI POSAO [HQ] - Ep.5: A (01.10.2014.)")  # same video
    _retitle(conn, "v2", "DRŽAVNI POSAO [HQ] - Ep.7: B (02.10.2014.)")
    _video(conn, "v3", "DRŽAVNI POSAO [HQ] - Ep.6: C (03.10.2014.)", ["DRŽAVNI POSAO 3"])
    r = build_episodes(conn)
    assert r.steps_reset == [6]
    assert _steps(conn) == [5]
