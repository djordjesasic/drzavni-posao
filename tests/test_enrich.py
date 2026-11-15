import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from dp.catalog.characters import load_seed, parse_seed
from dp.config import Settings
from dp.db import connect, migrate
from dp.enrich import client, prompt
from dp.enrich import run as enrich_run

SEED = Path(__file__).parent.parent / "seed" / "characters.yaml"


@pytest.fixture
def conn(tmp_path: Path):  # type: ignore[no-untyped-def]
    c = connect(tmp_path / "t.sqlite")
    migrate(c)
    load_seed(c, parse_seed(SEED))
    c.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v1', 't', '{}')")
    c.execute(
        "INSERT INTO episodes (id, video_id, season, title, air_date) "
        "VALUES (254, 'v1', 3, 'Narkotici', '2014-05-01')"
    )
    c.executemany(
        "INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin, text_fold) "
        "VALUES (254, ?, ?, ?, ?, ?, ?)",
        [(0, 5000, 7000, "Ovčija glava!", "Ovčija glava!", "ovcija glava!"),
         (1, 65000, 67000, "Torbice, šta ti je?", "Torbice, šta ti je?", "torbice")],
    )  # fmt: skip
    c.execute(
        "INSERT INTO episode_steps (episode_id, step, status, video_id) "
        "VALUES (254, 'transcribe', 'done', 'v1'), (254, 'speakers', 'done', 'v1')"
    )
    c.commit()
    return c


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        youtube_channel="", data_dir=tmp_path, db=tmp_path / "t.sqlite", catalog_dir=tmp_path,
        seed_characters=SEED, seed_pilot=tmp_path, whisper_url="", llama_url="",
        pause_start="00:00", pause_end="00:00", api_allowed_hosts=[], api_token=None,
        download_pause_s=(0, 0), language="sr", chunk_target_s=150, chunk_max_s=180,
        enrich_models={"fake": {"base_url": "http://x/v1", "model": "m"}},
    )  # fmt: skip


def test_messages_contain_registry_and_timed_transcript(conn) -> None:  # type: ignore[no-untyped-def]
    system, user = prompt.build_messages(conn, 254)
    assert system["role"] == "system" and "JSON" in system["content"]
    text = user["content"]
    assert "torbica: Dragan Torbica (glavni)" in text and "Dragan Jove Torbica" in text
    assert "[0:05] Ovčija glava!" in text and "[1:05] Torbice, šta ti je?" in text
    assert "„Narkotici“" in text
    # Registry and schema come before the transcript (shared prefix, prompt cache).
    assert text.index("REGISTAR") < text.index("ŠEMA") < text.index("TRANSKRIPT")


def test_transcript_has_voice_tags_and_background(conn) -> None:  # type: ignore[no-untyped-def]
    conn.execute("UPDATE segments SET actor='torbica', actor_score=0.6 WHERE idx=0")
    conn.execute("UPDATE segments SET actor='cvarkov', actor_score=0.2 WHERE idx=1")
    text = prompt.transcript(conn, 254)
    assert "[0:05] <torbica> Ovčija glava!" in text
    assert "[1:05] <cvarkov?> Torbice" in text
    conn.execute("UPDATE segments SET actor='other' WHERE idx=1")
    conn.execute("UPDATE segments SET actor=NULL WHERE idx=0")
    text = prompt.transcript(conn, 254)
    assert "[0:05] Ovčija glava!" in text and "[1:05] <drugi> Torbice" in text
    _, stable, _ = prompt.build_parts(conn, 254, "- UJEBP je stranka.")
    assert stable.index("POZADINA") < stable.index("REGISTAR")
    assert "POZADINA" not in prompt.build_parts(conn, 254)[1]


def test_parse_json_tolerates_fences_and_noise() -> None:
    assert client.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert client.parse_json('Evo:\n{"a": {"b": 2}}\nto je to') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        client.parse_json("[1, 2]")


