"""Export the episode catalog to CSV for browsing on the Mac."""

import csv
import sqlite3
from pathlib import Path

from dp.search.catalog import youtube_url

HEADER = ["broj", "sezona", "naslov", "datum", "trajanje", "vrsta", "youtube"]


def _duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


def export_episodes(conn: sqlite3.Connection, path: Path) -> int:
    """Write active episodes and specials ordered by id; return the row count.

    UTF-8 with BOM so spreadsheet apps show č/ć/š/ž/đ correctly.
    """
    rows = conn.execute(
        """SELECT id, season, title, air_date, duration_s, kind, video_id
           FROM episodes WHERE retired = 0 ORDER BY id"""
    ).fetchall()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(
                [
                    r["id"],
                    r["season"] if r["season"] is not None else "",
                    r["title"],
                    r["air_date"] or "",
                    _duration(r["duration_s"]),
                    r["kind"],
                    youtube_url(r["video_id"]),
                ]
            )
    tmp.replace(path)
    return len(rows)
