import pytest

from dp.config import _allowed_hosts, api_port


def test_hosts_get_api_port_and_bind_addr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_BIND_ADDR", "100.1.2.3")
    monkeypatch.delenv("DP_API_PORT", raising=False)
    api = {"allowed_hosts": ["llmserver"]}
    assert _allowed_hosts(api) == ["llmserver:8090", "100.1.2.3:8090"]


def test_hosts_use_api_port_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_BIND_ADDR", "100.1.2.3")
    monkeypatch.setenv("DP_API_PORT", "9000")
    assert _allowed_hosts({"allowed_hosts": ["llmserver"]}) == [
        "llmserver:9000",
        "100.1.2.3:9000",
    ]


def test_without_bind_addr_only_config_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DP_BIND_ADDR", raising=False)
    monkeypatch.setenv("DP_API_PORT", "1")
    assert _allowed_hosts({"allowed_hosts": ["x"]}) == ["x:1"]


def test_empty_api_port_means_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DP_API_PORT", "")
    assert api_port() == 8090
