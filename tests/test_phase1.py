import json
import sqlite3
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

SEED = Path(__file__).parent.parent / "series" / "drzavni-posao" / "seed" / "characters.yaml"


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
    prompt = whisper_prompt(conn, "Državni posao. ")
    assert len(prompt) <= PROMPT_MAX_CHARS + 1
    assert prompt.startswith("Državni posao. Boškić") or "Čvarkov" in prompt


def test_whisper_prompt_has_main_full_name_aliases(conn) -> None:  # type: ignore[no-untyped-def]
    load_seed(conn, parse_seed(SEED))
    prompt = whisper_prompt(conn, "Državni posao. ")
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
        seed_characters=tmp_path / "seed" / "characters.yaml", seed_pilot=tmp_path,
        whisper_url="http://w", llama_url="http://l",
        pause_start="00:00", pause_end="00:00", api_allowed_hosts=[], api_token=None,
        download_pause_s=(0, 0), language="sr", chunk_target_s=150, chunk_max_s=180,
        locks_dir=tmp_path,
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

    info = steps.do_transcribe(s, conn, 1, "v1")
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
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert calls == [] and info["chunks_reused"] == 2
    assert [r["text"] for r in conn.execute("SELECT text FROM segments ORDER BY idx")] == texts


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        youtube_channel="", data_dir=tmp_path, db=tmp_path / "t.sqlite", catalog_dir=tmp_path,
        seed_characters=tmp_path / "seed" / "characters.yaml", seed_pilot=tmp_path,
        whisper_url="http://w", llama_url="http://l",
        pause_start="00:00", pause_end="00:00", api_allowed_hosts=[], api_token=None,
        download_pause_s=(0, 0), language="sr", chunk_target_s=150, chunk_max_s=180,
        locks_dir=tmp_path,
    )  # fmt: skip


def _mock_whisper(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[tuple[float, float, str]]:
    """Fake cut + Whisper; answer(a, b, prompt) gives the segments for piece [a, b]."""
    calls: list[tuple[float, float, str]] = []
    span = {}

    def fake_cut(src: Path, a: float, b: float, dst: Path) -> None:
        span["ab"] = (a, b)
        dst.write_bytes(b"")

    def fake_whisper(url: str, wav: Path, lang: str, prompt: str) -> dict[str, Any]:
        a, b = span["ab"]
        calls.append((a, b, "p" if prompt else "-"))
        return {"segments": answer(a, b, prompt)}

    monkeypatch.setattr(steps.audio, "cut", fake_cut)
    monkeypatch.setattr(steps.whisper, "transcribe", fake_whisper)
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: None)
    return calls


