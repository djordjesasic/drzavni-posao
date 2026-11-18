"""Load a series' config.toml into a typed settings object.

Every series (TV show, YouTube channel...) lives in series/<name>/: config.toml next to its
seed/ folder (characters, voices, prompts, test questions). DP_SERIES picks the series
(default drzavni-posao); DP_CONFIG, if set, points at a config.toml directly.
"""

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
    # whose description goes into the index: the first of these with a result
    index_enrich_models: tuple[str, ...] = ("sonnet",)
    index_window_s: float = 75.0
    index_hop_s: float = 40.0
    # [worker] temperature limits in °C (dp.worker.Limits field names)
    worker_limits: dict[str, float] = field(default_factory=dict)
    # [download] keep the video up to this height; 0 = audio only
    video_height: int = 0
    # [series] short name (MCP server name) and the title shown to people
    series_name: str = "drzavni-posao"
    series_title: str = "Državni posao"
    # Locks shared by every series on the machine (one Whisper request at a time).
    locks_dir: Path = Path("/locks")
    # [transcribe] prompt: Whisper's initial prompt (names, spelling of the show)
    whisper_prompt: str = ""
    # [catalog] parser: how video titles become episodes (dp.catalog.classify)
    catalog_parser: str = "drzavni_posao"

    @property
    def seed_dir(self) -> Path:
        """The series' seed/ folder (characters, voices, prompts, test questions)."""
        return self.seed_characters.parent

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


def config_path() -> Path:
    """DP_CONFIG, else series/<DP_SERIES>/config.toml (relative to the working directory:
    /app in the containers, the repo root in development)."""
    if explicit := os.environ.get("DP_CONFIG"):
        return Path(explicit)
    return Path("series") / (os.environ.get("DP_SERIES") or "drzavni-posao") / "config.toml"


@cache
def get_settings() -> Settings:
    path = config_path().resolve()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    paths, services, schedule = raw["paths"], raw["services"], raw["schedule"]
    seed = path.parent / "seed"
    series = raw.get("series", {})
    return Settings(
        youtube_channel=raw["youtube_channel"].rstrip("/"),
        data_dir=Path(paths["data_dir"]),
        db=Path(os.environ.get("DP_DB", paths["db"])),
        catalog_dir=Path(paths["catalog_dir"]),
        seed_characters=seed / "characters.yaml",
        seed_pilot=seed / "pilot.yaml",
        series_name=series.get("name", path.parent.name),
        series_title=series.get("title", path.parent.name),
        locks_dir=Path(paths.get("locks_dir", "/locks")),
        whisper_prompt=raw["transcribe"].get("prompt", ""),
        catalog_parser=raw.get("catalog", {}).get("parser", "drzavni_posao"),
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
        index_enrich_models=_models(raw.get("index", {}).get("enrich_model", "sonnet")),
        index_window_s=float(raw.get("index", {}).get("window_s", 75)),
        index_hop_s=float(raw.get("index", {}).get("hop_s", 40)),
        worker_limits={k: float(v) for k, v in raw.get("worker", {}).items()},
        video_height=int(raw["download"].get("video_height", 0)),
    )


def _models(value: str | list[str]) -> tuple[str, ...]:
    """[index] enrich_model: one name or a list, best first."""
    return (value,) if isinstance(value, str) else tuple(value)


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
