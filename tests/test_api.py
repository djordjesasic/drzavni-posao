import json
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from dp.catalog.characters import load_seed, parse_seed
from dp.config import get_settings
from dp.db import connect, migrate
from dp.enrich.prompt import PROMPT_VERSION
from dp.index import build
from dp.text import fold

SEED = Path(__file__).parent.parent / "series" / "drzavni-posao" / "seed" / "characters.yaml"
TOKEN = "test-token"
VOCAB = ["telefon", "mama", "sendvic", "laptop"]


def fake_embed(texts: list[str]) -> np.ndarray:
    m = np.array([[fold(t).count(w) + 0.01 for w in VOCAB] for t in texts], dtype=np.float32)
    return m / np.linalg.norm(m, axis=1, keepdims=True)


def _add_content(db: Path, tmp: Path) -> None:
    """Episode 2036 with a transcript (speaker labels), a description and an index."""
    from test_enrich import _settings  # tests/ is on sys.path (pytest rootdir mode)

    conn = connect(db)
    lines = [
        (0, 3000, "Grejem sendvič na laptopu.", "cvarkov", 0.6),
        (100_000, 104_000, "Telefon ti vraćam kad mama dođe po njega.", "cvarkov", 0.2),
        (200_000, 201_000, "Mljac.", None, None),
    ]
    conn.executemany(
        "INSERT INTO segments (episode_id, idx, start_ms, end_ms, text, text_latin, text_fold, "
        "actor, actor_score) VALUES (2036, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(i, a, b, x, x, fold(x), who, sc) for i, (a, b, x, who, sc) in enumerate(lines)],
    )
    data = {"summary": "Čvarkov oduzima Boškiću telefon.", "topics": ["telefon"]}
    conn.execute(
        "INSERT INTO enrichment (episode_id, model, prompt_version, status, data, video_id) "
        "VALUES (2036, 'sonnet', ?, 'done', ?, 'abc123')",
        (PROMPT_VERSION, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()
    build.do_index(_settings(tmp), conn, 2036, fake_embed)
    conn.close()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


# The MCP session manager can run only once per process, so all tests share one client.
@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    db = tmp_path_factory.mktemp("api") / "t.sqlite"
    conn = connect(db)
    migrate(conn)
    load_seed(conn, parse_seed(SEED))
    conn.execute(
        "INSERT INTO videos (video_id, title, raw) VALUES ('abc123', 'x', '{}')",
    )
    conn.execute(
        """INSERT INTO episodes (id, video_id, season, title, air_date, duration_s)
           VALUES (2036, 'abc123', 12, 'Na potpis', '2023-09-25', 731)"""
    )
    conn.commit()
    conn.close()
    _add_content(db, db.parent)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DP_DB", str(db))
        mp.setenv("DP_API_TOKEN", TOKEN)
        mp.setattr("dp.search.content._embed", fake_embed)
        get_settings.cache_clear()
        from dp.search.app import app

        # Use an allowed production host so /mcp passes DNS rebinding protection.
        with TestClient(app, base_url="http://llmserver:8090") as c:
            yield c
    get_settings.cache_clear()


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


def test_token_required(client: TestClient) -> None:
    assert client.get("/episodes/2036").status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.get("/episodes/2036", headers=bad).status_code == 401


def test_episode_and_search(client: TestClient) -> None:
    ep = client.get("/episodes/2036", headers=AUTH).json()
    assert ep["youtube_url"] == "https://www.youtube.com/watch?v=abc123"
    assert [e["id"] for e in client.get("/episodes?q=POTPIS", headers=AUTH).json()] == [2036]
    assert client.get("/episodes/1", headers=AUTH).status_code == 404


def test_characters_diacritics_insensitive(client: TestClient) -> None:
    ids = {c["id"] for c in client.get("/characters?q=marjanovic", headers=AUTH).json()}
    assert ids == {"direktor_marjanovic", "cevara_marjanovic"}


def test_mcp_lists_and_calls_tools(client: TestClient) -> None:
    headers = {**AUTH, "Accept": "application/json, text/event-stream"}
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    assert client.post("/mcp", json=init, headers=headers).status_code == 200
    r = client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers
    )
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert {
        "search_episodes", "search_titles", "get_episode", "get_transcript_excerpt",
        "find_character", "catalog_info",
    } <= names  # fmt: skip
    r = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_episode", "arguments": {"episode": 2036}},
        },
        headers=headers,
    )
    assert "Na potpis" in json.dumps(r.json(), ensure_ascii=False)


def test_mcp_requires_token(client: TestClient) -> None:
    assert client.post("/mcp", json={}).status_code == 401


def test_bearer_scheme_is_case_insensitive(client: TestClient) -> None:
    lower = {"Authorization": f"bearer {TOKEN}"}
    assert client.get("/episodes/2036", headers=lower).status_code == 200


