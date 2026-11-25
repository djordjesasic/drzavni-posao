"""Step functions for download → prepare → transcribe of one episode."""

import json
import logging
import os
import random
import sqlite3
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

from dp import pipeline
from dp.config import Settings
from dp.db import active_video_id
from dp.enrich.store import mark_stale
from dp.index import build as index_build
from dp.schedule import wait_if_paused
from dp.text import fold, to_latin
from dp.transcribe import audio, download, quality, speakers, whisper

log = logging.getLogger(__name__)

# Whisper's initial prompt only takes ~220 tokens; main and most frequent names first.
PROMPT_MAX_CHARS = 600
# A chunk that loops is redone in pieces this long (no loops in the pilot experiment).
RETRY_PIECE_S = 30.0
# Lock file in Settings.locks_dir (shared by every series on the machine) that
# serializes our Whisper requests across processes and containers.
WHISPER_LOCK = "whisper.lock"
# Temp files (piece-*.wav, transcript .tmp) older than this are leftovers of a killed
# process (docker stop: SIGKILL after 10 s, no finally). A live process may keep a piece
# through the 65 min pause window plus the lock wait and the 10 min request timeout,
# so the margin is well above that.
STALE_TEMP_S = 3 * 3600


def whisper_prompt(conn: sqlite3.Connection, start: str = "") -> str:
    """`start` (config [transcribe] prompt, e.g. the show's name), then character names."""
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
    prompt = start
    for name in names:
        nxt = f"{prompt}{name}, "
        if len(nxt) > PROMPT_MAX_CHARS:
            break
        prompt = nxt
    return prompt.rstrip(", ") + "."


def _paths(s: Settings, eid: int) -> tuple[Path, Path, Path]:
    return s.audio_dir / f"{eid}.opus", s.work_dir / str(eid) / "audio.wav", s.work_dir / str(eid)


def do_download(s: Settings, eid: int, video_id: str) -> dict[str, Any]:
    return download.download(video_id, eid, s.data_dir, s.video_height)


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

    def __init__(
        self,
        s: Settings,
        wav: Path,
        work: Path,
        prompt: str,
        before: Callable[[], None] | None = None,
    ) -> None:
        self.s, self.wav, self.work, self.prompt = s, wav, work, prompt
        self.before = before
        self.busy = 0.0
        self.calls = 0

    def piece(self, start: float, end: float, prompt: str) -> list[dict[str, Any]]:
        wait_if_paused(self.s.pause_start, self.s.pause_end)
        if self.before:
            self.before()  # the worker's heat check; outside the lock, so others go on
        # Unique across containers too (each may run python as PID 1, all share /data):
        # two runs on one episode must not overwrite or delete each other's audio.
        fd, name = tempfile.mkstemp(dir=self.work, prefix="piece-", suffix=".wav")
        os.close(fd)  # ffmpeg writes the path itself (-y overwrites the empty file)
        path = Path(name)
        try:
            audio.cut(self.wav, start, end, path)
            # Locked per piece, not per chunk/episode, so the other project's requests
            # can still get in between ours.
            with whisper.server_lock(self.s.locks_dir / WHISPER_LOCK):
                # The lock wait may have run into the pause window.
                wait_if_paused(self.s.pause_start, self.s.pause_end)
                t0 = time.monotonic()  # after the lock: waiting is not Whisper's speed
                res = whisper.transcribe(self.s.whisper_url, path, self.s.language, prompt)
                self.busy += time.monotonic() - t0
                self.calls += 1
        finally:
            path.unlink(missing_ok=True)
        return _segments(res, start)

    def chunk(self, start: float, end: float) -> dict[str, Any]:
        """Transcribe one chunk; if it looks like a loop, redo it in short pieces."""
        segs = self.piece(start, end, self.prompt)
        rec: dict[str, Any] = {
            "start": start, "end": end, "first_try": segs, "retry": None,
            "retried": False, "unresolved": False,
        }  # fmt: skip
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
        rec["retried"] = True
        rec["unresolved"] = quality.too_little_text(final, end - start)
        if rec["unresolved"]:
            log.warning("chunk %.0f-%.0f s still has too little text after retry", start, end)
        return rec


