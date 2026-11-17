import wave
from pathlib import Path

import numpy as np
import pytest
from test_enrich import SEED, _settings  # tests/ is on sys.path (pytest rootdir mode)

from dp.catalog.characters import load_seed, parse_seed
from dp.db import connect, migrate
from dp.transcribe import speakers

# Fake "voices": each actor is a constant signal level; the fake embedder maps a
# level to a direction, so the real profile and scoring code can be exercised.
LEVELS = {"cvarkov": 0.5, "torbica": -0.5, "boskic": 0.1}
DIRS = {0.5: [1, 0, 0, 0], -0.5: [0, 1, 0, 0], 0.1: [0, 0, 1, 0], 0.0: [0, 0, 0, 1]}


def fake_embed(pieces: list[np.ndarray]) -> np.ndarray:
    out = []
    for p in pieces:
        level = min(DIRS, key=lambda v: abs(v - float(np.median(p))))
        out.append(np.array(DIRS[level], dtype=np.float32) + 0.05)
    return speakers.normalize(np.stack(out))


def _write_wav(path: Path, spans: list[tuple[float, float, float]], total: float) -> None:
    x = np.zeros(int(total * speakers.SR), dtype=np.float32)
    for start, end, level in spans:
        x[int(start * speakers.SR) : int(end * speakers.SR)] = level
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(speakers.SR)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


def test_parse_clip() -> None:
    assert speakers.parse_clip("243 5:02-5:28") == (243, 302.0, 328.0)
    assert speakers.parse_clip("25 6:50 -7:41") == (25, 410.0, 461.0)
    for bad in ("243 5:28-5:02", "243 5:02", "x 1:00-2:00"):
        with pytest.raises(ValueError):
            speakers.parse_clip(bad)


def test_windows() -> None:
    x = np.arange(10 * speakers.SR, dtype=np.float32)
    ws = speakers.windows(x, 1.0, 7.0)  # 6 s span, 3 s windows, 1.5 s hop
    assert len(ws) == 3 and all(len(w) == 3 * speakers.SR for w in ws)
    assert len(speakers.windows(x, 1.0, 2.0)) == 1
    assert speakers.windows(x, 11.0, 12.0) == []


def test_score_keeps_closest_actor_unless_far() -> None:
    profiles = {"a": np.array([1.0, 0.0]), "b": np.array([0.0, 1.0])}
    embs = speakers.normalize(np.array([[1.0, 0.1], [0.3, 1.0], [1.0, -0.2], [-1.0, -0.2]]))
    labels = [x[0] for x in speakers.score(embs, profiles)]
    assert labels == ["a", "b", "a", speakers.OTHER]  # the last one is far from both


def test_profiles_and_speakers_step(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    conn = connect(s.db)
    migrate(conn)
    load_seed(conn, parse_seed(SEED))
    # Clip episode 1: each actor speaks 10 s.
    _write_wav(
        s.work_dir / "1" / "audio.wav",
        [(0, 10, 0.5), (10, 20, -0.5), (20, 30, 0.1)],
        30,
    )
    clips = tmp_path / "voices.yaml"
    clips.write_text(
        "cvarkov: ['1 0:00-0:10']\ntorbica: ['1 0:10-0:20']\nboskic: ['1 0:20-0:30']\n"
    )
    info = speakers.build_profiles(s, conn, fake_embed, clips)  # type: ignore[arg-type]
    assert {c["actor"]: c["correct"] for c in info["clips"]} == {
        "cvarkov": 1.0, "torbica": 1.0, "boskic": 1.0,
    }  # fmt: skip

    # Silence is never part of a profile.
    _write_wav(s.work_dir / "3" / "audio.wav", [], 10)
    clips.write_text("cvarkov: ['1 0:00-0:10']\ntorbica: ['3 0:02-0:08']\n")
    with pytest.raises(ValueError, match="silent"):
        speakers.build_profiles(s, conn, fake_embed, clips)  # type: ignore[arg-type]

    # Episode 2: torbica, then silence (another voice), then a 1 s interjection.
    _write_wav(s.work_dir / "2" / "audio.wav", [(0, 4, -0.5), (6, 7, 0.5)], 10)
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v', 't', '{}')")
    conn.execute("INSERT INTO episodes (id, video_id, title) VALUES (2, 'v', 't')")
    conn.executemany(
        "INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin, text_fold) "
        "VALUES (2, ?, ?, ?, 'x', 'x', 'x')",
        # The last one starts past the end of the 10 s audio (Whisper does that): with
        # almost no samples it must be skipped, not crash the voice model.
        [(0, 0, 4000), (1, 4000, 6000), (2, 6000, 7000), (3, 9_999, 12_000)],
    )
    res = speakers.do_speakers(s, conn, 2, fake_embed)  # type: ignore[arg-type]
    assert res["labelled"] == 2
    rows = conn.execute("SELECT actor FROM segments WHERE episode_id=2 ORDER BY idx")
    assert [r[0] for r in rows] == ["torbica", speakers.OTHER, None, None]
