import argparse
from collections.abc import Iterator
from pathlib import Path

import pytest

from dp import cli
from dp.config import get_settings


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    db = tmp_path / "t.sqlite"
    monkeypatch.setenv("DP_DB", str(db))
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_catalog_stats_on_empty_db(empty_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.cmd_catalog_stats(argparse.Namespace())
    assert "run catalog-build first" in capsys.readouterr().out
