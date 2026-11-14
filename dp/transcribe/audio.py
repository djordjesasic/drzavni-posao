"""Prepare step helpers: 16 kHz mono WAV, silence detection and chunk planning."""

import re
import subprocess
from pathlib import Path

_SILENCE_START = re.compile(r"silence_start: (-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end: ([\d.]+)")


def to_wav(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_suffix(".part.wav")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(part)],
        check=True,
    )  # fmt: skip
    part.replace(dst)


def duration_s(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return float(out.stdout.strip())


def silences(path: Path, noise_db: int = -35, min_s: float = 0.4) -> list[tuple[float, float]]:
    """(start, end) of every silence ffmpeg's silencedetect finds."""
    out = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    return parse_silences(out.stderr)


def parse_silences(stderr: str) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    start: float | None = None
    for line in stderr.splitlines():
        if m := _SILENCE_START.search(line):
            start = max(0.0, float(m[1]))
        elif (m := _SILENCE_END.search(line)) and start is not None:
            result.append((start, float(m[1])))
            start = None
    return result


def _nearest(
    mids: list[float], lo: float, hi: float, ideal: float, fallback: float | None = None
) -> float:
    """Silence midpoint in [lo, hi] closest to ideal; fallback (default ideal) if none."""
    options = [m for m in mids if lo <= m <= hi]
    if options:
        return min(options, key=lambda m: abs(m - ideal))
    return ideal if fallback is None else fallback


def plan_chunks(
    total: float,
    quiet: list[tuple[float, float]],
    target: float,
    maximum: float,
    edge: float = 0.0,
) -> list[tuple[float, float]]:
    """Split [0, total] into pieces of about `target` s, never longer than `maximum`,
    cutting in the middle of the silence closest to the target point.

    With `edge` > 0 the first and last ~edge seconds (intro/outro music) become
    pieces of their own, cut at the nearest silence within ±1/3 of `edge`."""
    mids = [(a + b) / 2 for a, b in quiet]
    start, stop = 0.0, total
    if edge and total > 4 * edge:
        start = _nearest(mids, edge * 2 / 3, edge * 4 / 3, edge)
        stop = _nearest(mids, total - edge * 4 / 3, total - edge * 2 / 3, total - edge)
    chunks: list[tuple[float, float]] = [(0.0, start)] if start else []
    cursor = start
    while stop - cursor > maximum:
        lo, hi, ideal = cursor + target * 0.6, cursor + maximum, cursor + target
        cut = _nearest(mids, lo, hi, ideal, fallback=hi)
        chunks.append((cursor, cut))
        cursor = cut
    chunks.append((cursor, stop))
    if stop < total:
        chunks.append((stop, total))
    return chunks


def cut(src: Path, start: float, end: float, dst: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-c", "copy", str(dst)],
        check=True,
    )  # fmt: skip