def _dialogue(a: float, b: float, prompt: str = "") -> list[dict[str, Any]]:
    return [
        {"start": i * 3, "end": i * 3 + 3, "text": f"Replika {a + i * 3:.0f} sa dosta teksta."}
        for i in range(int((b - a) // 3))
    ]


def _done(conn: sqlite3.Connection, vid: str, *names: str, eid: int = 1) -> None:
    for name in names:
        pipeline.run_step(conn, eid, name, vid, lambda: {}, force=True)


def _chunks(s: Settings, eid: int, chunks: list[list[float]]) -> None:
    work = s.work_dir / str(eid)
    work.mkdir(parents=True, exist_ok=True)
    (work / "chunks.json").write_text(json.dumps(chunks))


def test_whisper_error_body_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Resp:
        def __init__(self, body: Any) -> None:
            self.body = body

        def raise_for_status(self) -> None:
            pass

        def json(self) -> Any:
            return self.body

    wav = tmp_path / "x.wav"
    wav.write_bytes(b"")
    body: Any = {"error": "failed to process audio"}
    monkeypatch.setattr(steps.whisper.httpx, "post", lambda *a, **k: Resp(body))
    with pytest.raises(steps.whisper.WhisperError, match="failed to process audio"):
        steps.whisper.transcribe("http://w", wav, "sr", "")
    body = {"text": ""}  # no segments list at all
    with pytest.raises(steps.whisper.WhisperError):
        steps.whisper.transcribe("http://w", wav, "sr", "")
    body = {"text": "", "segments": []}  # silence is a valid, empty transcript
    assert steps.whisper.transcribe("http://w", wav, "sr", "")["segments"] == []


def test_hollow_repair_is_not_reused(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 30], [30, 150], [150, 300]])

    def answer(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        if b <= 150:  # silent intro, then speech Whisper keeps swallowing, even in pieces
            return []
        return _dialogue(a, b)

    calls = _mock_whisper(monkeypatch, answer)
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert info["chunks_retried"] == 1 and info["chunks_unresolved"] == 1
    doc = json.loads((s.transcripts_dir / "1.json").read_text())
    assert [ch["retried"] for ch in doc["chunks"]] == [False, True, False]
    assert [ch["unresolved"] for ch in doc["chunks"]] == [False, True, False]

    # A plain run (e.g. resuming after a failure) reuses the unresolved chunk as it is.
    calls.clear()
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert calls == [] and info["chunks_reused"] == 3 and info["chunks_unresolved"] == 1

    # Forced: the silent 30 s intro and the good chunk are reused (no endless retry),
    # the hollow chunk goes to Whisper again.
    calls.clear()
    info = steps.do_transcribe(s, conn, 1, "v1", force=True)
    assert info["chunks_reused"] == 2 and info["chunks_unresolved"] == 1
    assert {c[:2] for c in calls} == {(30, 150), (30, 60), (60, 90), (90, 120), (120, 150)}

    # An older file marks a retry only by its retry list (legacy shape, no flags): the
    # hollow one is recognised as unresolved all the same.
    for ch in doc["chunks"]:
        ch.pop("retried", None)
        ch.pop("unresolved", None)
    (s.transcripts_dir / "1.json").write_text(json.dumps(doc))
    calls.clear()
    assert steps.do_transcribe(s, conn, 1, "v1")["chunks_unresolved"] == 1 and calls == []
    (s.transcripts_dir / "1.json").write_text(json.dumps(doc))
    steps.do_transcribe(s, conn, 1, "v1", force=True)
    assert (30, 150, "p") in calls and all(a >= 30 and b <= 150 for a, b, _ in calls)

    # Once the 30 s pieces do hear the speech, the repair holds and is reused from then on.
    def fixed(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        if (a, b) == (30, 150):
            return []
        return [] if b <= 30 else _dialogue(a, b)

    calls = _mock_whisper(monkeypatch, fixed)
    info = steps.do_transcribe(s, conn, 1, "v1", force=True)
    assert info["chunks_retried"] == 1 and info["chunks_unresolved"] == 0
    calls.clear()
    assert steps.do_transcribe(s, conn, 1, "v1")["chunks_reused"] == 3 and calls == []


def test_whisper_lock_held_per_piece_and_not_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl
    import os
    import threading
    from types import SimpleNamespace

    s = _settings(tmp_path)
    lock = tmp_path / steps.WHISPER_LOCK
    seen: dict[str, Any] = {}
    cut_done, released = threading.Event(), threading.Event()
    clock = [0.0]  # fake monotonic clock: no wall-clock thresholds on a busy machine
    # A piece file another container left in the same work dir (its python is PID 1 too).
    stranger = tmp_path / "piece-1.wav"
    stranger.write_bytes(b"other audio")

    def fake_cut(src: Path, a: float, b: float, dst: Path) -> None:
        assert dst.exists()  # created by mkstemp; ffmpeg -y overwrites it
        seen.setdefault("pieces", []).append(dst.name)
        dst.write_bytes(b"")
        cut_done.set()

    def fake_whisper(url: str, wav: Path, lang: str, prompt: str) -> dict[str, Any]:
        # Another process (a separate open file) must not get the lock meanwhile.
        fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        assert released.is_set()  # the request only went out after the other holder let go
        clock[0] += 1.0  # the request itself takes 1 s
        return {"segments": []}

    monkeypatch.setattr(steps.audio, "cut", fake_cut)
    monkeypatch.setattr(steps.whisper, "transcribe", fake_whisper)
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(steps, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    tr = steps._Transcriber(s, tmp_path / "a.wav", tmp_path, "")

    # Someone else holds the lock: the piece waits for it, but not on Whisper's clock.
    other = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(other, fcntl.LOCK_EX)
    results: list[Any] = []
    worker = threading.Thread(target=lambda: results.append(tr.piece(0, 30, "")))
    worker.start()
    assert cut_done.wait(timeout=30)  # the piece is cut; next it goes for the lock
    clock[0] += 100.0  # time spent waiting for the lock
    released.set()
    os.close(other)
    worker.join(timeout=60)
    assert results == [[]] and tr.calls == 1
    assert tr.busy == 1.0  # only the request, not the 100 s lock wait
    assert tr.piece(30, 60, "") == []
    names = seen["pieces"]
    assert len(set(names)) == 2 and all(
        n.startswith("piece-") and n.endswith(".wav") for n in names
    )
    assert stranger.name not in names and stranger.read_bytes() == b"other audio"
    assert not any((tmp_path / n).exists() for n in names)

    # Released right after the response: free again between pieces.
    fd = os.open(lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.close(fd)


def test_run_episode_waits_for_pause_before_each_step(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path)
    log: list[str] = []
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: log.append("wait"))
    monkeypatch.setattr(steps, "do_download", lambda *a: log.append("download") or {})
    monkeypatch.setattr(steps, "do_prepare", lambda *a: log.append("prepare") or {})
    monkeypatch.setattr(steps, "do_transcribe", lambda *a: log.append("transcribe") or {})
    monkeypatch.setattr(steps.speakers, "get_embedder", lambda s: None)
    monkeypatch.setattr(steps.speakers, "do_speakers", lambda *a: log.append("speakers") or {})
    monkeypatch.setattr(steps.index_build, "do_index", lambda *a, **k: log.append("index") or {})

    steps.run_episode(s, conn, 1, list(pipeline.STEPS))
    assert log == [x for step in pipeline.STEPS for x in ("wait", step)]
    # Steps already done for this video are skipped without waiting.
    log.clear()
    steps.run_episode(s, conn, 1, list(pipeline.STEPS))
    assert log == []
    log.clear()
    steps.run_episode(s, conn, 1, ["prepare"], force=True)
    assert log == ["wait", "prepare"]


def test_transcript_reused_only_for_same_video(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 30], [30, 120]])
    calls = _mock_whisper(monkeypatch, _dialogue)
    old = [{"start": 0, "end": 3, "text": "Tekst starog videa."}]
    legacy = {  # pilot transcript, written before video_id was stored
        "episode_id": 1, "prompt": "", "segments": old,
        "chunks": [{"start": 0, "end": 30, "first_try": old, "retry": None, "segments": old},
                   {"start": 30, "end": 120, "first_try": [], "retry": None,
                    "segments": _dialogue(30, 120)}],
    }  # fmt: skip
    s.transcripts_dir.mkdir(parents=True)
    out = s.transcripts_dir / "1.json"
    out.write_text(json.dumps(legacy))

    # Unlabelled: not reused until `dp label-transcripts` vouches for it, which it does
    # when transcribe is recorded done for this very video.
    _done(conn, "v1", "download", "prepare", "transcribe")
    assert steps.label_transcripts(s, conn)["labelled"] == 1
    steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert calls == []
    assert json.loads(out.read_text())["video_id"] == "v1"

    # Same unlabeled file, but transcribe was last done for another video: ignored.
    out.write_text(json.dumps(legacy))
    conn.execute("UPDATE episode_steps SET video_id='v0' WHERE episode_id=1 AND step='transcribe'")
    conn.commit()
    assert steps.label_transcripts(s, conn)["labelled"] == 0
    steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert [c[:2] for c in calls] == [(0, 30), (30, 120)]

    # Episode re-pointed to another video with the same chunk plan: nothing carried over,
    # even with --force; then the new transcript is reused for the new video.
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('vNew', 't', '{}')")
    conn.execute("UPDATE episodes SET video_id='vNew' WHERE id=1")
    conn.commit()
    _done(conn, "vNew", "download", "prepare")
    calls.clear()
    steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert [c[:2] for c in calls] == [(0, 30), (30, 120)]
    assert json.loads(out.read_text())["video_id"] == "vNew"
    calls.clear()
    steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert calls == []


def test_transcribe_keeps_finished_chunks_after_failure(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 90], [90, 180], [180, 270]])
    fail = {"on": True}

    def answer(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        if (a, b) == (180, 270) and fail["on"]:
            raise steps.whisper.WhisperError("failed to process audio")
        return _dialogue(a, b)

    calls = _mock_whisper(monkeypatch, answer)
    with pytest.raises(steps.whisper.WhisperError):
        steps.do_transcribe(s, conn, 1, "v1")
    out = s.transcripts_dir / "1.json"
    doc = json.loads(out.read_text())
    assert doc["partial"] is True and doc["video_id"] == "v1"
    assert [(ch["start"], ch["end"]) for ch in doc["chunks"]] == [(0, 90), (90, 180)]
    assert list(s.transcripts_dir.iterdir()) == [out]  # no temp files left behind
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 0

    # The next run sends only the chunk that failed.
    fail["on"] = False
    calls.clear()
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert calls == [(180, 270, "p")] and info["chunks_reused"] == 2
    assert "partial" not in json.loads(out.read_text())


def test_forced_transcribe_without_changes_keeps_speakers(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 90], [90, 180]])
    calls = _mock_whisper(monkeypatch, _dialogue)
    monkeypatch.setattr(steps.speakers, "get_embedder", lambda s: None)

    def label(*a: Any) -> dict[str, Any]:
        with conn:
            conn.execute("UPDATE segments SET actor='torbica' WHERE episode_id=1")
        return {}

    monkeypatch.setattr(steps.speakers, "do_speakers", label)
    monkeypatch.setattr(steps.index_build, "do_index", lambda *a, **k: {})
    _done(conn, "v1", "download", "prepare")
    all_steps = ["transcribe", "speakers", "index"]
    steps.run_episode(s, conn, 1, all_steps)

    def status() -> dict[str, str]:
        rows = conn.execute("SELECT step, status FROM episode_steps WHERE episode_id=1")
        return {r["step"]: r["status"] for r in rows if r["step"] in all_steps}

    def actors() -> set[str | None]:
        return {r[0] for r in conn.execute("SELECT actor FROM segments WHERE episode_id=1")}

    assert actors() == {"torbica"}
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data, video_id) "
        "SELECT 1, 'sonnet', 2, 'done', '{}', video_id FROM episodes WHERE id=1"
    )
    conn.commit()

    def description() -> str:
        return conn.execute("SELECT status FROM enrichment WHERE episode_id=1").fetchone()[0]

    # Forced rerun, every chunk reused, same text: labels kept, later steps stay done.
    calls.clear()
    res = steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert calls == [] and res["transcribe"]["unchanged"] is True
    assert actors() == {"torbica"}
    assert status() == {"transcribe": "done", "speakers": "done", "index": "done"}
    info = conn.execute("SELECT info FROM episode_steps WHERE step='transcribe'").fetchone()
    assert json.loads(info[0])["unchanged"] is True
    assert description() == "done"

    # A forced rerun that changes the text rewrites the rows and invalidates later steps.
    out = s.transcripts_dir / "1.json"
    doc = json.loads(out.read_text())
    doc["chunks"] = doc["chunks"][:1]
    out.write_text(json.dumps(doc))
    monkeypatch.setattr(
        steps.whisper, "transcribe", lambda *a: {"segments": [_seg("Nova replika.", 0, 3)]}
    )
    res = steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert res["transcribe"]["unchanged"] is False
    assert actors() == {None}
    assert status() == {"transcribe": "done", "speakers": "pending", "index": "pending"}
    # ... and the description made from the old text is marked for redoing.
    assert description() == "stale"