def test_json_schema_sent_only_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict[str, Any]] = []

    class R:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "{}"}}], "usage": {}}

    def fake_post(url: str, **kw: Any) -> R:
        bodies.append(kw["json"])
        return R()

    monkeypatch.setattr(client.httpx, "post", fake_post)
    cfg = {"base_url": "http://x/v1", "model": "m"}
    client.chat(cfg, [], schema=prompt.JSON_SCHEMA)
    client.chat(cfg | {"json_schema": True}, [], schema=prompt.JSON_SCHEMA)
    assert bodies[0]["response_format"] == {"type": "json_object"}
    rf = bodies[1]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["required"] == list(prompt.SCHEMA)


def test_enrich_stores_result_and_skips_done(
    tmp_path: Path,
    conn,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[dict[str, str]]] = []
    answer = {k: [] for k in prompt.SCHEMA} | {
        "summary": "Torbica glumi da je drogiran.",
        "characters": [{"id": "torbica", "name": "Torbica", "presence": "na_ekranu"},
                       {"id": "izmisljen", "name": "X", "presence": "pominje_se"}],
    }  # fmt: skip

    def fake_chat(cfg: dict[str, Any], messages: list[dict[str, str]], **kw: Any) -> client.Reply:
        calls.append(messages)
        return client.Reply(json.dumps(answer), 1000, 200)

    monkeypatch.setattr(client, "chat", fake_chat)
    s = _settings(tmp_path)
    info = enrich_run.enrich_episode(s, conn, 254, "fake")
    assert info is not None
    assert info["missing_keys"] == [] and info["unknown_ids"] == ["izmisljen"]
    row = conn.execute("SELECT status, data, tokens_in FROM enrichment").fetchone()
    assert row["status"] == "done" and row["tokens_in"] == 1000
    assert json.loads(row["data"])["summary"].startswith("Torbica")

    assert enrich_run.enrich_episode(s, conn, 254, "fake") is None
    assert len(calls) == 1
    with pytest.raises(ValueError, match="unknown model"):
        enrich_run.enrich_episode(s, conn, 254, "nema")


def test_enrich_claude_gets_stable_part_separately(
    tmp_path: Path,
    conn,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_claude(cfg: dict[str, Any], system: str, stable: str, episode: str) -> client.Reply:
        seen.update(system=system, stable=stable, episode=episode)
        return client.Reply('{"summary": "x"}', 3000, 500, tokens_cached=2000)

    monkeypatch.setattr(enrich_run.claude, "chat", fake_claude)
    s = _settings(tmp_path)
    s.enrich_models["c"] = {"api": "anthropic", "model": "claude-haiku-5-5"}
    info = enrich_run.enrich_episode(s, conn, 254, "c")
    assert info is not None and info["tokens_cached"] == 2000
    assert "REGISTAR" in seen["stable"] and "TRANSKRIPT" not in seen["stable"]
    assert "Ovčija glava" in seen["episode"] and "REGISTAR" not in seen["episode"]


def test_enrich_records_failure(
    tmp_path: Path,
    conn,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client, "chat", lambda cfg, m, **kw: client.Reply("nije json", 10, 5))
    info = enrich_run.enrich_episode(_settings(tmp_path), conn, 254, "fake")
    assert info is not None and "error" in info
    row = conn.execute("SELECT status, raw FROM enrichment").fetchone()
    assert (row["status"], row["raw"]) == ("failed", "nije json")


def _good_answer() -> dict[str, Any]:
    return {k: [] for k in prompt.SCHEMA} | {
        "summary": "x",
        "opening_scene": {},
        "current_event": None,
    }


def test_enrich_is_done_only_for_the_same_video(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_chat(cfg: dict[str, Any], messages: list[dict[str, str]], **kw: Any) -> client.Reply:
        calls.append(1)
        return client.Reply(json.dumps(_good_answer()), 10, 5)

    monkeypatch.setattr(client, "chat", fake_chat)
    s = _settings(tmp_path)
    enrich_run.enrich_episode(s, conn, 254, "fake")
    assert conn.execute("SELECT video_id FROM enrichment").fetchone()[0] == "v1"
    assert enrich_run.enrich_episode(s, conn, 254, "fake") is None
    assert len(calls) == 1
    # The episode id now points to another video: the old description is stale.
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v2', 't', '{}')")
    conn.execute("UPDATE episodes SET video_id='v2' WHERE id=254")
    conn.execute("UPDATE episode_steps SET video_id='v2'")  # transcribe and speakers re-done
    conn.execute("UPDATE enrichment SET video_id='v1'")
    assert enrich_run.enrich_episode(s, conn, 254, "fake") is not None
    assert len(calls) == 2
    assert conn.execute("SELECT video_id FROM enrichment").fetchone()[0] == "v2"


def test_null_video_id_row_is_not_valid(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client, "chat", lambda cfg, m, **kw: client.Reply(json.dumps(_good_answer()), 1, 1)
    )
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data) "
        "VALUES (254, 'fake', ?, 'done', '{}')",
        (prompt.PROMPT_VERSION,),
    )
    assert enrich_run.valid_enrichment(conn, 254, "fake", "v1") is None
    assert enrich_run.enrich_episode(_settings(tmp_path), conn, 254, "fake") is not None


