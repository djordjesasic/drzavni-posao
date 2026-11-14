"""Hybrid search: FTS5 (words) + vectors (meaning), fused per episode with RRF."""

import re
import sqlite3
from collections.abc import Callable
from typing import Any

import numpy as np

from dp.text import fold

RRF_K = 60
PER_LIST = 50  # windows taken from each list before fusion
_WORD = re.compile(r"[a-z0-9]{3,}")
# Very common words that only add noise to an OR query.
_STOP = {
    "kad", "kada", "koji", "koja", "koje", "sta", "sto", "kako", "gde", "epizoda", "epizodi",
    "epizode", "jer", "ali", "prica", "kao", "sam", "nije", "taj", "ona", "oni", "ovo", "ono",
    "bio", "bila", "ima", "neki", "neka", "prvi", "put",
}  # fmt: skip


def fts_query(text: str) -> str:
    words = [w for w in _WORD.findall(fold(text)) if w not in _STOP]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


def _fts(conn: sqlite3.Connection, query: str) -> list[int]:
    q = fts_query(query)
    if not q:
        return []
    rows = conn.execute(
        "SELECT rowid FROM windows_fts WHERE windows_fts MATCH ? ORDER BY bm25(windows_fts) "
        "LIMIT ?",
        (q, PER_LIST),
    )
    return [r[0] for r in rows]


class Vectors:
    """All window vectors in memory (tens of thousands x 1024 floats: fine)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT id, embedding FROM windows ORDER BY id").fetchall()
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
    lists = {"words": _fts(conn, query), "meaning": vectors.top(embed([query])[0])}
    score: dict[int, float] = {}
    # episode -> (rank, no time, window id): the window shown is the best ranked one,
    # preferring one with a time (a summary hit links to the start of the episode).
    best: dict[int, tuple[int, bool, int]] = {}
    meta = {}
    wanted = {w for ids in lists.values() for w in ids}
    if wanted:
        marks = ",".join("?" * len(wanted))
        for r in conn.execute(
            f"SELECT id, episode_id, kind, start_ms, text FROM windows WHERE id IN ({marks})",
            list(wanted),
        ):
            meta[r["id"]] = r
    for ids in lists.values():
        seen: set[int] = set()
        rank = 0
        for wid in ids:
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
