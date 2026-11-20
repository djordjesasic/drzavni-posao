import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_index import _db, fake_embed

from dp.index import build, search

QUERY = "cvarkov greje sendvic u laptopu"


def _index_all(tmp_path: Path) -> tuple[Any, sqlite3.Connection]:
    s, conn = _db(tmp_path)
    for eid in (254, 612, 487):
        build.do_index(s, conn, eid, fake_embed)
    return s, conn


def test_search_skips_windows_deleted_after_vectors_loaded(tmp_path: Path) -> None:
    _, conn = _index_all(tmp_path)
    vectors = search.Vectors(conn)  # loaded before the worker re-indexes episode 612
    old = [r[0] for r in conn.execute("SELECT id FROM windows WHERE episode_id=612")]
    conn.executemany("DELETE FROM windows_fts WHERE rowid=?", [(i,) for i in old])
    conn.execute("DELETE FROM windows WHERE episode_id=612")
    conn.commit()

    hits = search.search(conn, QUERY, fake_embed, vectors)
    assert 612 not in [h["episode_id"] for h in hits]
    assert hits  # the other episodes still come back


def test_search_skips_retired_episode(tmp_path: Path) -> None:
    _, conn = _index_all(tmp_path)
    hits = search.search(conn, QUERY, fake_embed)
    assert hits[0]["episode_id"] == 612

    conn.execute("UPDATE episodes SET retired=1 WHERE id=612")
    conn.commit()
    hits = search.search(conn, QUERY, fake_embed)
    assert 612 not in [h["episode_id"] for h in hits]


def test_retired_windows_are_filtered_at_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, conn = _index_all(tmp_path)
    monkeypatch.setattr(search, "PER_LIST", 1)
    assert search._fts(conn, QUERY) and search.Vectors(conn).top(fake_embed([QUERY])[0], 1)
    top_fts = search._fts(conn, QUERY)[0]
    retired = conn.execute("SELECT episode_id FROM windows WHERE id=?", (top_fts,)).fetchone()[0]
    conn.execute("UPDATE episodes SET retired=1 WHERE id=?", (retired,))
    conn.commit()
    # The single candidate slot goes to an active episode, not to the retired one.
    (wid,) = search._fts(conn, QUERY)
    assert conn.execute("SELECT episode_id FROM windows WHERE id=?", (wid,)).fetchone()[0] != (
        retired
    )
    vectors = search.Vectors(conn)
    kept = {
        r[0]
        for r in conn.execute("SELECT id, kind FROM windows WHERE episode_id != ?", (retired,))
        if r[1] not in search.NO_VECTOR_KINDS
    }
    assert set(vectors.ids.tolist()) == kept
    # Unlinked (video_id NULL) counts as inactive too.
    conn.execute("UPDATE episodes SET retired=0, video_id=NULL WHERE id=?", (retired,))
    conn.commit()
    assert set(search.Vectors(conn).ids.tolist()) == kept
