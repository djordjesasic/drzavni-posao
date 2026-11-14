"""Who speaks: ECAPA voice embeddings compared with profiles of the three main actors.

The voice gives the actor, not the character: an actor also plays other roles and
imitates people, so the character is decided later from context (enrichment).
"""

import logging
import re
import sqlite3
import wave
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from dp.config import Settings

log = logging.getLogger(__name__)

SR = 16000
ECAPA_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"
TORCH_THREADS = 4  # the mini PC is shared; leave most cores to others
# Profiles: mean of embeddings over short windows of the owner's clips.
WINDOW_S = 3.0
HOP_S = 1.5
# Profile windows quieter than this (pauses between lines) are skipped; speech in
# the series is around 0.05-0.15 RMS, pauses below 0.02.
MIN_RMS = 0.02
# Segments shorter than this are left unlabelled (PLAN: short interjections).
MIN_SEGMENT_S = 1.5
MAX_SEGMENT_S = 20.0
# Below MIN_SCORE the segment is not a main actor's voice (music, ads, guests).
# Owner check of 3 pilot episodes: an actor label was right 349/353 times, while
# "other" at 0.35 was wrong 40/46 times (changed voice: drugged, on the phone,
# imitating), so low scores still keep the closest actor, marked as unsure.
MIN_SCORE = 0.15
MIN_MARGIN = 0.0
SURE_SCORE = 0.35  # below this the label is a guess (shown as "cvarkov?" to the LLM)
OTHER = "other"

_CLIP = re.compile(r"^\s*(\d+)\s+(\d+):(\d{2})\s*-\s*(\d+):(\d{2})\s*$")


def parse_clip(line: str) -> tuple[int, float, float]:
    """'243 5:02-5:28' -> (243, 302.0, 328.0)."""
    m = _CLIP.match(line)
    if not m:
        raise ValueError(f"bad clip {line!r}, expected '<episode> m:ss-m:ss'")
    eid, m1, s1, m2, s2 = (int(x) for x in m.groups())
    start, end = m1 * 60 + s1, m2 * 60 + s2
    if end <= start:
        raise ValueError(f"clip {line!r} ends before it starts")
    return eid, float(start), float(end)


