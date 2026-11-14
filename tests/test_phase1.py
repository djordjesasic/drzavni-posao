import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from dp import pilot, pipeline
from dp.catalog.characters import load_seed, parse_seed
from dp.config import Settings
from dp.db import connect, migrate
from dp.schedule import TZ, seconds_until_resume
from dp.transcribe import quality, steps
from dp.transcribe.audio import parse_silences, plan_chunks
from dp.transcribe.steps import PROMPT_MAX_CHARS, split_points, whisper_prompt

SEED = Path(__file__).parent.parent / "seed" / "characters.yaml"


def _at(h: int, m: int) -> datetime:
    return datetime(2026, 11, 12, h, m, tzinfo=TZ)


def test_pause_window() -> None:
    assert seconds_until_resume(_at(18, 54), "18:55", "20:00") == 0
    assert seconds_until_resume(_at(18, 55), "18:55", "20:00") == 65 * 60
    assert seconds_until_resume(_at(19, 59), "18:55", "20:00") == 60
    assert seconds_until_resume(_at(20, 0), "18:55", "20:00") == 0
    # Window across midnight.
    assert seconds_until_resume(_at(23, 30), "23:00", "01:00") == 90 * 60
    assert seconds_until_resume(_at(0, 30), "23:00", "01:00") == 30 * 60


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = connect(tmp_path / "t.sqlite")
    migrate(c)
    for i in range(1, 121):
        c.execute("INSERT INTO videos (video_id, title, raw) VALUES (?, 't', '{}')", (f"v{i}",))
        c.execute(
            "INSERT INTO episodes (id, video_id, season, title) VALUES (?, ?, 1, 't')",
            (i, f"v{i}"),
        )
    c.commit()
    return c


def test_run_step_records_and_skips(conn) -> None:  # type: ignore[no-untyped-def]
    calls: list[int] = []

    def fn() -> dict[str, int]:
        calls.append(1)
        return {"n": len(calls)}

    assert pipeline.run_step(conn, 1, "download", "v1", fn) == {"n": 1}
    assert pipeline.run_step(conn, 1, "download", "v1", fn) is None  # done for this video
    assert pipeline.run_step(conn, 1, "download", "vX", fn) == {"n": 2}  # other video: redo
    row = conn.execute("SELECT status, video_id, elapsed_s FROM episode_steps").fetchone()
    assert (row["status"], row["video_id"]) == ("done", "vX")
    assert row["elapsed_s"] is not None


def test_run_step_records_failure(conn) -> None:  # type: ignore[no-untyped-def]
    def boom() -> dict[str, int]:
        raise RuntimeError("whisper down")

    with pytest.raises(RuntimeError):
        pipeline.run_step(conn, 2, "transcribe", "v2", boom)
    row = conn.execute("SELECT status, error FROM episode_steps").fetchone()
    assert row["status"] == "failed" and "whisper down" in row["error"]


def test_pilot_selection(conn) -> None:  # type: ignore[no-untyped-def]
    owner = [pilot.PilotEpisode(5, "owner_guests", "x"), pilot.PilotEpisode(100, "zap", None)]
    chosen = pilot.select(conn, owner)
    ids = [p.episode_id for p in chosen]
    assert len(ids) == len(set(ids)) == 32
    first = [p.episode_id for p in chosen if p.grp == "first_50"]
    assert len(first) == 10 and all(e <= 50 for e in first) and 5 not in first
    assert len([p for p in chosen if p.grp == "spread"]) == 20
    with pytest.raises(ValueError, match="not in the catalog"):
        pilot.select(conn, [pilot.PilotEpisode(9999, "owner_guests", None)])


def test_spread_is_even() -> None:
    assert pilot.spread(list(range(10)), 5) == [1, 3, 5, 7, 9]
    assert pilot.spread([1, 2], 5) == [1, 2]


def test_parse_silences_and_plan_chunks() -> None:
    stderr = (
        "[silencedetect] silence_start: 140.2\n[silencedetect] silence_end: 141.0 | x\n"
        "[silencedetect] silence_start: 290.0\n[silencedetect] silence_end: 291.0 | x\n"
    )
    quiet = parse_silences(stderr)
    assert quiet == [(140.2, 141.0), (290.0, 291.0)]
    chunks = plan_chunks(500.0, quiet, target=150, maximum=180)
    assert chunks == [(0.0, 140.6), (140.6, 290.5), (290.5, 470.5), (470.5, 500.0)]
    assert all(b - a <= 180 for a, b in chunks)
    assert plan_chunks(100.0, [], 150, 180) == [(0.0, 100.0)]


def test_plan_chunks_with_edges() -> None:
    quiet = [(27.0, 28.0), (140.2, 141.0), (290.0, 291.0), (466.0, 467.0)]
    chunks = plan_chunks(500.0, quiet, target=150, maximum=180, edge=30)
    # Intro and outro cut at the silences near 30 s from each end.
    assert chunks[0] == (0.0, 27.5) and chunks[-1] == (466.5, 500.0)
    assert all(b - a <= 180 for a, b in chunks)
    assert all(a == p[1] for p, (a, _) in zip(chunks, chunks[1:], strict=False))
    # No silence nearby: cut exactly at 30 s; short audio: no edge pieces.
    assert plan_chunks(500.0, [], 150, 180, edge=30)[0] == (0.0, 30.0)
    assert plan_chunks(100.0, [], 150, 180, edge=30) == [(0.0, 100.0)]


def test_whisper_prompt_fits(conn) -> None:  # type: ignore[no-untyped-def]
    load_seed(conn, parse_seed(SEED))
    prompt = whisper_prompt(conn)
    assert len(prompt) <= PROMPT_MAX_CHARS + 1
    assert prompt.startswith("Državni posao. Boškić") or "Čvarkov" in prompt