def test_partial_transcript_keeps_unreached_good_chunks(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    plan = [[i * 90, (i + 1) * 90] for i in range(10)]
    _chunks(s, 1, plan)
    hollow = {180, 540}  # chunks 2 and 6: Whisper keeps swallowing their speech
    fail = {"at": None}

    def answer(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        chunk = a // 90 * 90
        if chunk == fail["at"]:
            raise steps.whisper.WhisperError("failed to process audio")
        return [] if chunk in hollow else _dialogue(a, b)

    calls = _mock_whisper(monkeypatch, answer)
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert info["chunks_unresolved"] == 2
    out = s.transcripts_dir / "1.json"

    # Forced rerun: chunk 2 is redone, then Whisper fails on chunk 6.
    fail["at"] = 540
    calls.clear()
    with pytest.raises(steps.whisper.WhisperError):
        steps.do_transcribe(s, conn, 1, "v1", force=True)
    doc = json.loads(out.read_text())
    assert doc["partial"] is True
    # The partial file still knows every chunk of the previous file not reached yet:
    # the good 3-5 and 7-9, and the unresolved 6 (a plain run reuses that too).
    assert sorted(ch["start"] for ch in doc["chunks"]) == [i * 90 for i in range(10)]

    # A plain resume repeats nothing; a forced run sends only the two bad chunks.
    fail["at"] = None
    calls.clear()
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert calls == [] and info["chunks_reused"] == 10 and info["chunks_unresolved"] == 2
    info = steps.do_transcribe(s, conn, 1, "v1", force=True)
    assert info["chunks_reused"] == 8
    assert {a // 90 * 90 for a, _, _ in calls} == hollow


def test_unchanged_only_if_transcribe_was_done_before(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 90], [90, 180]])
    calls = _mock_whisper(monkeypatch, _dialogue)
    monkeypatch.setattr(steps.speakers, "get_embedder", lambda s: None)
    monkeypatch.setattr(steps.speakers, "do_speakers", lambda *a: {})
    monkeypatch.setattr(steps.index_build, "do_index", lambda *a, **k: {})
    _done(conn, "v1", "download", "prepare")
    steps.run_episode(s, conn, 1, ["transcribe", "speakers", "index"])

    # A run that rewrote the rows died before run_step recorded done: still 'running'.
    conn.execute("UPDATE episode_steps SET status='running' WHERE step='transcribe'")
    conn.commit()
    calls.clear()
    res = steps.run_episode(s, conn, 1, ["transcribe"])
    # do_transcribe only reports that the rows are identical; run_step does not honour
    # it because transcribe was not done when this run started.
    assert calls == [] and res["transcribe"]["unchanged"] is True
    status = {r[0]: r[1] for r in conn.execute("SELECT step, status FROM episode_steps")}
    assert status["speakers"] == status["index"] == "pending"


def test_run_episode_requires_earlier_steps_for_current_video(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    ran: list[str] = []
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: None)
    for name in ("do_download", "do_prepare", "do_transcribe"):
        monkeypatch.setattr(steps, name, lambda *a, n=name: ran.append(n) or {})
    monkeypatch.setattr(steps.speakers, "get_embedder", lambda s: None)
    monkeypatch.setattr(steps.speakers, "do_speakers", lambda *a: ran.append("speakers") or {})
    monkeypatch.setattr(steps.index_build, "do_index", lambda *a, **k: ran.append("index") or {})

    # Fresh episode, full run: fine.
    steps.run_episode(s, conn, 1, list(pipeline.STEPS))
    assert len(ran) == 5
    # Only a later step, earlier ones done for this video: fine (the pilot case).
    ran.clear()
    steps.run_episode(s, conn, 1, ["speakers"], force=True)
    steps.run_episode(s, conn, 1, ["index"])  # pending after speakers
    assert ran == ["speakers", "index"]
    # Voice-clip episodes: download and prepare only.
    steps.run_episode(s, conn, 2, ["download", "prepare"])

    # Re-pointed: old audio and chunks are on disk, but nothing is done for the new video.
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('vNew', 't', '{}')")
    conn.execute("UPDATE episodes SET video_id='vNew' WHERE id=1")
    conn.commit()
    ran.clear()
    with pytest.raises(ValueError, match=r"episode 1: step 'prepare' is not done for video vNew"):
        steps.run_episode(s, conn, 1, ["transcribe", "speakers", "index"])
    assert ran == []
    steps.run_episode(s, conn, 1, list(pipeline.STEPS))
    assert len(ran) == 5


def test_steps_require_only_their_data(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    ran: list[str] = []
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: None)
    for name in ("do_download", "do_prepare", "do_transcribe"):
        monkeypatch.setattr(steps, name, lambda *a, n=name: ran.append(n) or {})
    monkeypatch.setattr(steps.speakers, "get_embedder", lambda s: None)
    monkeypatch.setattr(steps.speakers, "do_speakers", lambda *a: ran.append("speakers") or {})
    monkeypatch.setattr(steps.index_build, "do_index", lambda *a, **k: ran.append("index") or {})

    # index needs the transcript, not the speaker labels.
    steps.run_episode(s, conn, 1, ["download", "prepare", "transcribe", "index"])
    assert ran == ["do_download", "do_prepare", "do_transcribe", "index"]
    # speakers needs prepare and transcribe, not download as such.
    conn.execute("UPDATE episode_steps SET status='failed' WHERE episode_id=1 AND step='download'")
    conn.commit()
    ran.clear()
    steps.run_episode(s, conn, 1, ["speakers"])
    assert ran == ["speakers"]
    # transcribe reads prepare's output only: a failed re-download does not block it.
    ran.clear()
    steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert ran == ["do_transcribe"]
    # index without a transcript for this video is refused.
    with pytest.raises(ValueError, match=r"episode 2: step 'transcribe' is not done"):
        steps.run_episode(s, conn, 2, ["index"])
    with pytest.raises(ValueError, match=r"episode 2: step 'download' is not done"):
        steps.run_episode(s, conn, 2, ["prepare"])
    assert set(pipeline.REQUIRES) == set(pipeline.STEPS)
    assert all(
        pipeline.STEPS.index(r) < pipeline.STEPS.index(k)
        for k, reqs in pipeline.REQUIRES.items()
        for r in reqs
    )


def test_hollow_after_cleaning_is_not_reused(tmp_path: Path) -> None:
    # Distinct lines with plenty of raw chars, but every one is invented and dropped by
    # clean(): too little text, measured on what survives clean().
    invented = [
        _seg(f"Top {i} najboljih filmova svih vremena.", i * 3, i * 3 + 2) for i in range(30)
    ]
    assert quality.too_little_text(invented, 90.0) and quality.is_suspect(invented, 90.0)
    good = _dialogue(90, 180)
    path = tmp_path / "1.json"

    def write(chunk: dict[str, Any]) -> None:
        path.write_text(json.dumps({"video_id": "v1", "chunks": [
            {"start": 0, "end": 90, "segments": invented, **chunk},
            {"start": 90, "end": 180, "retry": None, "segments": good},
        ]}))  # fmt: skip

    for chunk in (
        {"retry": [{"start": 0, "end": 30, "mode": "prompt"}], "repaired": False},
        {"retry": [{"start": 0, "end": 30, "mode": "prompt"}]},  # no flag (older file)
        {"reused": True, "repaired": True},  # pilot-style reused repair
        {"retried": True, "unresolved": True},  # current shape
    ):
        write(chunk)
        prev = steps._previous_chunks(path, "v1")
        # Retried and still hollow: flagged unresolved, redone only when forced.
        assert prev[(0, 90)].unresolved and not prev[(90, 180)].unresolved, chunk
    # Never retried: is_suspect now sees the cleaned text too, so it is not reused either
    # (rule changed in round 6; before, the raw text let it through forever).
    write({"retry": None})
    assert list(steps._previous_chunks(path, "v1")) == [(90, 180)]


def test_corrupt_transcript_is_not_fatal(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 30]])
    s.transcripts_dir.mkdir(parents=True)
    out = s.transcripts_dir / "1.json"
    calls = _mock_whisper(monkeypatch, _dialogue)
    monkeypatch.setattr(steps.index_build, "do_index", lambda *a, **k: {})
    _done(conn, "v1", "download", "prepare", "transcribe")

    for bad in ('{"chunks": [', "[1, 2]", '{"video_id": "v1", "chunks": [{"start": 0}]}'):
        out.write_text(bad)
        assert steps._previous_chunks(out, "v1") == {}
    assert "ignoring" in caplog.text

    # An index-only run does not read the transcript; labelling skips the corrupt file.
    out.write_text('{"chunks": [')
    steps.run_episode(s, conn, 1, ["index"], force=True)
    assert steps.label_transcripts(s, conn)["skipped"] == {"1.json": "unreadable"}
    assert out.read_text() == '{"chunks": ['
    # transcribe treats it as nothing to reuse and writes a good file.
    steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert calls == [(0, 30, "p")] and json.loads(out.read_text())["video_id"] == "v1"


