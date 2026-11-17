"""Hybrid search: FTS5 (words) + vectors (meaning), fused per episode with RRF."""

import logging
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import numpy as np

from dp.text import fold

log = logging.getLogger(__name__)

RRF_K = 60
# Windows of active episodes only, applied at the source so retired ones neither take
# candidate slots nor memory (the same active-episode rule as dp.db.active_video_id).
ACTIVE_EPISODE = "e.retired = 0 AND e.video_id IS NOT NULL"  # e = episodes
PER_LIST = 50  # windows taken from each list before fusion
_WORD = re.compile(r"[a-z0-9]{3,}")
# Very common words that only add noise to an OR query.
_STOP = {
    "kad", "kada", "koji", "koja", "koje", "sta", "sto", "kako", "gde", "epizoda", "epizodi",
    "epizode", "jer", "ali", "prica", "kao", "sam", "nije", "taj", "ona", "oni", "ovo", "ono",
    "bio", "bila", "ima", "neki", "neka", "prvi", "put",
}  # fmt: skip


def _stem(word: str) -> str:
    """Crude stem for Serbian case endings, as an FTS5 prefix: "glava" -> "glav"*, so
    glavu/glave/glavom match too. Short words stay exact (too many false prefixes)."""
    if len(word) >= 7:
        return f'"{word[:-2]}"*'
    if len(word) >= 5:
        return f'"{word[:-1]}"*'
    return f'"{word}"'


def fts_query(text: str) -> str:
    words = [w for w in _WORD.findall(fold(text)) if w not in _STOP]
    return " OR ".join(_stem(w) for w in dict.fromkeys(words))


def _fts(conn: sqlite3.Connection, query: str) -> list[int]:
    q = fts_query(query)
    if not q:
        return []
    rows = conn.execute(
        "SELECT windows_fts.rowid FROM windows_fts "
        "JOIN windows w ON w.id = windows_fts.rowid JOIN episodes e ON e.id = w.episode_id "
        f"WHERE windows_fts MATCH ? AND {ACTIVE_EPISODE} ORDER BY bm25(windows_fts) LIMIT ?",
        (q, PER_LIST),
    )
    return [r[0] for r in rows]


class Vectors:
    """All window vectors in memory (tens of thousands x 1024 floats: fine)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT w.id, w.embedding FROM windows w JOIN episodes e ON e.id = w.episode_id "
            f"WHERE {ACTIVE_EPISODE} ORDER BY w.id"
        ).fetchall()
        self.ids = np.array([r[0] for r in rows], dtype=np.int64)
        self.m = (
            np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
            if rows
            else np.zeros((0, 1), np.float32)
        )

    def top(self, q: np.ndarray, n: int = PER_LIST) -> list[int]:
        if not len(self.ids):
            return []
        sims = self.m @ q
        order = np.argsort(-sims)[:n]
        return [int(self.ids[i]) for i in order]


def search(
    conn: sqlite3.Connection,
    query: str,
    embed: Callable[[list[str]], np.ndarray],
    vectors: Vectors | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Episodes ranked by reciprocal rank fusion of the best window per list."""
    vectors = vectors or Vectors(conn)
    try:
        meaning = vectors.top(embed([query])[0])
    except Exception:  # embed service down: words alone still find quotes and names
        log.warning("embedding the query failed; searching by words only", exc_info=True)
        meaning = []
    lists = {"words": _fts(conn, query), "meaning": meaning}
    score: dict[int, float] = {}
    # episode -> (rank, no time, window id): the window shown is the best ranked one,
    # preferring one with a time (a summary hit links to the start of the episode).
    best: dict[int, tuple[int, bool, int]] = {}
    meta = {}
    wanted = {w for ids in lists.values() for w in ids}
    if wanted:
        marks = ",".join("?" * len(wanted))
        # The candidate ids come from three reads that are not atomic: a worker may
        # re-index an episode in between (or after Vectors was loaded). So an id can be
        # missing here, and an episode retired since then is dropped by the join
        # (the same active-episode rule as dp.db.active_video_id).
        for r in conn.execute(
            "SELECT w.id, w.episode_id, w.kind, w.start_ms, w.text FROM windows w "
            "JOIN episodes e ON e.id = w.episode_id "
            f"WHERE w.id IN ({marks}) AND {ACTIVE_EPISODE}",
            list(wanted),
        ):
            meta[r["id"]] = r
    for ids in lists.values():
        seen: set[int] = set()
        rank = 0
        for wid in ids:
            if wid not in meta:  # gone or not searchable: not counted in the ranks
                continue
            eid = meta[wid]["episode_id"]
            if eid in seen:  # only the best window of an episode counts in each list
                continue
            seen.add(eid)
            rank += 1
            score[eid] = score.get(eid, 0) + 1 / (RRF_K + rank)
            cand = (rank, meta[wid]["start_ms"] is None, wid)
            best[eid] = min(best.get(eid, cand), cand)
    out = []
    for eid in sorted(score, key=lambda e: -score[e])[:limit]:
        w = meta[best[eid][2]]
        out.append(
            {
                "episode_id": eid,
                "score": round(score[eid], 5),
                "start_s": (w["start_ms"] or 0) // 1000,
                "kind": w["kind"],
                "snippet": w["text"][:300],
            }
        )
    return out