def test_whisper_prompt_has_main_full_name_aliases(conn) -> None:  # type: ignore[no-untyped-def]
    load_seed(conn, parse_seed(SEED))
    prompt = whisper_prompt(conn)
    assert "Dragan Jove Torbica" in prompt
    assert prompt.index("Dragan Jove Torbica") < prompt.index("Jagoda")


def _seg(text: str, start: float = 0.0, end: float = 1.0) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text}


def test_quality_detects_loops_and_swallowed_speech() -> None:
    assert quality.is_suspect([_seg("Uvijek sam u 11.")] * 4, 20.0)
    assert not quality.is_suspect([_seg("Ustanite.")] * 3, 20.0)  # real dialogue
    # 150 s piece with one short line: speech was swallowed.
    assert quality.is_suspect([_seg("Hvala vam.")], 150.0)
    # Short piece (intro music) is not judged by text amount.
    assert not quality.is_suspect([_seg("Hvala vam.")], 30.0)
    normal = [_seg(f"Rečenica broj {i} koja ima normalnu dužinu.") for i in range(60)]
    assert not quality.is_suspect(normal, 150.0)
    # A B A B loop of longer lines.
    a, b = _seg("Tamo će mi se smrznuti kad je ona tvoja."), _seg("Jel' šta, ja sam se prepao.")
    assert quality.is_suspect([a, b, a, b, a], 30.0)
    assert not quality.is_suspect([_seg("Dobro."), _seg("Ne."), _seg("Dobro."), _seg("Dobro!"),
                                   _seg("Dobro.")], 30.0)  # fmt: skip
    # One short line over a whole 30 s piece.
    assert quality.is_suspect([_seg("Hvala vam.", 0, 30)], 30.0)


def test_quality_clean() -> None:
    segs = [
        _seg("🎵🎵🎵", 0, 5),
        _seg("Top 10 najboljih krišnjaka na svijetu.", 5, 30),
        _seg("Ivana Korom Reviewer: Ivana Korom", 30, 31),
        _seg("...", 31, 32),
        _seg("Dobro.", 32, 33),
        _seg("Dobro.", 33, 34),
        _seg("Ne možemo preko vremena!", 34, 36),
        _seg("Hvala vam.", 36, 37),
        _seg("Uvijek se sviđa.", 40, 70),
    ]
    out = quality.clean(segs)
    assert [g["text"] for g in out] == ["Dobro.", "Ne možemo preko vremena!", "Hvala vam."]
    assert (out[0]["start"], out[0]["end"]) == (32, 34)


def test_split_points() -> None:
    assert split_points(0, 100, 30) == [0, 30, 60, 90, 100]
    assert split_points(0, 92, 30) == [0, 30, 60, 92]  # 2 s tail joins the last piece
    assert split_points(10, 25, 30) == [10, 25]


def test_transcribe_retries_loops_and_reuses_good_chunks(
    tmp_path: Path,
    conn,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = Settings(
        youtube_channel="", data_dir=tmp_path, db=tmp_path / "t.sqlite", catalog_dir=tmp_path,
        seed_characters=SEED, seed_pilot=tmp_path, whisper_url="http://w", llama_url="http://l",
        pause_start="00:00", pause_end="00:00", api_allowed_hosts=[], api_token=None,
        download_pause_s=(0, 0), language="sr", chunk_target_s=150, chunk_max_s=180,
    )  # fmt: skip
    work = s.work_dir / "1"
    work.mkdir(parents=True)
    (work / "chunks.json").write_text(json.dumps([[0, 90], [90, 180]]))
    calls: list[tuple[float, float, str]] = []
    span = {}

    def fake_cut(src: Path, a: float, b: float, dst: Path) -> None:
        span["ab"] = (a, b)
        dst.write_bytes(b"")

    def fake_whisper(url: str, wav: Path, lang: str, prompt: str) -> dict[str, Any]:
        a, b = span["ab"]
        calls.append((a, b, "p" if prompt else "-"))
        if (a, b) == (0, 90):  # first chunk loops when sent whole
            return {"segments": [{"start": 0, "end": 30, "text": "Hvala vam."}] * 4}
        if (a, b) == (0, 30):  # intro music: one invented line over it, with or without prompt
            return {"segments": [{"start": 0, "end": 30, "text": "Uvijek se sviđa."}]}
        n = int((b - a) // 3)
        return {"segments": [
            {"start": i * 3, "end": i * 3 + 3, "text": f"Replika {a + i * 3:.0f} sa dosta teksta."}
            for i in range(n)
        ]}  # fmt: skip

    monkeypatch.setattr(steps.audio, "cut", fake_cut)
    monkeypatch.setattr(steps.whisper, "transcribe", fake_whisper)
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: None)

    info = steps.do_transcribe(s, conn, 1)
    assert info["chunks_retried"] == 1 and info["chunks_reused"] == 0
    assert calls == [
        (0, 90, "p"), (0, 30, "p"), (0, 30, "-"), (30, 60, "p"), (60, 90, "p"), (90, 180, "p")
    ]  # fmt: skip
    texts = [r["text"] for r in conn.execute("SELECT text FROM segments ORDER BY idx")]
    assert "Hvala vam." not in texts and "Uvijek se sviđa." not in texts
    assert texts[0] == "Replika 30 sa dosta teksta."

    calls.clear()
    # Second run: chunk 2 was good, chunk 1 was already repaired (its leftover
    # stretched line is dropped by clean); neither goes to Whisper again.
    info = steps.do_transcribe(s, conn, 1)
    assert calls == [] and info["chunks_reused"] == 2
    assert [r["text"] for r in conn.execute("SELECT text FROM segments ORDER BY idx")] == texts
