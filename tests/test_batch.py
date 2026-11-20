import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from test_enrich import _settings, conn  # noqa: F401 – fixture

from dp.enrich import batch, prompt
from dp.enrich.store import valid_enrichment


def _msg(text: str, stop: str = "end_turn") -> NS:
    usage = NS(input_tokens=100, output_tokens=50, cache_read_input_tokens=80,
               cache_creation_input_tokens=0)  # fmt: skip
    return NS(stop_reason=stop, stop_details=None, content=[NS(type="text", text=text)],
              usage=usage)  # fmt: skip


class FakeAPI:
    """messages.batches with create/retrieve/results, results set by the test."""

    def __init__(self) -> None:
        self.created: list[list[dict[str, Any]]] = []
        self.status = "in_progress"
        self.out: list[NS] = []
        self.messages = NS(batches=self)

    def create(self, requests: list[dict[str, Any]]) -> NS:
        self.created.append(list(requests))
        return NS(id=f"b{len(self.created)}")

    def retrieve(self, batch_id: str) -> NS:
        counts = {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}
        return NS(processing_status=self.status, request_counts=counts)

    def results(self, batch_id: str) -> list[NS]:
        return self.out


def _answer(summary: str) -> str:
    return json.dumps({k: [] for k in prompt.SCHEMA} | {"summary": summary})


def test_submit_then_collect_stores_descriptions(
    tmp_path: Path,
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    s = _settings(tmp_path)
    s.enrich_models["c"] = {"api": "anthropic", "model": "m"}
    s = s.__class__(**{**s.__dict__, "index_enrich_models": ("c",)})
    conn.execute(
        "INSERT INTO episode_steps (episode_id, step, status, video_id) "
        "VALUES (254, 'index', 'done', 'v1')"
    )
    api = FakeAPI()
    assert batch.candidates(s, conn, "c") == [254]
    assert batch.submit(s, conn, "c", [254], api) == ["b1"]
    req = api.created[0][0]
    assert req["custom_id"] == "ep-254" and req["params"]["model"] == "m"
    assert "Ovčija glava" in req["params"]["messages"][0]["content"][1]["text"]
    assert batch.candidates(s, conn, "c") == []  # waiting in the batch: not sent twice

    assert batch.collect(s, conn, api)["waiting_batches"] == 1  # not ended yet
    [b] = batch.status(conn, api)
    assert (b["episodes"], b["api"], b["counts"]["processing"]) == (1, "in_progress", 1)
    api.status = "ended"
    api.out = [NS(custom_id="ep-254", result=NS(type="succeeded", message=_msg(_answer("x"))))]
    assert batch.collect(s, conn, api)["done"] == 1
    row = valid_enrichment(conn, 254, "c", "v1")
    assert row is not None and json.loads(row["data"])["summary"] == "x"
    assert row["tokens_in"] == 180 and row["elapsed_s"] is None
    # The new description must reach the index.
    step = conn.execute("SELECT status FROM episode_steps WHERE step='index'").fetchone()[0]
    assert step == "pending"
    assert conn.execute("SELECT status FROM enrich_batches").fetchone()[0] == "collected"
    assert batch.status(conn, api)[0]["status"] == "collected"
    assert batch.collect(s, conn, api)["done"] == 0  # collected batches are not read again


def test_collect_handles_errors_repoints_and_changed_transcripts(
    tmp_path: Path,
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    s = _settings(tmp_path)
    s.enrich_models["c"] = {"api": "anthropic", "model": "m"}
    api = FakeAPI()
    batch.submit(s, conn, "c", [254], api)
    api.status = "ended"

    api.out = [NS(custom_id="ep-254", result=NS(type="expired"))]
    assert batch.collect(s, conn, api)["failed"] == 1
    assert conn.execute("SELECT status FROM enrichment").fetchone()[0] == "failed"

    # Transcribed again after the batch was sent: the result is kept, but as stale.
    batch.submit(s, conn, "c", [254], api)
    conn.execute("UPDATE enrich_batches SET submitted_at='2000-01-01 00:00:00' WHERE id='b2'")
    conn.execute("UPDATE episode_steps SET updated_at=datetime('now') WHERE step='transcribe'")
    api.out = [NS(custom_id="ep-254", result=NS(type="succeeded", message=_msg(_answer("y"))))]
    assert batch.collect(s, conn, api)["stale"] == 1

    # The episode now points to another video: the result belongs to the old one.
    batch.submit(s, conn, "c", [254], api)
    conn.execute("INSERT INTO videos (video_id, title, raw) VALUES ('v2', 't', '{}')")
    conn.execute("UPDATE episodes SET video_id='v2' WHERE id=254")
    assert batch.collect(s, conn, api)["repointed"] == 1


def test_vision_model_needs_the_video_and_is_split_by_size(
    tmp_path: Path,
    conn: sqlite3.Connection,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _settings(tmp_path)
    s.enrich_models["v"] = {"api": "anthropic", "model": "m", "frames_every_s": 10}
    assert batch.candidates(s, conn, "v") == []  # no video yet
    with pytest.raises(ValueError):
        batch.candidates(s, conn, "fake")  # not an anthropic model
    monkeypatch.setattr(batch.frames, "extract", lambda path, every: [(5000, b"\xff\xd8jpeg")])
    (tmp_path / "video").mkdir()
    (tmp_path / "video" / "254.mkv").write_bytes(b"")
    assert batch.candidates(s, conn, "v") == [254]
    monkeypatch.setattr(batch, "MAX_BATCH_BYTES", 1)  # every request alone
    api = FakeAPI()
    assert batch.submit(s, conn, "v", [254, 254], api) == ["b1", "b2"]
    content = api.created[0][0]["params"]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "text", "text", "image"]
