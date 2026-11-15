"""Client for the shared whisper.cpp server (one request at a time, see CLAUDE.md)."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx


class WhisperError(RuntimeError):
    """The server answered, but not with a transcript."""


@contextmanager
def server_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock shared by every process using lock_path (all containers
    see the same data dir), so only one of our requests reaches Whisper at a time.
    The kernel drops the lock if the process dies, so a crash never leaves it stuck."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the only descriptor releases the flock


def transcribe(
    base_url: str, wav: Path, language: str, prompt: str, timeout_s: float = 600
) -> dict[str, Any]:
    with wav.open("rb") as f:
        r = httpx.post(
            f"{base_url}/inference",
            files={"file": (wav.name, f, "audio/wav")},
            data={
                "language": language,
                "prompt": prompt,
                "response_format": "verbose_json",
                "temperature": "0.0",
                "temperature_inc": "0.2",
            },
            timeout=timeout_s,
        )
    r.raise_for_status()
    return check_response(r.json())


def check_response(body: Any) -> dict[str, Any]:
    """whisper-server reports some failures as HTTP 200 with {"error": ...}; taken as an
    empty transcript they would silently become a hole, so they must fail the step.
    verbose_json always carries a "segments" list (empty for silence)."""
    if not isinstance(body, dict) or "error" in body or not isinstance(body.get("segments"), list):
        raise WhisperError(f"unexpected whisper response: {str(body)[:300]}")
    return body