def _chars(segs: list[dict[str, Any]]) -> int:
    return sum(len(g["text"]) for g in quality.clean(segs))


# Unresolved chunks: a chunk that still has too little text after the retry
# (quality.too_little_text) is stored with unresolved=True. The step still ends as done
# (a real music-only stretch would otherwise fail it forever). A plain run, also one
# resuming after a failure, reuses it as it is, so resuming never repeats that work;
# only a forced run sends it to Whisper again. Use `dp run --force --steps transcribe`:
# that redoes only such chunks and leaves speakers/index alone where the text did not
# change. A plain `dp run --force` also redoes download and prepare, which invalidates
# speakers and index of every episode.
#
# Chunk record keys: "retried" (went through the 30 s retry) and "unresolved" (retried
# and still too little text) are written since round 7. Legacy shapes, still read:
# "retry" list alone (pilot fresh records) and "repaired" (pilot reused records: was
# retried; briefly also fresh records: retried and not hollow).


class _Prev(NamedTuple):
    segs: list[dict[str, Any]]
    retried: bool
    unresolved: bool


def _previous_chunks(path: Path, video_id: str) -> dict[tuple[float, float], _Prev]:
    """Chunks of an earlier run of this episode that a plain run need not send to
    Whisper again: those that passed the checks and those already retried (a retried
    chunk may still hold a stretched line over music, which clean() drops; redoing it
    would not help). Unresolved ones are included and flagged; do_transcribe redoes
    them only when forced (see "Unresolved chunks").

    Only a transcript of the same video counts: after a re-point the chunk plan often
    has the same boundaries (the 0-30 s intro), and the old video's text would slip in.
    A file without video_id is never trusted here; `dp label-transcripts` labels the
    pilot files it can vouch for (see label_transcripts)."""
    doc = _read_transcript(path)
    if doc is None:
        return {}
    if doc.get("video_id") != video_id:
        log.info("ignoring %s: written for another video", path)
        return {}
    out = {}
    try:
        for ch in doc.get("chunks", []):
            segs = ch["segments"] if "segments" in ch else _segments(ch["raw"], ch["start"])
            span = ch["end"] - ch["start"]
            # Legacy records (see "Chunk record keys") carry no explicit flags.
            retried = bool(
                ch["retried"] if "retried" in ch else ch.get("retry") or ch.get("repaired")
            )
            unresolved = bool(
                ch["unresolved"]
                if "unresolved" in ch
                else retried and quality.too_little_text(segs, span)
            )
            if retried or not quality.is_suspect(segs, span):
                key = (round(ch["start"], 3), round(ch["end"], 3))
                out[key] = _Prev(segs, retried, unresolved)
    except (KeyError, TypeError, ValueError) as e:
        log.warning("ignoring malformed transcript %s: %s", path, e)
        return {}
    return out


def _read_transcript(path: Path) -> dict[str, Any] | None:
    """The transcript document, or None if missing or unreadable. A corrupt file costs
    one re-transcription instead of failing the step forever."""
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable transcript %s: %s", path, e)
        return None
    if not isinstance(doc, dict):
        log.warning("ignoring transcript %s: not a JSON object", path)
        return None
    return doc


def _remove_stale_temp(s: Settings, eid: int, now: float | None = None) -> None:
    """Delete this episode's temp files left by a killed process (see STALE_TEMP_S)."""
    cutoff = (time.time() if now is None else now) - STALE_TEMP_S
    leftovers = [
        *(s.work_dir / str(eid)).glob("piece-*.wav"),
        *s.transcripts_dir.glob(f".{eid}-*.tmp"),  # _write_atomic's temp names
    ]
    for p in leftovers:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                log.info("removed leftover %s", p)
        except FileNotFoundError:
            pass  # another process cleaned it up first


