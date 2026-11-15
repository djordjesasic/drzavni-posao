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
from dp.transcribe.speakers import SURE_SCORE

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
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data, video_id) "
        "VALUES (254, 'sonnet', ?, 'done', ?, 'v254')",
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
    assert info == {
        "windows": {"transcript": 2, "summary": 1, "story": 1},
        "description": True,
        "embedded": 0,
        "kept": 4,
    }
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
    d = {
        "summary": "x",
        "topics": [None, "porez"],
        "stories": [{"about": [None], "summary": "s", "start": None}],
    }
    wins = build.description_windows("T", d)
    assert "Teme: porez" in wins[0]["text"] and wins[1]["start_ms"] is None


def test_description_tolerates_wrong_types() -> None:
    d = {
        "summary": ["not", "text"],
        "topics": "Torbica",
        "places": {"a": 1},
        "opening_scene": "Dragan stize",
        "characters": ["torbica", {"name": "Cvarkov"}, {"id": 5}],
        "quotes": ["x", {"text": "Ovčija glava"}],
        "stories": ["priča", {"teller": "cvarkov", "about": "torbica", "start": "1:40"}],
        "imitations": "nista",
    }
    wins = build.description_windows("T", d)
    assert "Teme: Torbica" in wins[0]["text"] and "Likovi: Cvarkov" in wins[0]["text"]
    assert "Replike: Ovčija glava" in wins[0]["text"] and "Mesta" not in wins[0]["text"]
    assert [w["kind"] for w in wins] == ["summary", "story"]
    assert "priča o torbica" in wins[1]["text"] and wins[1]["start_ms"] == 100_000


