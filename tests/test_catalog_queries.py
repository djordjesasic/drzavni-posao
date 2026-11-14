import sqlite3
from pathlib import Path

import pytest

from dp.catalog.characters import load_seed, parse_seed
from dp.db import connect, migrate
from dp.search import catalog

SEED = Path(__file__).parent.parent / "seed" / "characters.yaml"


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "t.sqlite")
    migrate(c)
    load_seed(c, parse_seed(SEED))
    for vid, eid, kind, season, title, date, dur in [
        ("a", 1, "episode", 1, "Biti slobodan", "2012-09-26", 3600),
        ("b", 2, "episode", 1, "Košarka", "2012-10-23", 1800),
        ("s", 10001, "special", 1, "Novogodišnji specijal", "2012-12-31", 1800),
    ]:
        c.execute("INSERT INTO videos (video_id, title, raw) VALUES (?, ?, '{}')", (vid, title))
        c.execute(
            """INSERT INTO episodes (id, video_id, season, title, air_date, duration_s, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (eid, vid, season, title, date, dur, kind),
        )
    c.commit()
    return c


def test_limit_zero_returns_nothing(conn: sqlite3.Connection) -> None:
    assert catalog.search_titles(conn, "", limit=0) == []
    assert catalog.search_titles(conn, "", limit=-5) == []
    assert len(catalog.search_titles(conn, "", limit=1)) == 1


def test_like_wildcards_are_literal(conn: sqlite3.Connection) -> None:
    assert catalog.find_characters(conn, "_") == []
    assert catalog.find_characters(conn, "%") == []


def test_find_characters_returns_aliases(conn: sqlite3.Connection) -> None:
    (c,) = catalog.find_characters(conn, "stevica")
    assert c["id"] == "stevica_cvarkov"
    assert set(c["aliases"]) == {"Stevica Čvarkov", "Stevica"}
    assert len(catalog.find_characters(conn, "")) == len(parse_seed(SEED))


def test_summary_separates_specials(conn: sqlite3.Connection) -> None:
    s = catalog.catalog_summary(conn)
    assert s["episodes"] == {"count": 2, "hours": 1.5}
    assert s["specials"] == {"count": 1, "hours": 0.5}
    assert s["episode_numbers"] == [1, 2]
    assert s["special_ids_from"] == 10001


def test_retired_rows_are_hidden(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE episodes SET retired=1, retired_video_id=video_id, video_id=NULL WHERE id=2"
    )
    conn.commit()
    assert [e["id"] for e in catalog.search_titles(conn, "")] == [1, 10001]
    assert catalog.search_titles(conn, "košarka") == []
    assert catalog.get_episode(conn, 2) is None
    assert catalog.get_episode(conn, 1) is not None
    s = catalog.catalog_summary(conn)
    assert s["episodes"] == {"count": 1, "hours": 1.0}
    assert s["episode_numbers"] == [1, 1]
