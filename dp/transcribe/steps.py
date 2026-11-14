"""Step functions for download → prepare → transcribe of one episode."""

import json
import logging
import random
import sqlite3
import time
from pathlib import Path
from typing import Any

from dp import pipeline
from dp.config import Settings
from dp.index import build as index_build
from dp.schedule import wait_if_paused
from dp.text import fold, to_latin
from dp.transcribe import audio, download, quality, speakers, whisper

log = logging.getLogger(__name__)

# Whisper's initial prompt only takes ~220 tokens; main and most frequent names first.
PROMPT_MAX_CHARS = 600
# A chunk that loops is redone in pieces this long (no loops in the pilot experiment).
RETRY_PIECE_S = 30.0


def whisper_prompt(conn: sqlite3.Connection) -> str:
    order = {"glavni": 0, "na_ekranu": 1, "samo_glas": 2, "samo_u_prici": 3}
    rows = sorted(
        conn.execute("SELECT id, name, kind FROM characters"),
        key=lambda r: (order[r["kind"]], r["name"]),
    )
    names: list[str] = []
    for r in rows:
        names.append(r["name"])
        if r["kind"] == "glavni":
            # Full-name variants of the main characters (e.g. "Dragan Jove Torbica").
            names += [
                a[0]
                for a in conn.execute(
                    "SELECT alias FROM aliases WHERE character_id=? AND alias LIKE '% %' "
                    "ORDER BY alias",
                    (r["id"],),
                )
                if a[0] != r["name"]
            ]
    prompt = "Državni posao. "
    for name in names:
        nxt = f"{prompt}{name}, "
        if len(nxt) > PROMPT_MAX_CHARS:
            break
        prompt = nxt
    return prompt.rstrip(", ") + "."


def _paths(s: Settings, eid: int) -> tuple[Path, Path, Path]:
    return s.audio_dir / f"{eid}.opus", s.work_dir / str(eid) / "audio.wav", s.work_dir / str(eid)


def do_download(s: Settings, eid: int, video_id: str) -> dict[str, Any]:
    return download.download(video_id, eid, s.data_dir)


def do_prepare(s: Settings, eid: int) -> dict[str, Any]:
    opus, wav, work = _paths(s, eid)
    audio.to_wav(opus, wav)
    total = audio.duration_s(wav)
    quiet = audio.silences(wav)
    chunks = audio.plan_chunks(total, quiet, s.chunk_target_s, s.chunk_max_s, s.edge_s)
    (work / "chunks.json").write_text(json.dumps(chunks))
    return {"duration_s": round(total, 2), "silences": len(quiet), "chunks": len(chunks)}


def _segments(res: dict[str, Any], offset: float) -> list[dict[str, Any]]:
    out = []
    for seg in res.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if text:
            out.append(
                {"start": offset + float(seg["start"]), "end": offset + float(seg["end"]),
                 "text": text}
            )  # fmt: skip
    return out


def split_points(start: float, end: float, step: float, min_tail: float = 5.0) -> list[float]:
    """Boundaries of `step`-long pieces over [start, end]; a short tail joins the last piece."""
    points = [start]
    while end - points[-1] > step:
        points.append(points[-1] + step)
    if len(points) > 1 and end - points[-1] < min_tail:
        points.pop()
    return [*points, end]


class _Transcriber:
    """Sends pieces of one episode's WAV to Whisper, one at a time, and keeps the score."""

    def __init__(self, s: Settings, wav: Path, work: Path, prompt: str) -> None:
        self.s, self.wav, self.work, self.prompt = s, wav, work, prompt
        self.busy = 0.0
        self.calls = 0

    def piece(self, start: float, end: float, prompt: str) -> list[dict[str, Any]]:
        wait_if_paused(self.s.pause_start, self.s.pause_end)
        path = self.work / "piece.wav"
        audio.cut(self.wav, start, end, path)
        t0 = time.monotonic()
        res = whisper.transcribe(self.s.whisper_url, path, self.s.language, prompt)
        self.busy += time.monotonic() - t0
        self.calls += 1
        path.unlink()
        return _segments(res, start)

    def chunk(self, start: float, end: float) -> dict[str, Any]:
        """Transcribe one chunk; if it looks like a loop, redo it in short pieces."""
        segs = self.piece(start, end, self.prompt)
        rec: dict[str, Any] = {"start": start, "end": end, "first_try": segs, "retry": None}
        if not quality.is_suspect(segs, end - start):
            rec["segments"] = segs
            return rec
        log.info("chunk %.0f-%.0f s suspect (loop/too little text), retrying", start, end)
        bounds = split_points(start, end, RETRY_PIECE_S)
        retry, final = [], []
        for a, b in zip(bounds, bounds[1:], strict=False):
            # A piece as short as the chunk was already tried with the prompt.
            got = segs if len(bounds) == 2 else self.piece(a, b, self.prompt)
            mode = "prompt"
            if quality.is_suspect(got, b - a):
                alt = self.piece(a, b, "")
                if not quality.is_suspect(alt, b - a) or _chars(alt) > _chars(got):
                    got, mode = alt, "no_prompt"
            retry.append({"start": a, "end": b, "mode": mode})
            final += got
        rec["retry"] = retry
        rec["segments"] = final
        return rec


