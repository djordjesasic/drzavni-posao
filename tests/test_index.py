import json
from pathlib import Path

import numpy as np
from test_enrich import SEED, _settings  # tests/ is on sys.path (pytest rootdir mode)

from dp.catalog.characters import load_seed, parse_seed
from dp.db import connect, migrate
from dp.enrich.prompt import PROMPT_VERSION
from dp.eval import run as eval_run
from dp.index import build, search
from dp.text import fold

VOCAB = ["ovcij", "glav", "drog", "megafon", "laptop", "sendvic", "dreger", "prozor"]


def fake_embed(texts: list[str]) -> np.ndarray:
    """Bag of word stems: enough for 'meaning' to differ from exact words."""
    m = np.array([[fold(t).count(w) + 0.01 for w in VOCAB] for t in texts], dtype=np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def _db(tmp_path: Path):  # type: ignore[no-untyped-def]
    s = _settings(tmp_path)
    conn = connect(s.db)
    migrate(conn)
    load_seed(conn, parse_seed(SEED))
    for eid, title, lines in [
        (254, "Narkotici", ["Ovčija glava je za mene!", "Šta si popio, beli prah?"]),
        (612, "Oguglavanje", ["Grejem sendvič na laptopu.", "Mljac njam."]),
        (487, "Dreger", ["Ulazimo kroz prozor.", "Na ulazu je dreger."]),
    ]:
        conn.execute(
            "INSERT INTO videos (video_id, title, raw) VALUES (?, 't', '{}')", (f"v{eid}",)
        )
        conn.execute(
            "INSERT INTO episodes (id, video_id, title) VALUES (?, ?, ?)", (eid, f"v{eid}", title)
        )
        conn.executemany(
            "INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin, "
            "text_fold, actor, actor_score) VALUES (?, ?, ?, ?, ?, ?, ?, 'torbica', 0.6)",
            [(eid, i, 100_000 * i, 100_000 * i + 3000, t, t, fold(t)) for i, t in enumerate(lines)],
        )
    data = {"summary": "Dragan glumi da je drogiran da bi sam pojeo ovčiju glavu.",
            "stories": [{"teller": "cvarkov", "about": ["torbica"], "summary": "beli prah",
                         "start": "1:40"}]}  # fmt: skip
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data) "
        "VALUES (254, 'sonnet', ?, 'done', ?)",
        (PROMPT_VERSION, json.dumps(data)),
    )
    conn.commit()
    return s, conn


def test_transcript_windows_overlap_and_names(tmp_path: Path) -> None:
    _, conn = _db(tmp_path)
    segs = conn.execute("SELECT * FROM segments WHERE episode_id=254 ORDER BY idx").fetchall()
    wins = build.transcript_windows(segs, {"torbica": "Dragan Torbica"}, 75, 40)
    # Segments at 0 s and 100 s: windows from 0, 40 (empty: skipped), 80 (second line).
    assert [w["start_ms"] for w in wins] == [0, 100_000]
    assert wins[0]["text"] == "Dragan Torbica: Ovčija glava je za mene!"


def test_index_is_replaced_on_rerun_and_search_finds_episodes(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    for eid in (254, 612, 487):
        build.do_index(s, conn, eid, fake_embed)
    info = build.do_index(s, conn, 254, fake_embed)  # rerun replaces, no duplicates
    assert info == {"windows": {"transcript": 2, "summary": 1, "story": 1}, "description": True}
    n_win = conn.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
    n_fts = conn.execute("SELECT COUNT(*) FROM windows_fts").fetchone()[0]
    assert n_win == n_fts == 4 + 2 + 2

    hits = search.search(conn, "dragan se pravi drogiran zbog ovcije glave", fake_embed)
    assert hits[0]["episode_id"] == 254
    hits = search.search(conn, "cvarkov greje sendvic u laptopu", fake_embed)
    assert hits[0]["episode_id"] == 612 and hits[0]["kind"] == "transcript"
    hits = search.search(conn, "ulaze kroz prozor zbog dregera", fake_embed)
    assert hits[0]["episode_id"] == 487


def test_description_tolerates_nulls_in_lists() -> None:
    d = {"summary": "x", "topics": [None, "porez"], "stories": [{"about": [None], "start": None}]}
    wins = build.description_windows("T", d)
    assert "Teme: porez" in wins[0]["text"] and wins[1]["start_ms"] is None


def test_fts_query_folds_and_drops_short_and_stop_words() -> None:
    assert search.fts_query("Kad Čvarkov greje sendvič") == '"cvarkov" OR "greje" OR "sendvic"'
    assert search.fts_query("a i u") == ""


def test_eval_counts_only_indexed_questions(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    for eid in (254, 612, 487):
        build.do_index(s, conn, eid, fake_embed)
    qs = tmp_path / "q.yaml"
    qs.write_text(
        "pitanja:\n"
        "  - {pitanje: 'grejao sendvic na laptopu', epizoda: 612}\n"
        "  - {pitanje: 'ovcija glava', epizoda: 254}\n"
        "  - {pitanje: 'nesto van pilota', epizoda: 1500}\n"
        "  - {pitanje: 'bez broja', epizoda: ''}\n"
    )
    res = eval_run.evaluate(conn, qs, fake_embed)
    assert (res["top1"], res["top5"]) == ("2/2", "2/2")
    assert [q["in_index"] for q in res["questions"]] == [True, True, False, False]
