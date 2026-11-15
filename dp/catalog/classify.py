"""Classify channel videos and build the episodes table.

Episode titles look like: "DRŽAVNI POSAO [HQ] - Ep.2036: Na potpis (25.09.2023.)".
Season comes from the "DRŽAVNI POSAO <n>[ - part 2]" playlist; episodes missing from
season playlists inherit the season of their neighbouring episode numbers.
"""

import datetime
import json
import re
import sqlite3
from dataclasses import dataclass, field

from dp.index.store import delete_windows

EPISODE_RE = re.compile(
    r"^DRŽAVNI POSAO\s*(?:\[HQ\])?\s*-\s*Ep\.\s*(?P<num>\d+)\s*:\s*(?P<title>.+?)\s*"
    r"\((?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})\.?\)(?:\s*\([^)]*\))?\s*$",
    re.IGNORECASE,
)
SPECIAL_RE = re.compile(
    r"^DRŽAVNI POSAO\s*(?:\[HQ\])?\s*-\s*(?P<title>.+?)\s*"
    r"\((?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})\.?\)\s*$",
    re.IGNORECASE,
)
# Specials have no running number; they get ids above every real episode number.
SPECIAL_ID_BASE = 10000
SEASON_PL_RE = re.compile(r"^DRŽAVNI POSAO (\d+)(?: - part \d+)?$")


@dataclass(frozen=True)
class Parsed:
    kind: str  # episode | special | gag | short | trailer | other_show | clip
    reason: str
    number: int | None = None
    title: str | None = None
    air_date: str | None = None


def classify(title: str, playlists: list[str], duration_s: int | None) -> Parsed:
    m = EPISODE_RE.match(title)
    if m:
        num = int(m["num"])
        if not 0 < num < SPECIAL_ID_BASE:
            # Would collide with special ids (or is no real episode number).
            return Parsed("clip", "episode number out of range")
        return Parsed("episode", "title Ep.N", num, m["title"], _date(m))
    upper = title.upper()
    # Before the shorts check: a special without a known duration is still a special.
    if upper.startswith("DRŽAVNI POSAO") and "SPECIJAL" in upper:
        s = SPECIAL_RE.match(title)
        if s:
            return Parsed("special", "special", None, s["title"], _date(s))
        return Parsed("special", "special", None, title, None)
    if "Šortsi" in playlists or duration_s is None:
        return Parsed("short", "shorts tab/playlist")
    if title.startswith("PRAVI FUDBAL") or "PRAVI FUDBAL" in playlists:
        return Parsed("other_show", "Pravi fudbal")
    if title.startswith("GEGOVI") or any(p.startswith("GEGOVI") for p in playlists):
        return Parsed("gag", "Gegovi / cenzurisano")
    if "[NAJAVA]" in upper or "SEZONA" in upper or "USKORO" in upper or duration_s < 120:
        return Parsed("trailer", "announcement or short promo")
    return Parsed("clip", "no episode number in title")


def _date(m: re.Match[str]) -> str | None:
    """ISO date from the d/m/y groups, or None when the date does not exist."""
    try:
        return datetime.date(int(m["y"]), int(m["m"]), int(m["d"])).isoformat()
    except ValueError:
        return None


def season_of(playlists: list[str]) -> int | None:
    for p in playlists:
        m = SEASON_PL_RE.match(p)
        if m:
            return int(m[1])
    return None


@dataclass
class BuildResult:
    episodes: int
    specials: int
    by_kind: dict[str, int]
    duplicates: dict[int, list[str]]
    missing_numbers: list[int]
    season_inferred: list[int]
    missing_videos: int = 0  # videos gone from the channel (videos.missing=1)
    steps_reset: list[int] = field(default_factory=list)  # ids whose video changed


