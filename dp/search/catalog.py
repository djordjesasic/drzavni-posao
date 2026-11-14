"""Catalog queries shared by the HTTP API and MCP tools (phase 0: no transcripts yet)."""

import sqlite3
from typing import Any

from dp.catalog.classify import SPECIAL_ID_BASE
from dp.text import fold

# Input bounds shared by the HTTP API and MCP tools (larger values overflow or are nonsense).
EPISODE_ID_MAX = 99999
SEASON_MIN, SEASON_MAX = 1, 99
YEAR_MIN, YEAR_MAX = 2000, 2100


def youtube_url(video_id: str, start_s: int = 0) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" + (f"&t={start_s}s" if start_s else "")


def _episode(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["youtube_url"] = youtube_url(d["video_id"])
    return d


def get_episode(conn: sqlite3.Connection, episode_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM episodes WHERE id=? AND retired=0", (episode_id,)).fetchone()
    return _episode(row) if row else None


def search_titles(
    conn: sqlite3.Connection,
    query: str = "",
    season: int | None = None,
    year: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Episodes whose title contains every word of the query (diacritics-insensitive)."""
    if limit <= 0:
        return []
    words = fold(query).split()
    sql = "SELECT * FROM episodes WHERE retired=0"
    params: list[Any] = []
    if season is not None:
        sql += " AND season=?"
        params.append(season)
    if year is not None:
        sql += " AND substr(air_date, 1, 4)=?"
        params.append(str(year))
    out = []
    for row in conn.execute(sql + " ORDER BY id", params):
        title = fold(row["title"])
        if all(w in title for w in words):
            out.append(_episode(row))
            if len(out) >= limit:
                break
    return out


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_characters(conn: sqlite3.Connection, name: str) -> list[dict[str, Any]]:
    """Characters whose name or alias contains the query (diacritics-insensitive).

    An empty query returns the whole registry.
    """
    pattern = f"%{_like_escape(fold(name))}%"
    rows = conn.execute(
        """SELECT * FROM characters WHERE id IN (
             SELECT character_id FROM aliases WHERE alias_fold LIKE ? ESCAPE '\\')
           ORDER BY id""",
        (pattern,),
    ).fetchall()
    out = {r["id"]: {**dict(r), "aliases": []} for r in rows}
    if out:
        marks = ",".join("?" * len(out))
        for a in conn.execute(
            f"SELECT character_id, alias FROM aliases WHERE character_id IN ({marks})",
            list(out),
        ):
            out[a["character_id"]]["aliases"].append(a["alias"])
    return list(out.values())


def catalog_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts and hours per kind (regular episodes and specials) plus overall range."""
    by_kind = {
        r["kind"]: {"count": r["n"], "hours": round((r["s"] or 0) / 3600, 1)}
        for r in conn.execute(
            "SELECT kind, count(*) n, sum(duration_s) s FROM episodes WHERE retired=0 GROUP BY kind"
        )
    }
    row = conn.execute(
        """SELECT min(season) s0, max(season) s1, min(air_date) a, max(air_date) b,
                  min(id) e0, max(id) e1
           FROM episodes WHERE kind='episode' AND retired=0"""
    ).fetchone()
    return {
        "episodes": by_kind.get("episode", {"count": 0, "hours": 0.0}),
        "specials": by_kind.get("special", {"count": 0, "hours": 0.0}),
        "episode_numbers": [row["e0"], row["e1"]],
        "seasons": [row["s0"], row["s1"]],
        "first_air_date": row["a"],
        "last_air_date": row["b"],
        "special_ids_from": SPECIAL_ID_BASE + 1,
    }
