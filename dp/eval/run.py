"""Run the owner's test questions through search and count hits in the top 1/5/20."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from dp.index.search import ACTIVE_EPISODE, Vectors, search


def load_questions(path: Path) -> list[dict[str, Any]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for q in raw.get("pitanja") or []:
        ep = q.get("epizoda")
        out.append(
            {
                "query": str(q["pitanje"]),
                "episode": int(ep) if str(ep or "").strip().isdigit() else None,
                "note": q.get("napomena") or "",
            }
        )
    return out


def evaluate(
    conn: sqlite3.Connection, path: Path, embed: Callable[[list[str]], np.ndarray]
) -> dict[str, Any]:
    # Same rule as search: retired episodes and episodes without a video are not findable.
    indexed = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT w.episode_id FROM windows w JOIN episodes e ON e.id = w.episode_id "
            f"WHERE {ACTIVE_EPISODE}"
        )
    }
    vectors = Vectors(conn)
    rows = []
    for q in load_questions(path):
        res = search(conn, q["query"], embed, vectors, limit=20)
        ranked = [r["episode_id"] for r in res]
        expected = q["episode"]
        in_index = expected in indexed
        rank = ranked.index(expected) + 1 if expected in ranked else None
        top = res[0] if res else None
        rows.append(
            {
                "query": q["query"],
                "expected": expected,
                "in_index": in_index,
                "rank": rank,
                "top": [r["episode_id"] for r in res[:5]],
                "top_score": top["score"] if top else None,
                "time_s": next((r["start_s"] for r in res if r["episode_id"] == expected), None),
            }
        )
    scored = [r for r in rows if r["in_index"]]
    n = len(scored)

    def hit(k: int) -> str:
        h = sum(1 for r in scored if r["rank"] and r["rank"] <= k)
        return f"{h}/{n}"

    return {"top1": hit(1), "top5": hit(5), "top20": hit(20), "questions": rows}