def build_episodes(conn: sqlite3.Connection) -> BuildResult:
    """Classify all videos and sync the episodes table (idempotent; rows are only retired)."""
    rows = conn.execute(
        "SELECT video_id, title, duration_s, playlists, missing FROM videos"
    ).fetchall()
    missing_videos = 0
    by_kind: dict[str, int] = {}
    candidates: dict[int, list[tuple[str, Parsed, int | None, int | None]]] = {}
    specials: list[tuple[str, Parsed, int | None, int | None]] = []
    with conn:
        for r in rows:
            playlists = json.loads(r["playlists"])
            p = classify(r["title"], playlists, r["duration_s"])
            by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
            conn.execute(
                "UPDATE videos SET kind=?, kind_reason=? WHERE video_id=?",
                (p.kind, p.reason, r["video_id"]),
            )
            if r["missing"]:
                # Gone from the channel: not produced, so its episode row gets retired.
                missing_videos += 1
            elif p.kind == "episode":
                assert p.number is not None
                candidates.setdefault(p.number, []).append(
                    (r["video_id"], p, season_of(playlists), r["duration_s"])
                )
            elif p.kind == "special":
                specials.append((r["video_id"], p, season_of(playlists), r["duration_s"]))

        duplicates: dict[int, list[str]] = {}
        chosen: dict[int, tuple[str, Parsed, int | None, int | None]] = {}
        for num, cands in candidates.items():
            if len(cands) > 1:
                duplicates[num] = [c[0] for c in cands]
            # Deterministic pick: season-playlist upload, then longer, then smaller video_id.
            chosen[num] = min(cands, key=lambda c: (c[2] is None, -(c[3] or 0), c[0]))

        seasons = _fill_seasons({n: c[2] for n, c in chosen.items()})
        inferred = sorted(n for n, c in chosen.items() if c[2] is None and seasons[n] is not None)

        # Specials keep the id they got on an earlier build (episode_steps is keyed by id),
        # also when they were retired in between. Active rows win over retired ones.
        known_special = {
            r["vid"]: r["id"]
            for r in conn.execute(
                """SELECT id, coalesce(video_id, retired_video_id) vid FROM episodes
                   WHERE id > ? AND coalesce(video_id, retired_video_id) IS NOT NULL
                   ORDER BY retired DESC, id""",
                (SPECIAL_ID_BASE,),
            )
        }
        # Never reuse a special id, not even one of a retired row.
        last = conn.execute(
            "SELECT max(id) FROM episodes WHERE id > ?", (SPECIAL_ID_BASE,)
        ).fetchone()[0]
        next_special = (last or SPECIAL_ID_BASE) + 1

        rows_out = [
            (num, vid, seasons[num], p, dur, "episode")
            for num, (vid, p, _, dur) in sorted(chosen.items())
        ]
        # New specials get ids after every id ever used, in (air date, title) order.
        # Season comes from the playlist, else from the last regular episode aired on
        # or before the special.
        dated = sorted(
            ((p.air_date or "", seasons[n]) for n, (_, p, _, _) in chosen.items()),
            key=lambda d: d[0],
        )
        specials.sort(key=lambda c: (c[1].air_date or "", c[1].title or ""))
        for vid, p, pl_season, dur in specials:
            if vid in known_special:
                sid = known_special[vid]
            else:
                sid, next_special = next_special, next_special + 1
            season = pl_season or _season_by_date(dated, p.air_date)
            rows_out.append((sid, vid, season, p, dur, "special"))

        # Video each id held before this build (active or retired).
        previous = {
            r["id"]: r["vid"]
            for r in conn.execute(
                "SELECT id, coalesce(video_id, retired_video_id) vid FROM episodes"
            )
        }
        _retire_missing(conn, {r[0] for r in rows_out})
        steps_reset: list[int] = []
        # Every active row is rewritten below; clear video_id first so a video moving
        # to another id never hits UNIQUE(video_id).
        conn.execute("UPDATE episodes SET video_id=NULL WHERE retired=0")
        for eid, vid, season, p, dur, kind in rows_out:
            conn.execute(
                """INSERT INTO episodes (id, video_id, season, title, air_date, duration_s, kind)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET video_id=excluded.video_id,
                     season=excluded.season, title=excluded.title, air_date=excluded.air_date,
                     duration_s=excluded.duration_s, kind=excluded.kind,
                     retired=0, retired_video_id=NULL""",
                (eid, vid, season, p.title, p.air_date, dur, kind),
            )
            # Steps were done on another video: drop them so processing is redone.
            if eid in previous and previous[eid] != vid:
                # Derived data belongs to the old video too. enrichment is kept on
                # purpose (paid LLM output); its video_id no longer equals episodes.video_id, so
                # enrich and index ignore it.
                delete_windows(conn, eid)
                conn.execute("DELETE FROM segments WHERE episode_id=?", (eid,))
                cur = conn.execute("DELETE FROM episode_steps WHERE episode_id=?", (eid,))
                if cur.rowcount:
                    steps_reset.append(eid)

    nums = sorted(chosen)
    missing = sorted(set(range(1, nums[-1] + 1)) - set(nums)) if nums else []
    return BuildResult(
        len(chosen),
        len(specials),
        by_kind,
        duplicates,
        missing,
        inferred,
        missing_videos,
        steps_reset,
    )


def _retire_missing(conn: sqlite3.Connection, produced: set[int]) -> None:
    """Retire active rows the classifier no longer produces (rows are never deleted)."""
    for (eid,) in conn.execute("SELECT id FROM episodes WHERE retired=0").fetchall():
        if eid not in produced:
            conn.execute(
                """UPDATE episodes SET retired=1, retired_video_id=video_id, video_id=NULL
                   WHERE id=?""",
                (eid,),
            )


def _season_by_date(dated: list[tuple[str, int | None]], air_date: str | None) -> int | None:
    """Season of the last regular episode with a known season aired on or before air_date.

    `dated` must be sorted by date.
    """
    if not air_date:
        return None
    before = [season for d, season in dated if d and d <= air_date and season is not None]
    return before[-1] if before else None


def _fill_seasons(known: dict[int, int | None]) -> dict[int, int | None]:
    """Give episodes without a season playlist the season of their neighbours,
    but only when the nearest known episodes on both sides agree."""
    nums = sorted(known)
    out = dict(known)
    for i, n in enumerate(nums):
        if out[n] is not None:
            continue
        prev = next((known[m] for m in reversed(nums[:i]) if known[m] is not None), None)
        nxt = next((known[m] for m in nums[i + 1 :] if known[m] is not None), None)
        out[n] = prev if prev == nxt else None
    return out