def test_unknown_episode_returns_error_without_row(
    tmp_path: Path,
    conn: sqlite3.Connection,
) -> None:
    info = enrich_run.enrich_episode(_settings(tmp_path), conn, 9999, "fake")
    assert info is not None and "9999" in info["error"]
    assert conn.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0] == 0


def test_enrich_without_transcript_is_recorded_not_raised(
    tmp_path: Path,
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DELETE FROM segments")
    info = enrich_run.enrich_episode(_settings(tmp_path), conn, 254, "fake")
    assert info is not None and "error" in info
    assert conn.execute("SELECT status FROM enrichment").fetchone()[0] == "failed"


def test_failed_rerun_keeps_good_result(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter([json.dumps(_good_answer()), "nije json"])
    monkeypatch.setattr(client, "chat", lambda cfg, m, **kw: client.Reply(next(replies), 1, 1))
    s = _settings(tmp_path)
    assert "error" not in (enrich_run.enrich_episode(s, conn, 254, "fake") or {})
    info = enrich_run.enrich_episode(s, conn, 254, "fake", force=True)
    assert info is not None and "error" in info
    row = conn.execute("SELECT status, data FROM enrichment").fetchone()
    assert row["status"] == "done" and json.loads(row["data"])["summary"] == "x"


def test_failure_never_overwrites_a_done_result_of_another_video(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data, video_id) "
        "VALUES (254, 'fake', ?, 'done', '{}', 'old')",
        (prompt.PROMPT_VERSION,),
    )
    replies = iter(["nije json", json.dumps(_good_answer())])
    monkeypatch.setattr(client, "chat", lambda cfg, m, **kw: client.Reply(next(replies), 1, 1))
    info = enrich_run.enrich_episode(_settings(tmp_path), conn, 254, "fake")
    assert info is not None and "error" in info
    row = conn.execute("SELECT status, video_id FROM enrichment").fetchone()
    assert (row["status"], row["video_id"]) == ("done", "old")
    # Only a success replaces it.
    enrich_run.enrich_episode(_settings(tmp_path), conn, 254, "fake")
    row = conn.execute("SELECT status, video_id FROM enrichment").fetchone()
    assert (row["status"], row["video_id"]) == ("done", "v1")


def test_stale_description_is_redone_without_force_and_kept_on_failure(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data, video_id) "
        "VALUES (254, 'fake', ?, 'stale', '{\"summary\": \"staro\"}', 'v1')",
        (prompt.PROMPT_VERSION,),
    )
    conn.commit()
    replies = iter(["nije json", json.dumps(_good_answer())])
    monkeypatch.setattr(client, "chat", lambda cfg, m, **kw: client.Reply(next(replies), 1, 1))
    s = _settings(tmp_path)
    # No --force: a stale description is not "done", so the model is called.
    info = enrich_run.enrich_episode(s, conn, 254, "fake")
    assert info is not None and "error" in info
    row = conn.execute("SELECT status, data FROM enrichment").fetchone()
    assert row["status"] == "stale" and "staro" in row["data"]  # the failure kept it
    info = enrich_run.enrich_episode(s, conn, 254, "fake")
    assert info is not None and "error" not in info
    assert conn.execute("SELECT status FROM enrichment").fetchone()[0] == "done"


