"""Client for the shared whisper.cpp server (one request at a time, see CLAUDE.md)."""

from pathlib import Path
from typing import Any

import httpx


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
    return r.json()
