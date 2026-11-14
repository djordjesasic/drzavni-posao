"""Load config.toml into a typed settings object."""

import os
import tomllib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    youtube_channel: str
    data_dir: Path
    db: Path
    catalog_dir: Path
    seed_characters: Path
    seed_pilot: Path
    whisper_url: str
    llama_url: str
    pause_start: str
    pause_end: str
    api_allowed_hosts: list[str]
    api_token: str | None
    download_pause_s: tuple[float, float]
    language: str
    chunk_target_s: float
    chunk_max_s: float
    edge_s: float = 30.0
    # name -> {base_url, model, local?, api_key_env?, max_tokens?, extra?}
    enrich_models: dict[str, dict[str, Any]] = field(default_factory=dict)
    embed_url: str = "http://embed"
    index_enrich_model: str = "sonnet"  # whose description goes into the index
    index_window_s: float = 75.0
    index_hop_s: float = 40.0

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def work_dir(self) -> Path:
        """Per-episode intermediate files (16 kHz WAV, Whisper chunk output)."""
        return self.data_dir / "work"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"


@cache
def get_settings() -> Settings:
    path = Path(os.environ.get("DP_CONFIG", "config.toml"))
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    paths, services, schedule = raw["paths"], raw["services"], raw["schedule"]
    return Settings(
        youtube_channel=raw["youtube_channel"].rstrip("/"),
        data_dir=Path(paths["data_dir"]),
        db=Path(os.environ.get("DP_DB", paths["db"])),
        catalog_dir=Path(paths["catalog_dir"]),
        seed_characters=Path(paths["seed_characters"]),
        seed_pilot=Path(paths["seed_pilot"]),
        whisper_url=services["whisper_url"],
        llama_url=services["llama_url"],
        pause_start=schedule["pause_start"],
        pause_end=schedule["pause_end"],
        api_allowed_hosts=_allowed_hosts(raw["api"]),
        api_token=os.environ.get("DP_API_TOKEN") or None,
        download_pause_s=(
            float(raw["download"]["pause_min_s"]),
            float(raw["download"]["pause_max_s"]),
        ),
        language=raw["transcribe"]["language"],
        chunk_target_s=float(raw["transcribe"]["chunk_target_s"]),
        chunk_max_s=float(raw["transcribe"]["chunk_max_s"]),
        edge_s=float(raw["transcribe"].get("edge_s", 30)),
        enrich_models=raw.get("enrich", {}).get("models", {}),
        embed_url=os.environ.get("DP_EMBED_URL") or services.get("embed_url", "http://embed"),
        index_enrich_model=raw.get("index", {}).get("enrich_model", "sonnet"),
        index_window_s=float(raw.get("index", {}).get("window_s", 75)),
        index_hop_s=float(raw.get("index", {}).get("hop_s", 40)),
    )


def api_port() -> int:
    """API port; the same DP_API_PORT variable is used by compose.yaml."""
    return int(os.environ.get("DP_API_PORT") or 8090)


def _allowed_hosts(api: dict[str, Any]) -> list[str]:
    """Host headers accepted on /mcp: configured hostnames plus DP_BIND_ADDR, with the port."""
    names = [str(h) for h in api.get("allowed_hosts", [])]
    bind = os.environ.get("DP_BIND_ADDR")
    if bind:
        names.append(bind)
    return [f"{name}:{api_port()}" for name in names]
