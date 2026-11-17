"""Download step: audio (Opus, mono), optionally the video (kept for frames), full
metadata and YouTube's Serbian captions."""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yt_dlp

log = logging.getLogger(__name__)

# "-orig" is YouTube's own speech recognition in the video's language; plain "sr" may be
# a machine translation of captions in another language, so it is kept only as fallback.
SUB_LANGS = ["sr-orig", "sr", "sr-Latn", "sr-Cyrl", "hr-orig", "bs-orig"]


def _ydl_opts(tmp: Path, **extra: Any) -> dict[str, Any]:
    return {
        "outtmpl": str(tmp / "src.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        "retries": 3,
        **extra,
    }


def video_format(height: int) -> str:
    """yt-dlp format: best audio only (height 0), or video up to `height` plus audio."""
    if height <= 0:
        return "bestaudio/best"
    return f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"


def download(
    video_id: str, episode_id: int, data_dir: Path, video_height: int = 0
) -> dict[str, Any]:
    """Audio (and, with video_height > 0, the video kept in data/video/<id>.mkv)."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    tmp = data_dir / "work" / str(episode_id) / "download"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    opts = _ydl_opts(tmp, format=video_format(video_height), writeinfojson=True)
    if video_height > 0:
        opts["merge_output_format"] = "mkv"
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    src = next(p for p in tmp.iterdir() if p.name.startswith("src.") and p.suffix != ".json")

    audio_dir = data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    out = audio_dir / f"{episode_id}.opus"
    part = out.with_suffix(".part.opus")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-c:a", "libopus", "-b:a", "32k", str(part)],
        check=True,
    )  # fmt: skip
    part.replace(out)

    video: dict[str, Any] = {}
    if video_height > 0:
        video_dir = data_dir / "video"
        video_dir.mkdir(exist_ok=True)
        vout = video_dir / f"{episode_id}.mkv"
        src.replace(vout)
        video = {"video_bytes": vout.stat().st_size, "video_height": _height(vout)}

    meta_dir = data_dir / "meta"
    meta_dir.mkdir(exist_ok=True)
    info = json.loads((tmp / "src.info.json").read_text())
    (meta_dir / f"{episode_id}.info.json").write_text(json.dumps(info, ensure_ascii=False))

    subs = _download_subs(url, tmp, data_dir / "subs", episode_id)
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "audio_bytes": out.stat().st_size,
        "source_ext": src.suffix.lstrip("."),
        "upload_date": info.get("upload_date"),
        "duration": info.get("duration"),
        "auto_caption_langs": sorted(
            k for k in (info.get("automatic_captions") or {}) if k.startswith(("sr", "hr", "bs"))
        ),
        "subtitle_langs": sorted(info.get("subtitles") or {}),
        "subs_saved": subs,
        **video,
    }


def fetch_video(video_id: str, episode_id: int, data_dir: Path, height: int) -> dict[str, Any]:
    """Only the video, into data/video/<id>.mkv, for an episode whose audio is already
    processed: no pipeline step changes."""
    tmp = data_dir / "work" / str(episode_id) / "video"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    opts = _ydl_opts(tmp, format=video_format(height), merge_output_format="mkv")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
    src = next(p for p in tmp.iterdir() if p.name.startswith("src."))
    video_dir = data_dir / "video"
    video_dir.mkdir(exist_ok=True)
    out = video_dir / f"{episode_id}.mkv"
    src.replace(out)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"video_bytes": out.stat().st_size, "video_height": _height(out)}


def _height(path: Path) -> int | None:
    """Height of the first video stream, or None when it has none or ffprobe fails."""
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )  # fmt: skip
    try:
        return int(res.stdout.strip().splitlines()[0])
    except (IndexError, ValueError):
        return None


def _download_subs(url: str, tmp: Path, subs_dir: Path, episode_id: int) -> list[str]:
    """Best effort: a caption failure must not fail the audio download."""
    opts = _ydl_opts(
        tmp,
        skip_download=True,
        writesubtitles=True,
        writeautomaticsub=True,
        subtitleslangs=SUB_LANGS,
        subtitlesformat="vtt",
    )
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        log.warning("episode %s: captions not downloaded: %s", episode_id, e)
    saved = []
    subs_dir.mkdir(parents=True, exist_ok=True)
    for vtt in tmp.glob("src.*.vtt"):
        lang = vtt.name.removeprefix("src.").removesuffix(".vtt")
        vtt.replace(subs_dir / f"{episode_id}.{lang}.vtt")
        saved.append(lang)
    return sorted(saved)