def test_stale_temp_files_are_removed(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import time

    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 30]])
    s.transcripts_dir.mkdir(parents=True)
    work = s.work_dir / "1"
    old = time.time() - steps.STALE_TEMP_S - 60
    stale = [work / "piece-abc.wav", s.transcripts_dir / ".1-abc.tmp"]
    fresh = [work / "piece-new.wav", s.transcripts_dir / ".1-new.tmp"]  # live process
    other = [s.transcripts_dir / ".10-abc.tmp", work / "audio.wav"]  # not ours to remove
    for p in stale + fresh + other:
        p.write_bytes(b"x")
    for p in stale + other:
        os.utime(p, (old, old))
    _mock_whisper(monkeypatch, _dialogue)
    steps.do_transcribe(s, conn, 1, "v1")
    assert not any(p.exists() for p in stale)
    assert all(p.exists() for p in fresh + other)


def test_pilot_transcript_survives_failed_forced_rerun(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 90], [90, 180], [180, 270]])
    good = _dialogue(0, 90)
    legacy = {  # pilot file: no video_id; chunk 2 looped and was never repaired
        "episode_id": 1, "prompt": "", "segments": [],
        "chunks": [
            {"start": 0, "end": 90, "first_try": good, "retry": None, "segments": good},
            {"start": 90, "end": 180, "first_try": [], "retry": None,
             "segments": _dialogue(90, 180)},
            {"start": 180, "end": 270, "first_try": [], "retry": None,
             "segments": [_seg("Hvala vam.", 180 + i, 181 + i) for i in range(5)]},
        ],
    }  # fmt: skip
    s.transcripts_dir.mkdir(parents=True)
    out = s.transcripts_dir / "1.json"
    out.write_text(json.dumps(legacy))
    _done(conn, "v1", "download", "prepare", "transcribe")
    steps.label_transcripts(s, conn)  # the one-off migration after deploy

    def boom(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        raise steps.whisper.WhisperError("failed to process audio")

    calls = _mock_whisper(monkeypatch, boom)
    # Forced transcribe dies on the first chunk it sends, before any partial write.
    with pytest.raises(steps.whisper.WhisperError):
        steps.run_episode(s, conn, 1, ["transcribe"], force=True)
    assert calls == [(180, 270, "p")]
    assert (
        conn.execute("SELECT status FROM episode_steps WHERE step='transcribe'").fetchone()[0]
        == "failed"
    )
    doc = json.loads(out.read_text())
    assert doc["video_id"] == "v1" and doc["chunks"] == legacy["chunks"]  # only labelled

    # Next run: step is not done any more, but the good chunks are still reused.
    calls = _mock_whisper(monkeypatch, _dialogue)
    info = steps.run_episode(s, conn, 1, ["transcribe"])["transcribe"]
    assert calls == [(180, 270, "p")] and info["chunks_reused"] == 2


def test_unlabeled_transcript_never_reused_directly(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 30]])
    s.transcripts_dir.mkdir(parents=True)
    old = [{"start": 0, "end": 3, "text": "Stari tekst."}]
    (s.transcripts_dir / "1.json").write_text(
        json.dumps({"chunks": [{"start": 0, "end": 30, "segments": old}]})
    )
    calls = _mock_whisper(monkeypatch, _dialogue)
    steps.do_transcribe(s, conn, 1, "v1")
    assert calls == [(0, 30, "p")]


