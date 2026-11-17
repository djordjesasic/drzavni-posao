"""Still frames from an episode's video, for descriptions that also see the picture."""

import base64
import subprocess
from pathlib import Path


def video_path(data_dir: Path, episode_id: int) -> Path:
    return data_dir / "video" / f"{episode_id}.mkv"


def duration_s(path: Path) -> float:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
         str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return float(res.stdout.strip())


def extract(path: Path, every_s: float = 20.0, width: int = 512) -> list[tuple[int, bytes]]:
    """One JPEG every `every_s` seconds, starting at every_s / 2: [(ms, jpeg bytes)]."""
    out: list[tuple[int, bytes]] = []
    total = duration_s(path)
    t = every_s / 2
    while t < total:
        res = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(path),
             "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2",
             "-c:v", "mjpeg", "pipe:1"],
            capture_output=True, check=True,
        )  # fmt: skip
        if res.stdout:
            out.append((int(t * 1000), res.stdout))
        t += every_s
    return out


def to_blocks(frames: list[tuple[int, bytes]]) -> list[dict[str, object]]:
    """Messages API content blocks: a "[m:ss]" label before each image."""
    blocks: list[dict[str, object]] = []
    for ms, jpeg in frames:
        s = ms // 1000
        blocks.append({"type": "text", "text": f"[{s // 60}:{s % 60:02d}]"})
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(jpeg).decode(),
                },
            }
        )
    return blocks
