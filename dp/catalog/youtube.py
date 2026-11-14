"""Fetch the channel's video list and playlists with yt-dlp (flat, no downloads).

Raw yt-dlp output is saved under catalog_dir so that parsing can be rerun
without hitting YouTube again.
"""

import json
import logging
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import yt_dlp

log = logging.getLogger(__name__)

CHANNEL_TABS = ("videos", "streams", "shorts")
STAGING = ".staging"
CURRENT = "current"  # published listings; replaced as a whole directory
OLD = ".old"
STAGING_MARKER = ".started"
STAGING_MAX_AGE_S = 6 * 3600
# A newly missing share above this needs --force (guards against a truncated listing).
MAX_NEW_MISSING = 0.10
UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}
UNAVAILABLE = {"private", "needs_auth", "subscriber_only"}


def _extract_flat(url: str) -> dict[str, Any]:
    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return ydl.sanitize_info(info)


class TooManyMissing(Exception):
    """The listings would newly mark too many videos as missing; nothing was changed."""

    def __init__(self, newly_missing: int, present_before: int) -> None:
        super().__init__(
            f"{newly_missing} of {present_before} videos would become missing; "
            "the listing may be truncated (use --force to accept)"
        )
        self.newly_missing = newly_missing
        self.present_before = present_before


class FetchIncomplete(Exception):
    """Some listings failed; finished ones stay in the staging dir and a rerun resumes."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(f"{len(errors)} listing(s) failed: {', '.join(errors)}")
        self.errors = errors


def fetch_channel(channel_url: str, out_dir: Path, pause_s: float = 3.0) -> dict[str, int]:
    """Save raw flat listings of channel tabs and every playlist to out_dir.

    Listings are written to out_dir/.staging first. A failed listing does not stop
    the others; if any failed, FetchIncomplete is raised, the published listings are
    left untouched and a rerun only fetches what is still missing in staging (staging
    older than STAGING_MAX_AGE_S is discarded). When everything succeeded, staging
    replaces out_dir/current as a whole, so playlists removed from the channel do not
    linger.
    """
    staging = out_dir / STAGING
    _discard_stale_staging(staging)
    (staging / "playlists").mkdir(parents=True, exist_ok=True)
    marker = staging / STAGING_MARKER
    if not marker.exists():
        marker.touch()
    counts: dict[str, int] = {}
    errors: dict[str, str] = {}

    def fetch(name: str, url: str, path: Path, missing_ok: bool = False) -> dict[str, Any] | None:
        if path.exists():
            info = json.loads(path.read_text())
        else:
            try:
                info = _extract_flat(url)
            except yt_dlp.utils.DownloadError as e:
                if missing_ok and "does not have" in str(e):
                    info = {"entries": []}
                else:
                    log.warning("%s failed: %s", name, e)
                    errors[name] = str(e)
                    return None
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(info, ensure_ascii=False))
            tmp.replace(path)
            time.sleep(pause_s)
        counts[name] = len(info.get("entries") or [])
        return info

    for tab in CHANNEL_TABS:
        fetch(f"tab_{tab}", f"{channel_url}/{tab}", staging / f"tab_{tab}.json", missing_ok=True)

    pl_index = fetch("playlists", f"{channel_url}/playlists", staging / "playlists.json")
    for pl in (pl_index or {}).get("entries") or []:
        url = f"https://www.youtube.com/playlist?list={pl['id']}"
        fetch(
            f"playlist {pl.get('title') or pl['id']}",
            url,
            staging / "playlists" / f"{pl['id']}.json",
        )

    if errors:
        raise FetchIncomplete(errors)
    _publish(staging, out_dir)
    return counts


def _discard_stale_staging(staging: Path) -> None:
    """Drop a staging dir started more than STAGING_MAX_AGE_S ago (or of unknown age)."""
    if not staging.exists():
        return
    marker = staging / STAGING_MARKER
    if not marker.exists() or time.time() - marker.stat().st_mtime > STAGING_MAX_AGE_S:
        log.info("discarding stale staging dir %s", staging)
        shutil.rmtree(staging)


def _publish(staging: Path, out_dir: Path) -> None:
    """Swap staging in as out_dir/current using directory renames only."""
    current, old = out_dir / CURRENT, out_dir / OLD
    if current.exists():
        shutil.rmtree(old, ignore_errors=True)
        current.rename(old)
    staging.rename(current)
    shutil.rmtree(old, ignore_errors=True)


def listings_dir(raw_dir: Path) -> Path:
    """Where the published listings are: current, a leftover .old from an interrupted
    publish, or the legacy layout directly in raw_dir."""
    for name in (CURRENT, OLD):
        if (raw_dir / name).is_dir():
            return raw_dir / name
    return raw_dir


def _unavailable(entry: dict[str, Any]) -> bool:
    """Private or deleted placeholders carry no real data about the video."""
    title = (entry.get("title") or "").strip().lower()
    return title in UNAVAILABLE_TITLES or entry.get("availability") in UNAVAILABLE


def import_raw(conn: sqlite3.Connection, raw_dir: Path, force: bool = False) -> int:
    """Upsert videos from saved raw listings into the videos table.

    Videos absent from the listings get missing=1, the listed ones missing=0. Private
    and deleted placeholders are ignored (not upserted, not counted as present).
    Raises TooManyMissing, changing nothing, when more than MAX_NEW_MISSING of the
    currently present videos would become missing, unless force is set.
    """
    src = listings_dir(raw_dir)
    videos: dict[str, dict[str, Any]] = {}
    for tab_file in sorted(src.glob("tab_*.json")):
        for e in json.loads(tab_file.read_text()).get("entries") or []:
            if not _unavailable(e):
                videos.setdefault(e["id"], e)

    playlists: dict[str, list[str]] = {}
    for pl_file in sorted((src / "playlists").glob("*.json")):
        info = json.loads(pl_file.read_text())
        pl_title = info.get("title") or info["id"]
        for e in info.get("entries") or []:
            if _unavailable(e):
                continue
            videos.setdefault(e["id"], e)  # playlist may hold videos missing from tabs
            titles = playlists.setdefault(e["id"], [])
            if pl_title not in titles:
                titles.append(pl_title)

    if videos and not force:
        present = {r[0] for r in conn.execute("SELECT video_id FROM videos WHERE missing=0")}
        newly = len(present - videos.keys())
        if newly > MAX_NEW_MISSING * len(present):
            raise TooManyMissing(newly, len(present))

    with conn:
        for vid, e in videos.items():
            conn.execute(
                """INSERT INTO videos (video_id, title, duration_s, upload_date, playlists, raw)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET
                     title=excluded.title,
                     duration_s=COALESCE(excluded.duration_s, videos.duration_s),
                     upload_date=COALESCE(excluded.upload_date, videos.upload_date),
                     playlists=excluded.playlists, raw=excluded.raw,
                     fetched_at=datetime('now')""",
                (
                    vid,
                    e.get("title") or "",
                    int(e["duration"]) if e.get("duration") else None,
                    _date(e.get("upload_date")),
                    json.dumps(playlists.get(vid, []), ensure_ascii=False),
                    json.dumps(e, ensure_ascii=False),
                ),
            )
        # The listings are a complete published fetch, so anything not in them is gone
        # from the channel. An empty listing set is treated as "nothing fetched yet".
        if videos:
            conn.execute("UPDATE videos SET missing=0")
            conn.execute(
                """UPDATE videos SET missing=1
                   WHERE video_id NOT IN (SELECT value FROM json_each(?))""",
                (json.dumps(list(videos)),),
            )
    return len(videos)


def _date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return None
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"
