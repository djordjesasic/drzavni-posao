import json
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
