from __future__ import annotations

import asyncio

import pytest

from qq_data_integrations.napcat.http_client import NapCatHttpClient
from qq_data_integrations.napcat.settings import NapCatSettings
from qq_data_integrations.napcat.websocket_client import NapCatWebSocketClient, NapCatWebSocketError
from qq_data_integrations.napcat.webui_client import NapCatWebUiClient


def test_settings_disable_system_proxy_by_default(monkeypatch) -> None:
    monkeypatch.delenv("NAPCAT_USE_SYSTEM_PROXY", raising=False)
    settings = NapCatSettings.from_env()
    assert settings.use_system_proxy is False


def test_settings_can_enable_system_proxy(monkeypatch) -> None:
    monkeypatch.setenv("NAPCAT_USE_SYSTEM_PROXY", "true")
    settings = NapCatSettings.from_env()
    assert settings.use_system_proxy is True


def test_http_client_disables_env_proxy_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setattr("qq_data_integrations.napcat.http_client.httpx.Client", FakeClient)
    client = NapCatHttpClient("http://127.0.0.1:3000")
    assert captured["trust_env"] is False
    client.close()


def test_webui_client_disables_env_proxy_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            return None

    monkeypatch.setattr("qq_data_integrations.napcat.webui_client.httpx.Client", FakeClient)
    client = NapCatWebUiClient("http://127.0.0.1:6099/api", raw_token="random")
    assert captured["trust_env"] is False
    client.close()


def test_websocket_client_disables_env_proxy_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_connect(*args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop")

    async def consume_events() -> None:
        client = NapCatWebSocketClient("ws://127.0.0.1:3001", max_retries=0)
        async for _ in client.iter_events():
            break

    monkeypatch.setattr("qq_data_integrations.napcat.websocket_client.websockets.connect", fake_connect)
    with pytest.raises(NapCatWebSocketError):
        asyncio.run(consume_events())
    assert captured["proxy"] is None
