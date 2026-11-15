"""Index step: text windows of the transcript and pieces of the episode description,
each stored with a vector (semantic search) and in FTS5 (word search)."""

import json
import re
import sqlite3
from collections.abc import Callable
from typing import Any

import numpy as np

from dp.config import Settings
from dp.enrich.store import valid_enrichment
from dp.index import embed as embed_client
from dp.index.store import delete_windows
from dp.text import fold
from dp.transcribe.speakers import SURE_SCORE

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
                who = names.get(g["actor"] or "") if (g["actor_score"] or 0) >= SURE_SCORE else None
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
    """Comma-joined list from model output, skipping nulls and non-strings.
    A bare string counts as a one-item list (not joined character by character)."""
    if isinstance(items, str):
        items = [items]
    elif not isinstance(items, list | tuple):
        return ""
    return ", ".join(
        str(x) for x in items if x not in (None, "") and not isinstance(x, dict | list)
    )


def _objs(v: Any) -> list[dict[str, Any]]:
    """List of objects from model output: a lone object counts as one, non-objects are dropped."""
    if isinstance(v, dict):
        v = [v]
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _s(v: Any) -> str:
    """A scalar text field; anything else (lists, objects, null) is ignored."""
    return v if isinstance(v, str) else ""


def description_windows(title: str, d: dict[str, Any]) -> list[dict[str, Any]]:
    """The whole description as one piece, plus one piece per story and imitation
    (they carry a time, so a hit links to the right minute). Tolerant of wrong shapes:
    the model output is only shape-checked at the top level."""
    chars = _j([_s(c.get("name")) or _s(c.get("id")) for c in _objs(d.get("characters"))])
    quotes = " | ".join(t for q in _objs(d.get("quotes")) if (t := _s(q.get("text"))))
    opening = d.get("opening_scene")
    current = _s(d.get("current_event"))
    main = "\n".join(
        x
        for x in [
            f"Epizoda „{title}“.",
            _s(d.get("summary")),
            "Teme: " + _j(d.get("topics")),
            f"Aktuelno: {current}" if current else "",
            f"Početak: {_s(opening.get('description')) if isinstance(opening, dict) else ''}",
            f"Likovi: {chars}",
            "Javne ličnosti: " + _j(d.get("public_figures")),
            "Mesta: " + _j(d.get("places")),
            "Predmeti: " + _j(d.get("objects")),
            f"Replike: {quotes}",
        ]
        if x and not x.endswith(": ")
    )
    out = [{"kind": "summary", "start_ms": None, "end_ms": None, "text": main}]
    for s in _objs(d.get("stories")):
        about, summary = _j(s.get("about")), _s(s.get("summary"))
        if not (about or summary):
            continue
        teller = _s(s.get("teller"))
        text = f"{teller} priča" if teller else "Priča"
        text += (f" o {about}" if about else "") + (f": {summary}" if summary else "")
        out.append({"kind": "story", "start_ms": _ms(s.get("start")), "end_ms": None, "text": text})
    for i in _objs(d.get("imitations")):
        imitates, style = _s(i.get("imitates")), _s(i.get("style"))
        if not (imitates or style):
            continue
        who = _s(i.get("who"))
        text = f"{who} imitira" if who else "Imitacija"
        text += (f" {imitates}" if imitates else "") + (f", {style}" if style else "")
        out.append(
            {"kind": "imitation", "start_ms": _ms(i.get("start")), "end_ms": None, "text": text}
        )
    # Quotes are what people remember word for word; on their own they link to the minute
    # (inside the summary piece a hit would link to the start of the episode).
    for q in _objs(d.get("quotes")):
        text, start = _s(q.get("text")), _ms(q.get("start"))
        if text and start is not None:
            out.append({"kind": "quote", "start_ms": start, "end_ms": None, "text": text})
    return out


def do_index(
    s: Settings,
    conn: sqlite3.Connection,
    eid: int,
    embed: Embed | None = None,
    rebuild: bool = False,
) -> dict[str, Any]:
    """rebuild=True drops the episode's windows and embeds everything again (needed after
    the embedding model or fold() changed); otherwise unchanged windows are kept."""
    embed = embed or (lambda texts: embed_client.embed(s.embed_url, texts))
    ep = conn.execute("SELECT title, video_id FROM episodes WHERE id=?", (eid,)).fetchone()
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM characters")}
    segs = conn.execute(
        "SELECT start_ms, end_ms, text, actor, actor_score FROM segments "
        "WHERE episode_id=? ORDER BY idx",
        (eid,),
    ).fetchall()
    wins = transcript_windows(segs, names, s.index_window_s, s.index_hop_s)
    # A stale description (the transcript changed) is still used until it is made again.
    row = valid_enrichment(conn, eid, s.index_enrich_model, ep["video_id"], include_stale=True)
    if row:
        wins += description_windows(ep["title"], json.loads(row["data"]))
    # Keep windows whose content is unchanged (e.g. all transcript windows after a new
    # description) so only new ones are embedded; the result equals a full rebuild.
    existing: dict[tuple[Any, ...], list[int]] = {}
    old_rows = (
        []
        if rebuild
        else conn.execute(
            "SELECT id, kind, start_ms, end_ms, text FROM windows WHERE episode_id=? ORDER BY id",
            (eid,),
        ).fetchall()
    )
    for r in old_rows:
        existing.setdefault(tuple(r[1:]), []).append(r[0])
    fresh = []
    for w in wins:
        ids = existing.get((w["kind"], w["start_ms"], w["end_ms"], w["text"]))
        if ids:
            ids.pop()
        else:
            fresh.append(w)
    gone = [i for ids in existing.values() for i in ids]
    vecs = embed([w["text"] for w in fresh]) if fresh else np.zeros((0, 0), np.float32)
    with conn:
        if rebuild:
            delete_windows(conn, eid)
        conn.executemany("DELETE FROM windows_fts WHERE rowid=?", [(i,) for i in gone])
        conn.executemany("DELETE FROM windows WHERE id=?", [(i,) for i in gone])
        for w, v in zip(fresh, vecs, strict=True):
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
    return {
        "windows": kinds,
        "description": bool(row),
        "embedded": len(fresh),
        "kept": len(wins) - len(fresh),
    }