def _write_atomic(path: Path, doc: dict[str, Any]) -> None:
    """Replace path in one step, so a crash mid-write never leaves a truncated file
    that would make the next run lose every finished chunk."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())  # on disk before the rename, so a crash cannot leave it empty
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; transcripts were always world-readable
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def label_transcripts(s: Settings, conn: sqlite3.Connection) -> dict[str, Any]:
    """One-off migration (`dp label-transcripts`), safe to repeat: write video_id into
    transcripts from before it was stored (the pilot), nothing else changed. Only when
    transcribe is recorded done for the episode's current video, which proves the file
    belongs to it; any other file stays unlabelled and is never reused."""
    labelled, already = 0, 0
    skipped: dict[str, str] = {}
    for path in sorted(s.transcripts_dir.glob("*.json")):
        if not path.stem.isdigit():
            skipped[path.name] = "not an episode transcript"
            continue
        eid = int(path.stem)
        doc = _read_transcript(path)
        if doc is None:
            skipped[path.name] = "unreadable"
        elif "video_id" in doc:
            already += 1
        elif (vid := active_video_id(conn, eid)) is None:
            skipped[path.name] = "episode not in the active catalog"
        elif not pipeline.is_done(conn, eid, "transcribe", vid):
            skipped[path.name] = "transcribe not recorded done for the current video"
        else:
            _write_atomic(path, {**doc, "video_id": vid})
            labelled += 1
    return {"labelled": labelled, "already_labelled": already, "skipped": skipped}


def _rows(segments: list[dict[str, Any]]) -> list[tuple[int, int, str]]:
    return [(round(g["start"] * 1000), round(g["end"] * 1000), g["text"]) for g in segments]


def _reused(start: float, end: float, old: _Prev) -> dict[str, Any]:
    return {
        "start": start, "end": end, "reused": True, "retried": old.retried,
        "unresolved": old.unresolved, "segments": old.segs,
    }  # fmt: skip


def do_transcribe(
    s: Settings,
    conn: sqlite3.Connection,
    eid: int,
    video_id: str,
    force: bool = False,
    before_whisper: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Transcribe chunk by chunk. Chunks a previous run of the same video got right
    (no loop) are reused, so re-running this step only redoes the bad parts.
    force: also redo unresolved chunks (see "Unresolved chunks").
    before_whisper: called before every Whisper request (the worker waits there while
    the machine is too hot)."""
    _, wav, work = _paths(s, eid)
    _remove_stale_temp(s, eid)
    chunks: list[list[float]] = json.loads((work / "chunks.json").read_text())
    out_path = s.transcripts_dir / f"{eid}.json"
    previous = _previous_chunks(out_path, video_id)
    tr = _Transcriber(s, wav, work, whisper_prompt(conn, s.whisper_prompt), before_whisper)
    out_chunks: list[dict[str, Any]] = []
    reused = retried = unresolved = 0
    s.transcripts_dir.mkdir(parents=True, exist_ok=True)

    def doc(chunks_: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "episode_id": eid, "video_id": video_id, "prompt": tr.prompt, "chunks": chunks_,
            **extra,
        }  # fmt: skip

    def partial(todo: list[list[float]]) -> dict[str, Any]:
        """todo: chunks not reached yet. A partial file replaces the previous one, so it
        also carries those of them the previous file had right; otherwise a failure on
        a later chunk would throw that good work away. No "segments": only
        _previous_chunks reads a partial file, and it reads chunks, so cleaning the
        whole transcript after every chunk would be wasted."""
        ahead = [(a, b, previous.get((round(a, 3), round(b, 3)))) for a, b in todo]
        kept = out_chunks + [_reused(a, b, old) for a, b, old in ahead if old is not None]
        return doc(kept, partial=True)

    for i, (start, end) in enumerate(chunks):
        old = previous.get((round(start, 3), round(end, 3)))
        if old is not None and not (force and old.unresolved):
            out_chunks.append(_reused(start, end, old))
            reused += 1
            unresolved += old.unresolved
            continue
        rec = tr.chunk(start, end)
        retried += rec["retried"]
        unresolved += rec["unresolved"]
        out_chunks.append(rec)
        # Saved after every new chunk so a failure later on (e.g. a Whisper error body)
        # costs only the chunk in progress: _previous_chunks reuses the rest next run.
        _write_atomic(out_path, partial(chunks[i + 1 :]))

    raw_segments = [g for ch in out_chunks for g in ch["segments"]]
    segments = quality.clean(raw_segments)
    _write_atomic(out_path, doc(out_chunks, segments=segments))
    rows = _rows(segments)
    old_rows = [
        (r["start_ms"], r["end_ms"], r["text"])
        for r in conn.execute(
            "SELECT start_ms, end_ms, text FROM segments WHERE episode_id=? ORDER BY idx", (eid,)
        )
    ]
    # Same text as stored: keep the rows (and their speaker labels) and report it, so a
    # forced run that only retries hollow chunks is cheap where nothing changed. run_step
    # decides whether that may spare speakers/index (only if transcribe was done before).
    unchanged = old_rows == rows
    if not unchanged:
        with conn:
            conn.execute("DELETE FROM segments WHERE episode_id=?", (eid,))
            conn.executemany(
                """INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin,
                                         text_fold)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (eid, i, a, b, text, to_latin(text), fold(text))
                    for i, (a, b, text) in enumerate(rows)
                ],
            )
            # Descriptions were made from the old text: `dp enrich` redoes them (owner's
            # decision, 2026-11-15; about 3 cents per episode with Sonnet).
            stale = mark_stale(conn, eid)
            if stale:
                log.info("episode %s: transcript changed, %d description(s) stale", eid, stale)
    audio_s = chunks[-1][1] if chunks else 0.0
    done_s = sum(e - a for (a, e), ch in zip(chunks, out_chunks, strict=True) if "reused" not in ch)
    return {
        "audio_s": round(audio_s, 1),
        "whisper_s": round(tr.busy, 1),
        "whisper_calls": tr.calls,
        "speed_x": round(done_s / tr.busy, 2) if tr.busy else None,
        "chunks_reused": reused,
        "chunks_retried": retried,
        # Retried but still implausibly little text (fresh or reused). The step is done
        # anyway; retry with `dp run --force --steps transcribe` (a plain --force also
        # redoes download and prepare, invalidating speakers and index everywhere; see
        # "Unresolved chunks").
        "chunks_unresolved": unresolved,
        "segments_dropped": len(raw_segments) - len(segments),
        "segments": len(segments),
        "chars": sum(len(g["text"]) for g in segments),
        pipeline.UNCHANGED: unchanged,
    }


def run_episode(
    s: Settings,
    conn: sqlite3.Connection,
    eid: int,
    steps: list[str],
    force: bool = False,
    *,
    rebuild_index: bool = False,
    before_whisper: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """rebuild_index: recompute every embedding (after a change of the embedding model
    or fold()) and run the index step even if done. --force alone keeps do_index on
    its incremental path, so a forced retry of hollow chunks stays cheap.
    before_whisper: see do_transcribe."""
    vid = active_video_id(conn, eid)
    if vid is None:
        raise ValueError(f"episode {eid} not in the active catalog")
    results: dict[str, Any] = {}
    for step in pipeline.STEPS:
        if step not in steps:
            continue
        step_force = force or (rebuild_index and step == "index")
        if pipeline.will_run(conn, eid, step, vid, step_force):
            # Steps whose data this one reads must be in this call or done for this
            # video: after a re-point the old video's audio.wav and chunks.json are
            # still on disk, and transcribing them would label old text with the new id.
            for prev in pipeline.REQUIRES[step]:
                if prev not in steps and not pipeline.is_done(conn, eid, prev, vid):
                    raise ValueError(
                        f"episode {eid}: step {prev!r} is not done for video {vid}; "
                        f"run it first or add it to --steps"
                    )
            # Every step is heavy (CPU, network or shared services), so none may start in
            # the pause window; a step that will be skipped as done does not need to wait.
            wait_if_paused(s.pause_start, s.pause_end)
        if step == "download":
            fn = lambda: do_download(s, eid, vid)  # noqa: E731
        elif step == "prepare":
            fn = lambda: do_prepare(s, eid)  # noqa: E731
        elif step == "transcribe":
            fn = lambda: do_transcribe(s, conn, eid, vid, force, before_whisper)  # noqa: E731
        elif step == "speakers":
            fn = lambda: speakers.do_speakers(s, conn, eid, speakers.get_embedder(s))  # noqa: E731
        else:
            fn = lambda: index_build.do_index(s, conn, eid, rebuild=rebuild_index)  # noqa: E731
        results[step] = pipeline.run_step(conn, eid, step, vid, fn, force=step_force)
    return results


def polite_pause(s: Settings) -> None:
    lo, hi = s.download_pause_s
    time.sleep(random.uniform(lo, hi))
