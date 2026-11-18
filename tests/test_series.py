"""Series are modular: everything series-specific comes from series/<name>/."""

import sqlite3
from pathlib import Path

import pytest

from dp import config
from dp.catalog import classify
from dp.db import connect, migrate
from dp.enrich import prompt

REPO = Path(__file__).parent.parent
DP_SEED = REPO / "series" / "drzavni-posao" / "seed"


def _series(root: Path, name: str, extra: str = "") -> Path:
    """A minimal second series: config.toml and an empty seed/."""
    d = root / "series" / name
    (d / "seed").mkdir(parents=True)
    base = (REPO / "series" / "drzavni-posao" / "config.toml").read_text(encoding="utf-8")
    base = base.replace('name = "drzavni-posao"', f'name = "{name}"')
    base = base.replace('title = "Državni posao"', 'title = "Druga"')
    (d / "config.toml").write_text(base + extra)
    return d


def test_default_series_is_drzavni_posao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_CONFIG", raising=False)
    monkeypatch.delenv("DP_SERIES", raising=False)
    monkeypatch.chdir(REPO)
    config.get_settings.cache_clear()
    try:
        s = config.get_settings()
        assert s.series_name == "drzavni-posao" and s.series_title == "Državni posao"
        assert s.seed_dir == DP_SEED and s.seed_pilot == DP_SEED / "pilot.yaml"
        assert s.whisper_prompt == "Državni posao. " and s.catalog_parser == "drzavni_posao"
        assert s.locks_dir == Path("/locks")
    finally:
        config.get_settings.cache_clear()


def test_dp_series_picks_another_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _series(tmp_path, "druga")
    monkeypatch.delenv("DP_CONFIG", raising=False)
    monkeypatch.setenv("DP_SERIES", "druga")
    monkeypatch.chdir(tmp_path)
    config.get_settings.cache_clear()
    try:
        s = config.get_settings()
        assert s.series_name == "druga" and s.series_title == "Druga"
        assert s.seed_dir == tmp_path / "series" / "druga" / "seed"
    finally:
        config.get_settings.cache_clear()


def test_system_prompt_comes_from_the_series_seed(tmp_path: Path) -> None:
    dp_system = prompt.load_system(DP_SEED)
    assert "„Državni posao“" in dp_system and "JSON" in dp_system
    assert not dp_system.endswith("\n")
    # A series without its own text gets the generic one, which names no show.
    assert prompt.load_system(tmp_path) == prompt.SYSTEM_DEFAULT
    assert "Državni" not in prompt.SYSTEM_DEFAULT


def test_unknown_catalog_parser_is_refused(tmp_path: Path) -> None:
    conn: sqlite3.Connection = connect(tmp_path / "t.sqlite")
    migrate(conn)
    with pytest.raises(ValueError, match="PARSERS"):
        classify.build_episodes(conn, "nepoznat")
    assert classify.build_episodes(conn).episodes == 0  # the default still works