def load_clips(path: Path) -> dict[str, list[tuple[int, float, float]]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {actor: [parse_clip(c) for c in clips] for actor, clips in raw.items()}


def read_wav(path: Path) -> np.ndarray:
    """16 kHz mono PCM16 WAV (from the prepare step) as float32 in [-1, 1]."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16 kHz mono PCM16")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def windows(
    x: np.ndarray, start: float, end: float, win: float = WINDOW_S, hop: float = HOP_S
) -> list[np.ndarray]:
    """Overlapping windows of x[start:end] (seconds); a short span is one window."""
    a, b = int(start * SR), min(int(end * SR), len(x))
    w, h = int(win * SR), int(hop * SR)
    if b - a <= w:
        return [x[a:b]] if b > a else []
    return [x[i : i + w] for i in range(a, b - w + 1, h)]


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2))) if len(x) else 0.0


def normalize(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


class Embedder:
    """ECAPA (speechbrain) on the CPU; the model is fetched once into cache_dir."""

    def __init__(self, cache_dir: Path) -> None:
        import torch
        from speechbrain.inference.speaker import EncoderClassifier

        torch.set_num_threads(TORCH_THREADS)
        self._torch = torch
        self._model = EncoderClassifier.from_hparams(
            source=ECAPA_SOURCE, savedir=str(cache_dir), run_opts={"device": "cpu"}
        )

    def __call__(self, pieces: list[np.ndarray]) -> np.ndarray:
        """(n, 192) L2-normalized embeddings, one per piece."""
        out = []
        with self._torch.inference_mode():
            for p in pieces:
                e = self._model.encode_batch(self._torch.from_numpy(np.ascontiguousarray(p))[None])
                out.append(e.reshape(-1).numpy())
        return normalize(np.stack(out))


_embedder: Embedder | None = None


def get_embedder(s: Settings) -> Embedder:
    """One model per process (loading takes a few seconds)."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder(s.data_dir / "models" / "ecapa")
    return _embedder


def score(embs: np.ndarray, profiles: dict[str, np.ndarray]) -> list[tuple[str, float, float]]:
    """Per embedding: (best actor or OTHER, best cosine, margin over the runner-up)."""
    names = list(profiles)
    sims = embs @ np.stack([profiles[n] for n in names]).T
    out = []
    for row in sims:
        order = np.argsort(row)[::-1]
        best, margin = float(row[order[0]]), float(row[order[0]] - row[order[1]])
        actor = names[order[0]] if best >= MIN_SCORE and margin >= MIN_MARGIN else OTHER
        out.append((actor, best, margin))
    return out


def clip_embeddings(
    s: Settings, embed: Embedder, clips: dict[str, list[tuple[int, float, float]]]
) -> dict[str, list[np.ndarray]]:
    """actor -> one (n_windows, 192) array per clip."""
    out: dict[str, list[np.ndarray]] = {}
    audio: dict[int, np.ndarray] = {}
    for actor, items in clips.items():
        for eid, start, end in items:
            if eid not in audio:
                wav = s.work_dir / str(eid) / "audio.wav"
                if not wav.exists():
                    raise FileNotFoundError(f"{wav}: run download,prepare for episode {eid}")
                audio[eid] = read_wav(wav)
            ws = [w for w in windows(audio[eid], start, end) if rms(w) >= MIN_RMS]
            if not ws:
                raise ValueError(f"clip {eid} {start}-{end} of {actor} is silent")
            out.setdefault(actor, []).append(embed(ws))
    return out


def build_profiles(
    s: Settings, conn: sqlite3.Connection, embed: Embedder, clips_path: Path
) -> dict[str, Any]:
    """Store one profile per actor; report how well each clip matches its own actor
    when left out of that actor's profile (a quick sanity check of the clips)."""
    clips = load_clips(clips_path)
    per_clip = clip_embeddings(s, embed, clips)
    profiles = {a: normalize(np.concatenate(cs).mean(axis=0)) for a, cs in per_clip.items()}

    check = []
    for actor, cs in per_clip.items():
        for i, c in enumerate(cs):
            rest = [x for j, x in enumerate(cs) if j != i]
            held = dict(profiles)
            if rest:
                held[actor] = normalize(np.concatenate(rest).mean(axis=0))
            labels = score(c, held)
            eid, start, end = clips[actor][i]
            check.append(
                {
                    "actor": actor,
                    "clip": f"{eid} {int(start // 60)}:{int(start % 60):02d}",
                    "windows": len(c),
                    "correct": round(sum(lab[0] == actor for lab in labels) / len(labels), 2),
                    "mean_score": round(float(np.mean([lab[1] for lab in labels])), 3),
                }
            )
    names = list(profiles)
    between = {
        f"{a}~{b}": round(float(profiles[a] @ profiles[b]), 3)
        for i, a in enumerate(names)
        for b in names[i + 1 :]
    }
    with conn:
        conn.execute("DELETE FROM voice_profiles")
        for actor, p in profiles.items():
            n = sum(len(c) for c in per_clip[actor])
            secs = sum(e - st for _, st, e in clips[actor])
            conn.execute(
                "INSERT INTO voice_profiles (actor, embedding, windows, seconds) "
                "VALUES (?, ?, ?, ?)",
                (actor, p.astype(np.float32).tobytes(), n, secs),
            )
    return {"profiles": names, "between_profiles": between, "clips": check}


def load_profiles(conn: sqlite3.Connection) -> dict[str, np.ndarray]:
    rows = conn.execute("SELECT actor, embedding FROM voice_profiles ORDER BY actor").fetchall()
    if not rows:
        raise RuntimeError("no voice profiles; run `dp.cli voice-profiles` first")
    return {r[0]: np.frombuffer(r[1], dtype=np.float32) for r in rows}


def do_speakers(s: Settings, conn: sqlite3.Connection, eid: int, embed: Embedder) -> dict[str, Any]:
    """Label every segment of at least MIN_SEGMENT_S with an actor (or OTHER)."""
    profiles = load_profiles(conn)
    x = read_wav(s.work_dir / str(eid) / "audio.wav")
    segs = conn.execute(
        "SELECT idx, start_ms, end_ms FROM segments WHERE episode_id=? ORDER BY idx", (eid,)
    ).fetchall()
    long = [r for r in segs if (r["end_ms"] - r["start_ms"]) / 1000 >= MIN_SEGMENT_S]
    pieces = []
    for r in long:
        start = r["start_ms"] / 1000
        end = min(r["end_ms"] / 1000, start + MAX_SEGMENT_S)
        pieces.append(x[int(start * SR) : int(end * SR)])
    labels = score(embed(pieces), profiles) if pieces else []
    with conn:
        conn.execute("UPDATE segments SET actor=NULL, actor_score=NULL WHERE episode_id=?", (eid,))
        conn.executemany(
            "UPDATE segments SET actor=?, actor_score=? WHERE episode_id=? AND idx=?",
            [(a, round(sc, 3), eid, r["idx"]) for r, (a, sc, _) in zip(long, labels, strict=True)],
        )
    counts: dict[str, int] = {}
    for a, _, _ in labels:
        counts[a] = counts.get(a, 0) + 1
    return {
        "segments": len(segs),
        "labelled": len(long),
        "actors": dict(sorted(counts.items())),
    }
