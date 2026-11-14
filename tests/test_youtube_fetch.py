import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
import yt_dlp

from dp.catalog import youtube
from dp.db import connect, migrate

CHANNEL = "https://www.youtube.com/@test"


class FakeYouTube:
    def __init__(self, playlists: dict[str, list[str]]) -> None:
        self.playlists = playlists
        self.fail: set[str] = set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict[str, Any]:
        self.calls.append(url)
        if url in self.fail:
            raise yt_dlp.utils.DownloadError("HTTP Error 429")
        if url.endswith("/videos"):
            return {"entries": [{"id": "v1"}]}
        if url.endswith(("/streams", "/shorts")):
            raise yt_dlp.utils.DownloadError("This channel does not have a streams tab")
        if url.endswith("/playlists"):
            return {"entries": [{"id": p, "title": p} for p in self.playlists]}
        pid = url.split("list=")[1]
        return {"id": pid, "title": pid, "entries": [{"id": v} for v in self.playlists[pid]]}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeYouTube:
    f = FakeYouTube({"PL1": ["v1"], "PL2": ["v2"]})
    monkeypatch.setattr(youtube, "_extract_flat", f)
    return f


def test_failure_keeps_old_listing_and_rerun_resumes(tmp_path: Path, fake: FakeYouTube) -> None:
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert (tmp_path / "current" / "playlists" / "PL2.json").exists()

    fake.playlists = {"PL1": ["v1", "v3"], "PL3": ["v4"]}
    fake.fail = {"https://www.youtube.com/playlist?list=PL3"}
    fake.calls.clear()
    with pytest.raises(youtube.FetchIncomplete) as exc:
        youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert list(exc.value.errors) == ["playlist PL3"]
    # The published listing is untouched until a complete fetch succeeds.
    assert (tmp_path / "current" / "playlists" / "PL2.json").exists()

    fake.fail = set()
    fake.calls.clear()
    counts = youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert fake.calls == ["https://www.youtube.com/playlist?list=PL3"]  # only the missing one
    assert counts["playlist PL1"] == 2
    # The removed playlist does not linger; staging is gone.
    assert sorted(p.name for p in (tmp_path / "current" / "playlists").iterdir()) == [
        "PL1.json",
        "PL3.json",
    ]
    assert not (tmp_path / youtube.STAGING).exists()


def test_missing_tab_is_empty_not_an_error(tmp_path: Path, fake: FakeYouTube) -> None:
    counts = youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert counts["tab_streams"] == 0
    assert json.loads((tmp_path / "current" / "tab_shorts.json").read_text()) == {"entries": []}


def _listing(raw_dir: Path, ids: list[str]) -> None:
    (raw_dir / "playlists").mkdir(parents=True, exist_ok=True)
    entries = [{"id": v, "title": v, "duration": 600} for v in ids]
    (raw_dir / "tab_videos.json").write_text(json.dumps({"entries": entries}))