def test_token_without_scheme_is_rejected(client: TestClient) -> None:
    assert client.get("/episodes/2036", headers={"Authorization": TOKEN}).status_code == 401
    other = {"Authorization": f"Basic {TOKEN}"}
    assert client.get("/episodes/2036", headers=other).status_code == 401


MCP_HEADERS = {**AUTH, "Accept": "application/json, text/event-stream"}


def _mcp_call(client: TestClient, name: str, arguments: dict[str, object]) -> dict[str, object]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    r = client.post("/mcp", json=body, headers=MCP_HEADERS)
    assert r.status_code == 200
    return r.json()["result"]


@pytest.mark.parametrize(
    "path",
    [
        "/episodes/0",
        "/episodes/100000",
        "/episodes/99999999999999999999999",
        "/episodes?season=0",
        "/episodes?season=100",
        "/episodes?year=1999",
        "/episodes?year=99999999999999999999",
    ],
)
def test_out_of_range_params_are_422(client: TestClient, path: str) -> None:
    assert client.get(path, headers=AUTH).status_code == 422


def test_mcp_out_of_range(client: TestClient) -> None:
    assert "null" in json.dumps(_mcp_call(client, "get_episode", {"episode": 10**12}))
    result = _mcp_call(client, "search_titles", {"season": 500})
    assert result["isError"]
    assert "season must be" in json.dumps(result)


def _use_db(monkeypatch: pytest.MonkeyPatch, db: Path) -> None:
    monkeypatch.setenv("DP_DB", str(db))
    get_settings.cache_clear()


def test_missing_db_is_503(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_db(monkeypatch, tmp_path / "missing.sqlite")
    try:
        r = client.get("/episodes/2036", headers=AUTH)
        assert r.status_code == 503
        assert "dp.cli init" in r.json()["detail"]
        assert client.get("/health").status_code == 200
        result = _mcp_call(client, "catalog_info", {})
        assert result["isError"]
        assert "dp.cli init" in json.dumps(result)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_old_schema_is_503(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "old.sqlite"
    conn = connect(db)
    conn.execute("PRAGMA user_version=1")
    conn.close()
    _use_db(monkeypatch, db)
    try:
        r = client.get("/catalog", headers=AUTH)
        assert r.status_code == 503
        assert "not migrated" in r.json()["detail"]
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_content_search_links_to_the_minute(client: TestClient) -> None:
    r = client.post("/search", json={"query": "oduzima telefon dok mama ne dodje"}, headers=AUTH)
    body = r.json()
    assert body["indexed_episodes"] == 1
    top = body["results"][0]
    assert top["episode"] == 2036 and top["title"] == "Na potpis"
    assert top["minute"] == "1:40" and top["youtube_url"].endswith("v=abc123&t=100s")
    assert top["has_description"] is True
    assert client.post("/search", json={"query": ""}, headers=AUTH).status_code == 422


def test_content_search_flags_episode_without_description(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dp.search.content.index_description", lambda *a: None)
    r = client.post("/search", json={"query": "sendvic na laptopu"}, headers=AUTH)
    assert r.json()["results"][0]["has_description"] is False


def test_content_search_without_embed_service_uses_words(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down(texts: list[str]) -> np.ndarray:
        raise ConnectionError("embed is down")

    monkeypatch.setattr("dp.search.content._embed", down)
    r = client.post("/search", json={"query": "sendvic na laptopu"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["results"][0]["episode"] == 2036


def test_transcript_excerpt_and_description(client: TestClient) -> None:
    ex = client.get("/episodes/2036/transcript?start_s=90&end_s=210", headers=AUTH).json()
    assert ex["from"] == "1:30" and ex["youtube_url"].endswith("&t=90s")
    assert [(x["time"], x["speaker"]) for x in ex["lines"]] == [
        ("1:40", "Đorđe Čvarkov?"),  # unsure voice match
        ("3:20", None),  # too short to label
    ]
    assert (
        client.get("/episodes/2036/transcript", headers=AUTH).json()["lines"][0]["speaker"]
        == "Đorđe Čvarkov"
    )
    ep = client.get("/episodes/2036", headers=AUTH).json()
    assert ep["description"]["summary"].startswith("Čvarkov oduzima")


def test_mcp_content_tools(client: TestClient) -> None:
    res = json.dumps(
        _mcp_call(client, "search_episodes", {"query": "mama dodje po telefon"}),
        ensure_ascii=False,
    )
    assert '"episode": 2036' in res and "t=100s" in res
    res = json.dumps(
        _mcp_call(client, "get_transcript_excerpt", {"episode": 2036, "start_s": 95}),
        ensure_ascii=False,
    )
    assert "mama dođe po njega" in res
    assert "null" in json.dumps(
        _mcp_call(client, "get_transcript_excerpt", {"episode": 1, "start_s": 0})
    )
    assert _mcp_call(client, "search_episodes", {"query": "  "})["isError"]
