"""Index step: text windows of the transcript and pieces of the episode description,
each stored with a vector (semantic search) and in FTS5 (word search)."""

import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import numpy as np

from dp.config import Settings
from dp.enrich.prompt import PROMPT_VERSION
from dp.index import embed as embed_client
from dp.text import fold

Embed = Callable[[list[str]], np.ndarray]
_MMSS = re.compile(r"^\s*(\d+):(\d{1,2})")


def _ms(mmss: Any) -> int | None:
    m = _MMSS.match(str(mmss or ""))
    return (int(m[1]) * 60 + int(m[2])) * 1000 if m else None


def transcript_windows(
    segs: list[sqlite3.Row], names: dict[str, str], window_s: float, hop_s: float
) -> list[dict[str, Any]]:
    """Overlapping windows of window_s seconds every hop_s seconds. A line carries
    the speaker's name when the voice step is sure of it."""
    if not segs:
        return []
    end = segs[-1]["end_ms"]
    out = []
    t = 0
    prev: list[int] = []
    while t < end:
        inside = [g for g in segs if t <= g["start_ms"] < t + window_s * 1000]
        ids = [g["start_ms"] for g in inside]
        if inside and not set(ids) <= set(prev):  # skip a window adding nothing new
            prev = ids
            lines = []
            for g in inside:
                who = names.get(g["actor"] or "") if (g["actor_score"] or 0) >= 0.35 else None
                lines.append(f"{who}: {g['text']}" if who else g["text"])
            out.append(
                {
                    "kind": "transcript",
                    "start_ms": inside[0]["start_ms"],
                    "end_ms": inside[-1]["end_ms"],
                    "text": "\n".join(lines),
                }
            )
        t += int(hop_s * 1000)
    return out


def _j(items: Any) -> str:
    """Comma-joined list from model output, skipping nulls and non-strings."""
    return ", ".join(str(x) for x in items or [] if x not in (None, ""))


def description_windows(title: str, d: dict[str, Any]) -> list[dict[str, Any]]:
    """The whole description as one piece, plus one piece per story and imitation
    (they carry a time, so a hit links to the right minute)."""
    chars = _j(c.get("name") or c.get("id") for c in d.get("characters") or [])
    quotes = " | ".join(str(q.get("text") or "") for q in d.get("quotes") or [])
    main = "\n".join(
        x
        for x in [
            f"Epizoda „{title}“.",
            d.get("summary") or "",
            "Teme: " + _j(d.get("topics")),
            f"Aktuelno: {d['current_event']}" if d.get("current_event") else "",
            f"Početak: {(d.get('opening_scene') or {}).get('description') or ''}",
            f"Likovi: {chars}",
            "Javne ličnosti: " + _j(d.get("public_figures")),
            "Mesta: " + _j(d.get("places")),
            "Predmeti: " + _j(d.get("objects")),
            f"Replike: {quotes}",
        ]
        if x and not x.endswith(": ")
    )
    out = [{"kind": "summary", "start_ms": None, "end_ms": None, "text": main}]
    for s in d.get("stories") or []:
        about = _j(s.get("about"))
        out.append(
            {
                "kind": "story",
                "start_ms": _ms(s.get("start")),
                "end_ms": None,
                "text": f"{s.get('teller')} priča o {about}: {s.get('summary')}",
            }
        )
    for i in d.get("imitations") or []:
        out.append(
            {
                "kind": "imitation",
                "start_ms": _ms(i.get("start")),
                "end_ms": None,
                "text": f"{i.get('who')} imitira {i.get('imitates')}, {i.get('style')}",
            }
        )
    return out


def do_index(
    s: Settings, conn: sqlite3.Connection, eid: int, embed: Embed | None = None
) -> dict[str, Any]:
    embed = embed or (lambda texts: embed_client.embed(s.embed_url, texts))
    ep = conn.execute("SELECT title FROM episodes WHERE id=?", (eid,)).fetchone()
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM characters")}
    segs = conn.execute(
        "SELECT start_ms, end_ms, text, actor, actor_score FROM segments "
        "WHERE episode_id=? ORDER BY idx",
        (eid,),
    ).fetchall()
    wins = transcript_windows(segs, names, s.index_window_s, s.index_hop_s)
    row = conn.execute(
        "SELECT data FROM enrichment WHERE episode_id=? AND model=? AND prompt_version=? "
        "AND status='done'",
        (eid, s.index_enrich_model, PROMPT_VERSION),
    ).fetchone()
    if row:
        wins += description_windows(ep["title"], json.loads(row["data"]))
    vecs = embed([w["text"] for w in wins]) if wins else np.zeros((0, 0), np.float32)
    with conn:
        old = [r[0] for r in conn.execute("SELECT id FROM windows WHERE episode_id=?", (eid,))]
        conn.executemany("DELETE FROM windows_fts WHERE rowid=?", [(i,) for i in old])
        conn.execute("DELETE FROM windows WHERE episode_id=?", (eid,))
        for w, v in zip(wins, vecs, strict=True):
            cur = conn.execute(
                "INSERT INTO windows (episode_id, kind, start_ms, end_ms, text, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (eid, w["kind"], w["start_ms"], w["end_ms"], w["text"], v.tobytes()),
            )
            conn.execute(
                "INSERT INTO windows_fts (rowid, text_fold) VALUES (?, ?)",
                (cur.lastrowid, fold(w["text"])),
            )
    kinds: dict[str, int] = {}
    for w in wins:
        kinds[w["kind"]] = kinds.get(w["kind"], 0) + 1
    return {"windows": kinds, "description": bool(row)}
