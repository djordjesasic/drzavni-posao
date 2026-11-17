import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dp.transcribe import download


def test_video_format() -> None:
    assert download.video_format(0) == "bestaudio/best"
    assert download.video_format(480).startswith("bv*[height<=480]+ba")


class _FakeYDL:
    """Writes a tiny file like yt-dlp would: a video when the format asks for one."""

    calls: list[dict[str, Any]] = []

    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        _FakeYDL.calls.append(opts)

    def __enter__(self) -> "_FakeYDL":
        return self

    def __exit__(self, *a: object) -> None: ...

    def download(self, urls: list[str]) -> None:
        if self.opts.get("skip_download"):
            return
        tmp = Path(self.opts["outtmpl"]).parent
        video = self.opts["format"] != "bestaudio/best"
        src = tmp / ("src.mkv" if video else "src.webm")
        inputs = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
        if video:
            inputs = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=5:duration=1", *inputs]
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *inputs, "-shortest", str(src)],
            check=True,
        )
        (tmp / "src.info.json").write_text(json.dumps({"duration": 1}))


@pytest.mark.parametrize("height", [0, 480])
def test_download_keeps_video_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, height: int
) -> None:
    monkeypatch.setattr(download.yt_dlp, "YoutubeDL", _FakeYDL)
    info = download.download("vid", 7, tmp_path, video_height=height)
    assert (tmp_path / "audio" / "7.opus").stat().st_size > 0
    assert (tmp_path / "meta" / "7.info.json").exists()
    assert not (tmp_path / "work" / "7" / "download").exists()
    video = tmp_path / "video" / "7.mkv"
    if height:
        assert video.exists() and info["video_height"] == 240
        assert info["video_bytes"] == video.stat().st_size
        assert _FakeYDL.calls[-2]["merge_output_format"] == "mkv"
    else:
        assert not video.exists() and "video_bytes" not in info