def test_retired_or_videoless_episode_is_not_sent_to_the_model(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> client.Reply:
        raise AssertionError("model must not be called")

    monkeypatch.setattr(client, "chat", boom)
    s = _settings(tmp_path)
    conn.execute("UPDATE episodes SET retired=1, retired_video_id=video_id, video_id=NULL")
    info = enrich_run.enrich_episode(s, conn, 254, "fake")
    assert info is not None and "error" in info
    conn.execute("UPDATE episodes SET retired=0")  # active but without a video
    info = enrich_run.enrich_episode(s, conn, 254, "fake")
    assert info is not None and "error" in info
    assert conn.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0] == 0


def test_catalog_layer_import_stays_light() -> None:
    # dp.search.app needs the server stack; its catalog layer is what must stay light.
    code = (
        "import sys, dp.catalog.classify, dp.search.catalog; "
        "bad = [m for m in ('numpy', 'dp.index.build', 'dp.transcribe.speakers', "
        "'dp.enrich.claude', 'anthropic') if m in sys.modules]; print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_index_build_import_does_not_pull_in_llm_clients() -> None:
    code = (
        "import sys, dp.index.build; "
        "bad = [m for m in ('dp.enrich.claude', 'dp.enrich.client', 'anthropic') "
        "if m in sys.modules]; print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_check_reports_bad_shapes(conn: sqlite3.Connection) -> None:
    data = _good_answer() | {
        "places": "Torbica",
        "quotes": ["not an object"],
        "stories": [{"about": "Torbica"}],
    }
    assert enrich_run.check(conn, data)["bad_shape"] == ["quotes", "places", "stories.about"]
    assert enrich_run.check(conn, _good_answer())["bad_shape"] == []


def test_successful_enrichment_marks_index_step_pending(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client, "chat", lambda cfg, m, **kw: client.Reply(json.dumps(_good_answer()), 1, 1)
    )
    s = _settings(tmp_path)
    s.enrich_models["other"] = {"base_url": "http://x/v1", "model": "m"}
    conn.execute(
        "INSERT INTO episode_steps (episode_id, step, status) VALUES (254, 'index', 'done')"
    )
    index = "SELECT status FROM episode_steps WHERE step='index'"
    enrich_run.enrich_episode(s, conn, 254, "other")  # not the model that feeds the index
    assert conn.execute(index).fetchone()[0] == "done"
    s.enrich_models[s.index_enrich_model] = {"base_url": "http://x/v1", "model": "m"}
    enrich_run.enrich_episode(s, conn, 254, s.index_enrich_model)
    assert conn.execute(index).fetchone()[0] == "pending"
    # No step row: nothing is created.
    conn.execute("DELETE FROM episode_steps WHERE step='index'")
    enrich_run.enrich_episode(s, conn, 254, s.index_enrich_model, force=True)
    assert conn.execute(index).fetchone() is None


def test_enrich_needs_transcript_of_the_current_video(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> client.Reply:
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(client, "chat", boom)
    s = _settings(tmp_path)
    # Re-pointed, the segments still belong to the old video's transcript.
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v2', 't', '{}')")
    conn.execute("UPDATE episodes SET video_id='v2' WHERE id=254")
    conn.commit()
    info = enrich_run.enrich_episode(s, conn, 254, "fake")
    assert info == {
        "model": "fake",
        "error": "episode 254: step 'transcribe' is not done for the current video",
    }
    # Not done at all (e.g. failed or pending): same.
    conn.execute("UPDATE episode_steps SET video_id='v2', status='pending'")
    conn.commit()
    assert "error" in (enrich_run.enrich_episode(s, conn, 254, "fake") or {})
    assert conn.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0] == 0


def test_enrich_needs_speaker_tags(
    tmp_path: Path,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*a: Any, **kw: Any) -> client.Reply:
        raise AssertionError("the model must not be called")

    monkeypatch.setattr(client, "chat", boom)
    # A forced transcribe changed the text: speakers is pending, actor columns are empty.
    conn.execute("UPDATE episode_steps SET status='pending' WHERE step='speakers'")
    conn.commit()
    info = enrich_run.enrich_episode(_settings(tmp_path), conn, 254, "fake", force=True)
    assert info == {
        "model": "fake",
        "error": "episode 254: step 'speakers' is not done for the current video",
    }
    assert conn.execute("SELECT COUNT(*) FROM enrichment").fetchone()[0] == 0
