import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest

from dp import cli
from dp.catalog import youtube
from dp.config import get_settings
from dp.db import connect, migrate


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = tmp_path / "t.sqlite"
    monkeypatch.setenv("DP_DB", str(path))
    get_settings.cache_clear()
    conn = connect(path)
    migrate(conn)
    title = "DRŽAVNI POSAO [HQ] - Ep.{n}: T{n} (0{n}.10.2014.)"
    for vid, t, pls in [
        ("v1", title.format(n=1), ["DRŽAVNI POSAO 3"]),
        ("v2", title.format(n=2), []),  # retired below; season would be unknown
        ("s1", "DRŽAVNI POSAO [HQ] - Novogodišnji specijal (31.12.2014.)", ["DRŽAVNI POSAO 3"]),
        ("s2", "DRŽAVNI POSAO [HQ] - Stari specijal (31.12.2013.)", []),
    ]:
        conn.execute(
            """INSERT INTO videos (video_id, title, duration_s, playlists, raw)
               VALUES (?, ?, 600, ?, '{}')""",
            (vid, t, f'["{pls[0]}"]' if pls else "[]"),
        )
    conn.commit()
    cli.cmd_catalog_build(argparse.Namespace())
    conn.execute("UPDATE videos SET missing=1 WHERE video_id IN ('v2', 's2')")
    conn.commit()
    conn.close()
    yield path
    get_settings.cache_clear()


def test_build_and_stats_ignore_retired_rows(db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    cli.cmd_catalog_build(argparse.Namespace())
    out = capsys.readouterr().out
    assert "videos missing from the channel: 2" in out
    assert "no season: []" in out

    cli.cmd_catalog_stats(argparse.Namespace())
    out = capsys.readouterr().out
    assert "episodes with duration: 1, total 0.2 h" in out
    assert "Novogodišnji specijal" in out
    assert "Stari specijal" not in out


def test_catalog_import_refuses_without_force(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_import(conn: object, raw_dir: Path, force: bool = False) -> int:
        if not force:
            raise youtube.TooManyMissing(5, 10)
        return 7

    monkeypatch.setattr(youtube, "import_raw", fake_import)
    with pytest.raises(SystemExit, match="catalog import refused"):
        cli.cmd_catalog_import(argparse.Namespace(force=False))
    cli.cmd_catalog_import(argparse.Namespace(force=True))
    assert "imported 7 videos, 2 missing from the channel" in capsys.readouterr().out