def _chars(segs: list[dict[str, Any]]) -> int:
    return sum(len(g["text"]) for g in quality.clean(segs))


def _previous_chunks(
    path: Path,
) -> dict[tuple[float, float], tuple[list[dict[str, Any]], bool]]:
    """Chunks of an earlier run of this episode that need no new Whisper call: those
    that passed the checks and those already repaired (a repaired chunk may still hold
    a stretched line over music, which clean() drops; redoing it would not help)."""
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    out = {}
    for ch in doc.get("chunks", []):
        segs = ch["segments"] if "segments" in ch else _segments(ch["raw"], ch["start"])
        repaired = bool(ch.get("retry")) or ch.get("repaired", False)
        if repaired or not quality.is_suspect(segs, ch["end"] - ch["start"]):
            out[(round(ch["start"], 3), round(ch["end"], 3))] = (segs, repaired)
    return out


def do_transcribe(s: Settings, conn: sqlite3.Connection, eid: int) -> dict[str, Any]:
    """Transcribe chunk by chunk. Chunks a previous run got right (no loop) are reused,
    so re-running this step only redoes the bad parts."""
    _, wav, work = _paths(s, eid)
    chunks: list[list[float]] = json.loads((work / "chunks.json").read_text())
    out_path = s.transcripts_dir / f"{eid}.json"
    previous = _previous_chunks(out_path)
    tr = _Transcriber(s, wav, work, whisper_prompt(conn))
    out_chunks: list[dict[str, Any]] = []
    reused = retried = 0
    for start, end in chunks:
        old = previous.get((round(start, 3), round(end, 3)))
        if old is not None:
            segs, repaired = old
            out_chunks.append(
                {"start": start, "end": end, "reused": True, "repaired": repaired,
                 "segments": segs}
            )  # fmt: skip
            reused += 1
            continue
        rec = tr.chunk(start, end)
        retried += rec["retry"] is not None
        out_chunks.append(rec)

    raw_segments = [g for ch in out_chunks for g in ch["segments"]]
    segments = quality.clean(raw_segments)
    s.transcripts_dir.mkdir(parents=True, exist_ok=True)
    doc = {"episode_id": eid, "prompt": tr.prompt, "chunks": out_chunks, "segments": segments}
    out_path.write_text(json.dumps(doc, ensure_ascii=False))
    with conn:
        conn.execute("DELETE FROM segments WHERE episode_id=?", (eid,))
        conn.executemany(
            """INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin, text_fold)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (eid, i, round(g["start"] * 1000), round(g["end"] * 1000), g["text"],
                 to_latin(g["text"]), fold(g["text"]))
                for i, g in enumerate(segments)
            ],
        )  # fmt: skip
    audio_s = chunks[-1][1] if chunks else 0.0
    done_s = sum(e - a for (a, e), ch in zip(chunks, out_chunks, strict=True) if "reused" not in ch)
    return {
        "audio_s": round(audio_s, 1),
        "whisper_s": round(tr.busy, 1),
        "whisper_calls": tr.calls,
        "speed_x": round(done_s / tr.busy, 2) if tr.busy else None,
        "chunks_reused": reused,
        "chunks_retried": retried,
        "segments_dropped": len(raw_segments) - len(segments),
        "segments": len(segments),
        "chars": sum(len(g["text"]) for g in segments),
    }


def run_episode(
    s: Settings, conn: sqlite3.Connection, eid: int, steps: list[str], force: bool = False
) -> dict[str, Any]:
    row = conn.execute("SELECT video_id FROM episodes WHERE id=? AND retired=0", (eid,)).fetchone()
    if row is None or row["video_id"] is None:
        raise ValueError(f"episode {eid} not in the active catalog")
    vid = row["video_id"]
    results: dict[str, Any] = {}
    for step in pipeline.STEPS:
        if step not in steps:
            continue
        if step == "download":
            fn = lambda: do_download(s, eid, vid)  # noqa: E731
        elif step == "prepare":
            fn = lambda: do_prepare(s, eid)  # noqa: E731
        elif step == "transcribe":
            fn = lambda: do_transcribe(s, conn, eid)  # noqa: E731
        elif step == "speakers":
            fn = lambda: speakers.do_speakers(s, conn, eid, speakers.get_embedder(s))  # noqa: E731
        else:
            fn = lambda: index_build.do_index(s, conn, eid)  # noqa: E731
        results[step] = pipeline.run_step(conn, eid, step, vid, fn, force=force)
    return results


def polite_pause(s: Settings) -> None:
    lo, hi = s.download_pause_s
    time.sleep(random.uniform(lo, hi))
