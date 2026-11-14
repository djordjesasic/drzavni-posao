import csv
from pathlib import Path

from dp.catalog.export import HEADER, export_episodes
from dp.db import connect, migrate


def test_export_episodes_csv(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.sqlite")
    migrate(conn)
    rows = [
        ("s", 10001, "special", 1, "Novogodišnji specijal", "2012-12-31", 1800, 0),
        ("b", 2, "episode", 1, "Košarka", "2012-10-23", 240, 0),
        ("a", 1, "episode", 1, "Biti slobodan", "2012-09-26", 275, 0),
        ("r", 3, "episode", None, "Povučena", None, None, 1),
    ]
    for vid, eid, kind, season, title, date, dur, retired in rows:
        conn.execute("INSERT INTO videos (video_id, title, raw) VALUES (?, ?, '{}')", (vid, title))
        conn.execute(
            """INSERT INTO episodes
                 (id, video_id, season, title, air_date, duration_s, kind, retired)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (eid, vid, season, title, date, dur, kind, retired),
        )
    conn.commit()

    out = tmp_path / "exports" / "epizode.csv"
    assert export_episodes(conn, out) == 3
    with out.open(encoding="utf-8-sig", newline="") as f:
        got = list(csv.reader(f))
    assert got[0] == HEADER
    assert [r[0] for r in got[1:]] == ["1", "2", "10001"]  # ordered, retired excluded
    assert got[2] == [
        "2",
        "1",
        "Košarka",
        "2012-10-23",
        "4:00",
        "episode",
        "https://www.youtube.com/watch?v=b",
    ]
    assert not out.with_suffix(".tmp").exists()
