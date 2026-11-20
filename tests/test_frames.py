import base64
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_enrich import _settings, conn  # noqa: F401 – fixture

from dp.enrich import client, frames
from dp.enrich import run as enrich_run


def _video(path: Path, seconds: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"testsrc=size=640x360:rate=5:duration={seconds}", str(path)],
        check=True,
    )  # fmt: skip
    return path


def test_extract_takes_one_frame_per_interval(tmp_path: Path) -> None:
    shots = frames.extract(_video(tmp_path / "v.mkv", 25), every_s=10, width=256)
    assert [ms for ms, _ in shots] == [5000, 15000]
    assert all(jpeg[:2] == b"\xff\xd8" for _, jpeg in shots)  # JPEG
    blocks = frames.to_blocks(shots)
    assert blocks[0] == {"type": "text", "text": "[0:05]"}
    assert blocks[3]["source"]["media_type"] == "image/jpeg"  # type: ignore[index]
    assert base64.b64decode(blocks[3]["source"]["data"]) == shots[1][1]  # type: ignore[index]


def test_vision_model_sends_frames_and_needs_the_video(
    tmp_path: Path,
    conn,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def fake_claude(cfg: dict[str, Any], p: dict[str, Any]) -> client.Reply:
        seen.update(system=p["system"], images=p["messages"][0]["content"][2:])
        return client.Reply('{"summary": "x"}', 3000, 500)

    monkeypatch.setattr(enrich_run.claude, "chat_params", fake_claude)
    s = _settings(tmp_path)
    s.enrich_models["v"] = {"api": "anthropic", "model": "m", "frames_every_s": 10}
    info = enrich_run.enrich_episode(s, conn, 254, "v")
    assert info is not None and "no video" in info["error"]

    _video(frames.video_path(tmp_path, 254), 12)
    info = enrich_run.enrich_episode(s, conn, 254, "v")
    assert info is not None and "error" not in info
    assert "kadrove" in seen["system"]
    assert [b["type"] for b in seen["images"]] == ["text", "image"]