def test_import_marks_videos_missing_from_listings(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    raw = tmp_path / "raw"
    missing = lambda: dict(conn.execute("SELECT video_id, missing FROM videos"))  # noqa: E731

    _listing(raw, ["v1", "v2"])
    assert youtube.import_raw(conn, raw) == 2
    assert missing() == {"v1": 0, "v2": 0}

    _listing(raw, ["v1"])
    youtube.import_raw(conn, raw, force=True)  # 50% newly missing needs force
    assert missing() == {"v1": 0, "v2": 1}

    _listing(raw, ["v1", "v2"])
    youtube.import_raw(conn, raw)
    assert missing() == {"v1": 0, "v2": 0}


def _db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    return conn


def _write(path: Path, entries: list[dict[str, Any]], title: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    info: dict[str, Any] = {"entries": entries}
    if title:
        info.update(id=title, title=title)
    path.write_text(json.dumps(info))


def test_publish_swaps_current_and_leaves_no_leftovers(tmp_path: Path, fake: FakeYouTube) -> None:
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["current"]
    assert youtube.listings_dir(tmp_path) == tmp_path / "current"


def test_interrupted_publish_reads_old_and_next_fetch_completes(
    tmp_path: Path, fake: FakeYouTube
) -> None:
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    # Simulate a crash in _publish after current -> .old, before staging -> current.
    (tmp_path / youtube.CURRENT).rename(tmp_path / youtube.OLD)
    assert youtube.listings_dir(tmp_path) == tmp_path / youtube.OLD

    conn = _db(tmp_path / "db")
    assert youtube.import_raw(conn, tmp_path) == 2  # v1 and v2, read from .old
    assert {r[0] for r in conn.execute("SELECT video_id FROM videos")} == {"v1", "v2"}

    fake.playlists = {"PL1": ["v1", "v3"]}
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["current", "db"]
    assert youtube.listings_dir(tmp_path) == tmp_path / youtube.CURRENT
    youtube.import_raw(conn, tmp_path, force=True)
    assert dict(conn.execute("SELECT video_id, missing FROM videos")) == {
        "v1": 0,
        "v2": 1,
        "v3": 0,
    }


def test_import_reads_current_then_legacy_layout(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    raw = tmp_path / "raw"
    _write(raw / "tab_videos.json", [{"id": "legacy", "title": "L"}])
    assert youtube.import_raw(conn, raw) == 1
    _write(raw / "current" / "tab_videos.json", [{"id": "legacy", "title": "New"}])
    youtube.import_raw(conn, raw)
    assert conn.execute("SELECT title FROM videos").fetchone()[0] == "New"


def test_stale_staging_is_discarded(tmp_path: Path, fake: FakeYouTube) -> None:
    staging = tmp_path / youtube.STAGING
    _write(staging / "playlists" / "PL1.json", [{"id": "stale"}], "PL1")
    (staging / youtube.STAGING_MARKER).touch()
    old = time.time() - youtube.STAGING_MAX_AGE_S - 60
    os.utime(staging / youtube.STAGING_MARKER, (old, old))
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert "https://www.youtube.com/playlist?list=PL1" in fake.calls  # refetched
    pl1 = json.loads((tmp_path / "current" / "playlists" / "PL1.json").read_text())
    assert [e["id"] for e in pl1["entries"]] == ["v1"]


def test_fresh_staging_is_resumed(tmp_path: Path, fake: FakeYouTube) -> None:
    staging = tmp_path / youtube.STAGING
    _write(staging / "playlists" / "PL1.json", [{"id": "kept"}], "PL1")
    (staging / youtube.STAGING_MARKER).touch()
    youtube.fetch_channel(CHANNEL, tmp_path, pause_s=0)
    assert "https://www.youtube.com/playlist?list=PL1" not in fake.calls


def test_import_skips_private_and_deleted_placeholders(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    raw = tmp_path / "raw"
    good = [{"id": f"v{i}", "title": f"T{i}", "duration": 600} for i in range(20)]
    _write(raw / "tab_videos.json", good)
    youtube.import_raw(conn, raw)

    _write(raw / "tab_videos.json", good[1:])
    _write(
        raw / "playlists" / "PL.json",
        [
            {"id": "v0", "title": "[Private video]", "duration": None},
            {"id": "v1", "title": "[deleted video]"},
            {"id": "v2", "title": "T2", "availability": "needs_auth"},
        ],
        "PL",
    )
    assert youtube.import_raw(conn, raw) == 19
    row = conn.execute("SELECT title, duration_s, missing FROM videos WHERE video_id='v0'")
    assert tuple(row.fetchone()) == ("T0", 600, 1)  # real data kept, not counted present
    pls = conn.execute("SELECT playlists FROM videos WHERE video_id IN ('v1', 'v2')")
    assert [json.loads(r[0]) for r in pls] == [[], []]


def test_import_keeps_known_duration(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    raw = tmp_path / "raw"
    _write(raw / "tab_videos.json", [{"id": "v1", "title": "T", "duration": 600}])
    youtube.import_raw(conn, raw)
    _write(raw / "tab_videos.json", [{"id": "v1", "title": "T"}])
    youtube.import_raw(conn, raw)
    assert conn.execute("SELECT duration_s FROM videos").fetchone()[0] == 600


def test_import_dedupes_playlist_titles(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    raw = tmp_path / "raw"
    _write(raw / "playlists" / "a.json", [{"id": "v1", "title": "T"}], "DRŽAVNI POSAO 3")
    _write(raw / "playlists" / "b.json", [{"id": "v1", "title": "T"}], "DRŽAVNI POSAO 3")
    _write(raw / "playlists" / "c.json", [{"id": "v1", "title": "T"}], "Best of")
    youtube.import_raw(conn, raw)
    pls = json.loads(conn.execute("SELECT playlists FROM videos").fetchone()[0])
    assert pls == ["DRŽAVNI POSAO 3", "Best of"]


def test_import_refuses_mass_missing_unless_forced(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    raw = tmp_path / "raw"
    ids = [f"v{i}" for i in range(20)]
    _write(raw / "tab_videos.json", [{"id": v, "title": v} for v in ids])
    youtube.import_raw(conn, raw)

    # 2 of 20 newly missing (10%) is fine.
    _write(raw / "tab_videos.json", [{"id": v, "title": v} for v in ids[2:]])
    youtube.import_raw(conn, raw)
    # 3 more of the 18 present (17%) is refused and nothing changes.
    _write(raw / "tab_videos.json", [{"id": v, "title": "new"} for v in ids[5:]])
    with pytest.raises(youtube.TooManyMissing) as exc:
        youtube.import_raw(conn, raw)
    assert (exc.value.newly_missing, exc.value.present_before) == (3, 18)
    assert conn.execute("SELECT count(*) FROM videos WHERE missing=1").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM videos WHERE title='new'").fetchone()[0] == 0

    youtube.import_raw(conn, raw, force=True)
    assert conn.execute("SELECT count(*) FROM videos WHERE missing=1").fetchone()[0] == 5
