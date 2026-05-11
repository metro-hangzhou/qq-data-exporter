from __future__ import annotations

import httpx
import pytest

from qq_data_integrations.napcat.http_client import NapCatApiConnectError, NapCatHttpClient
from qq_data_integrations.napcat.webui_client import NapCatWebUiClient, NapCatWebUiConnectError


def test_webui_client_raises_clear_connect_error(monkeypatch) -> None:
    client = NapCatWebUiClient("http://127.0.0.1:6099/api", raw_token="random")

    class FakeInnerClient:
        def post(self, path: str, **kwargs):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", "http://127.0.0.1:6099/api/auth/login"))

        def close(self) -> None:
            return None

    monkeypatch.setattr(client, "_client", FakeInnerClient())
    with pytest.raises(NapCatWebUiConnectError) as exc_info:
        client.ensure_authenticated()
    assert "http://127.0.0.1:6099/api" in str(exc_info.value)


def test_http_client_raises_clear_connect_error(monkeypatch) -> None:
    client = NapCatHttpClient("http://127.0.0.1:3000")

    class FakeInnerClient:
        def post(self, path: str, json: dict):
            raise httpx.ConnectError("boom", request=httpx.Request("POST", "http://127.0.0.1:3000/get_group_list"))

        def close(self) -> None:
            return None

    monkeypatch.setattr(client, "_client", FakeInnerClient())
    with pytest.raises(NapCatApiConnectError) as exc_info:
        client.get_group_list()
    assert "http://127.0.0.1:3000" in str(exc_info.value)