def test_index_ignores_description_of_another_video(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    conn.execute("UPDATE enrichment SET video_id='v254'")
    assert build.do_index(s, conn, 254, fake_embed)["description"] is True
    conn.execute("UPDATE enrichment SET video_id='stari'")
    assert build.do_index(s, conn, 254, fake_embed)["description"] is False
    conn.execute("UPDATE enrichment SET video_id=NULL")  # NULL is not a match
    assert build.do_index(s, conn, 254, fake_embed)["description"] is False
    # A stale description of the current video is still indexed until it is redone.
    conn.execute("UPDATE enrichment SET video_id='v254', status='stale'")
    assert build.do_index(s, conn, 254, fake_embed)["description"] is True
    conn.execute("UPDATE enrichment SET status='failed'")
    assert build.do_index(s, conn, 254, fake_embed)["description"] is False


def test_quotes_with_a_time_are_their_own_windows() -> None:
    d = {"summary": "x", "quotes": [{"text": "Mama da dođe.", "start": "9:42"}, {"text": "Bez"}]}
    quotes = [w for w in build.description_windows("T", d) if w["kind"] == "quote"]
    assert quotes == [
        {"kind": "quote", "start_ms": 582_000, "end_ms": None, "text": "Mama da dođe."}
    ]


def test_description_skips_empty_quotes() -> None:
    d = {"summary": "x", "quotes": [{"text": None}, "plain string", {"text": "Mljac njam."}]}
    assert "Replike: Mljac njam." in build.description_windows("T", d)[0]["text"]
    d["quotes"] = [{"text": None}, {"text": ""}]
    assert "Replike" not in build.description_windows("T", d)[0]["text"]


def test_description_skips_empty_stories_and_has_no_none_text() -> None:
    d = {
        "summary": "x",
        "stories": [
            {"teller": "cvarkov", "about": ["torbica"]},
            {"teller": "boskic"},
            {"teller": "t", "summary": "beli prah"},
        ],
        "imitations": [{"who": "cvarkov", "imitates": "Zorana"}, {"who": "x"}],
    }
    wins = build.description_windows("T", d)
    assert [w["text"] for w in wins[1:]] == [
        "cvarkov priča o torbica",
        "t priča: beli prah",
        "cvarkov imitira Zorana",
    ]


def test_reindex_embeds_only_new_windows(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    calls: list[int] = []

    def counting(texts: list[str]) -> np.ndarray:
        calls.append(len(texts))
        return fake_embed(texts)

    first = build.do_index(s, conn, 254, counting)
    assert (first["embedded"], first["kept"]) == (4, 0) and calls == [4]
    ids = {r[0] for r in conn.execute("SELECT id FROM windows WHERE kind='transcript'")}
    assert build.do_index(s, conn, 254, counting)["embedded"] == 0 and calls == [4]
    # New description: transcript windows are kept, the summary and story are replaced.
    data = {"summary": "Nesto drugo, laptop.", "stories": []}
    conn.execute("UPDATE enrichment SET data=?", (json.dumps(data),))
    info = build.do_index(s, conn, 254, counting)
    assert (info["embedded"], info["kept"]) == (1, 2) and calls == [4, 1]
    assert ids == {r[0] for r in conn.execute("SELECT id FROM windows WHERE kind='transcript'")}
    n_win = conn.execute("SELECT COUNT(*) FROM windows WHERE episode_id=254").fetchone()[0]
    n_fts = conn.execute(
        "SELECT COUNT(*) FROM windows_fts WHERE rowid IN "
        "(SELECT id FROM windows WHERE episode_id=254)"
    ).fetchone()[0]
    assert (
        n_win == n_fts == 3 and conn.execute("SELECT COUNT(*) FROM windows_fts").fetchone()[0] == 3
    )


def test_rebuild_embeds_everything_again(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    calls: list[int] = []

    def counting(texts: list[str]) -> np.ndarray:
        calls.append(len(texts))
        return fake_embed(texts)

    build.do_index(s, conn, 254, counting)
    build.do_index(s, conn, 612, counting)
    info = build.do_index(s, conn, 254, counting, rebuild=True)
    assert (info["embedded"], info["kept"]) == (4, 0) and calls == [4, 2, 4]
    assert conn.execute("SELECT COUNT(*) FROM windows WHERE episode_id=254").fetchone()[0] == 4
    # FTS rows follow; other episodes are untouched.
    assert conn.execute("SELECT COUNT(*) FROM windows_fts").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM windows WHERE episode_id=612").fetchone()[0] == 2


def test_delete_windows_removes_fts_rows(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    build.do_index(s, conn, 254, fake_embed)
    build.do_index(s, conn, 612, fake_embed)
    build.delete_windows(conn, 254)
    assert conn.execute("SELECT COUNT(*) FROM windows WHERE episode_id=254").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM windows_fts").fetchone()[0] == 2


def test_eval_ignores_questions_of_retired_episodes(tmp_path: Path) -> None:
    s, conn = _db(tmp_path)
    for eid in (254, 612, 487):
        build.do_index(s, conn, eid, fake_embed)
    conn.execute(
        "UPDATE episodes SET retired=1, retired_video_id=video_id, video_id=NULL WHERE id=612"
    )
    qs = tmp_path / "q.yaml"
    qs.write_text("pitanja:\n  - {pitanje: 'grejao sendvic na laptopu', epizoda: 612}\n")
    res = eval_run.evaluate(conn, qs, fake_embed)
    assert [q["in_index"] for q in res["questions"]] == [False]


def test_transcript_window_uses_sure_score(tmp_path: Path) -> None:
    _, conn = _db(tmp_path)
    segs = conn.execute("SELECT * FROM segments WHERE episode_id=254 ORDER BY idx").fetchall()
    rows = [dict(g) | {"actor_score": SURE_SCORE - 0.01} for g in segs]
    assert build.transcript_windows(rows, {"torbica": "D"}, 75, 40)[0]["text"].startswith("Ovč")
    rows = [dict(g) | {"actor_score": SURE_SCORE} for g in segs]
    assert build.transcript_windows(rows, {"torbica": "D"}, 75, 40)[0]["text"].startswith("D:")


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