def test_index_rebuild_is_separate_from_force(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    got: list[bool] = []
    monkeypatch.setattr(steps, "wait_if_paused", lambda *a, **k: None)
    monkeypatch.setattr(
        steps.index_build, "do_index", lambda *a, rebuild: got.append(rebuild) or {}
    )
    _done(conn, "v1", "download", "prepare", "transcribe", "speakers", "index")

    steps.run_episode(s, conn, 1, ["index"])  # plain: done, skipped
    assert got == []
    steps.run_episode(s, conn, 1, ["index"], force=True)  # --force: incremental
    assert got == [False]
    got.clear()
    steps.run_episode(s, conn, 1, ["index"], rebuild_index=True)  # runs although done
    assert got == [True]
    # --rebuild-index forces only the index step, not the earlier ones.
    ran: list[str] = []
    monkeypatch.setattr(steps.speakers, "get_embedder", lambda s: None)
    monkeypatch.setattr(steps.speakers, "do_speakers", lambda *a: ran.append("speakers") or {})
    got.clear()
    steps.run_episode(s, conn, 1, ["speakers", "index"], rebuild_index=True)
    assert ran == [] and got == [True]


def test_invented_lines_are_retried_on_first_try(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 90]])

    def invented(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        n = int((b - a) // 3)
        return [_seg(f"Top {int(a) + i} najboljih filmova svih vremena.", i * 3, i * 3 + 2)
                for i in range(n)]  # fmt: skip

    calls = _mock_whisper(monkeypatch, invented)
    info = steps.do_transcribe(s, conn, 1, "v1")
    assert info["chunks_retried"] == 1 and info["chunks_unresolved"] == 1
    assert (0, 30, "p") in calls and info["segments"] == 0
    # Unresolved: a plain rerun keeps it, a forced rerun sends it again.
    calls.clear()
    assert steps.do_transcribe(s, conn, 1, "v1")["chunks_unresolved"] == 1 and calls == []
    steps.do_transcribe(s, conn, 1, "v1", force=True)
    assert (0, 90, "p") in calls


def test_label_transcripts(tmp_path: Path, conn: sqlite3.Connection) -> None:
    s = _settings(tmp_path)
    s.transcripts_dir.mkdir(parents=True)
    doc = {"episode_id": 0, "chunks": [], "segments": []}
    for eid in (1, 2, 3, 4):
        (s.transcripts_dir / f"{eid}.json").write_text(json.dumps(doc))
    (s.transcripts_dir / "5.json").write_text(json.dumps({**doc, "video_id": "v5"}))
    (s.transcripts_dir / "6.json").write_text("{broken")
    (s.transcripts_dir / "notes.json").write_text("{}")
    _done(conn, "v1", "transcribe", eid=1)
    _done(conn, "vOld", "transcribe", eid=2)  # done for another video
    conn.execute("UPDATE episodes SET retired=1 WHERE id=3")
    conn.commit()
    res = steps.label_transcripts(s, conn)
    assert res == {
        "labelled": 1,
        "already_labelled": 1,
        "skipped": {
            "2.json": "transcribe not recorded done for the current video",
            "3.json": "episode not in the active catalog",
            "4.json": "transcribe not recorded done for the current video",
            "6.json": "unreadable",
            "notes.json": "not an episode transcript",
        },
    }
    assert json.loads((s.transcripts_dir / "1.json").read_text()) == {**doc, "video_id": "v1"}
    # Idempotent.
    again = steps.label_transcripts(s, conn)
    assert again["labelled"] == 0 and again["already_labelled"] == 2


def test_transcript_file_is_readable_and_synced(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import stat

    s = _settings(tmp_path)
    _chunks(s, 1, [[0, 30]])
    _mock_whisper(monkeypatch, _dialogue)
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(steps.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    steps.do_transcribe(s, conn, 1, "v1")
    out = s.transcripts_dir / "1.json"
    assert stat.S_IMODE(out.stat().st_mode) == 0o644
    assert len(synced) == 2  # the partial write after the chunk and the final one


def test_run_episode_retries_unresolved_only_when_forced(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _settings(tmp_path)
    plan = [[i * 90, (i + 1) * 90] for i in range(10)]
    _chunks(s, 1, plan)
    fail = {"at": 810.0}

    def answer(a: float, b: float, prompt: str) -> list[dict[str, Any]]:
        chunk = a // 90 * 90
        if chunk == fail["at"]:
            raise steps.whisper.WhisperError("failed to process audio")
        return [] if chunk == 270 else _dialogue(a, b)  # chunk 3: a long music stretch

    calls = _mock_whisper(monkeypatch, answer)
    _done(conn, "v1", "download", "prepare")
    # The run fails on chunk 9, after chunk 3 was retried and stayed unresolved.
    with pytest.raises(steps.whisper.WhisperError):
        steps.run_episode(s, conn, 1, ["transcribe"])
    assert any(a // 90 * 90 == 270 for a, _, _ in calls)
    # Plain resume: chunk 3 is not sent again; only the failed chunk 9 is.
    fail["at"] = None
    calls.clear()
    res = steps.run_episode(s, conn, 1, ["transcribe"])["transcribe"]
    assert {a // 90 * 90 for a, _, _ in calls} == {810}
    assert res["chunks_unresolved"] == 1
    # Forced: chunk 3 is retried, everything else reused.
    calls.clear()
    res = steps.run_episode(s, conn, 1, ["transcribe"], force=True)["transcribe"]
    assert {a // 90 * 90 for a, _, _ in calls} == {270} and res["chunks_reused"] == 9
