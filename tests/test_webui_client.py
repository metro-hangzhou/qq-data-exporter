from __future__ import annotations

import hashlib

import httpx

from qq_data_integrations.napcat import NapCatWebUiClient


def test_webui_client_auth_and_status_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            assert request.headers.get("Authorization") is None
            assert request.content.decode("utf-8") == (
                '{"hash":"'
                + hashlib.sha256("secret.napcat".encode("utf-8")).hexdigest()
                + '"}'
            )
            return httpx.Response(200, json={"code": 0, "message": "success", "data": {"Credential": "cred-1"}})
        if request.url.path.endswith("/QQLogin/CheckLoginStatus"):
            assert request.headers["Authorization"] == "Bearer cred-1"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "isLogin": False,
                        "isOffline": False,
                        "qrcodeurl": "https://qr.example/login",
                        "loginError": "",
                    },
                },
            )
        if request.url.path.endswith("/QQLogin/GetQQLoginInfo"):
            assert request.headers["Authorization"] == "Bearer cred-1"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "success",
                    "data": {
                        "uin": "123456",
                        "nick": "Tester",
                        "online": True,
                        "avatarUrl": "https://avatar.example/1.png",
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatWebUiClient(
        "http://127.0.0.1:6099/api",
        raw_token="secret",
        transport=httpx.MockTransport(handler),
    )
    status = client.check_login_status()
    info = client.get_login_info()

    assert status.is_login is False
    assert status.qrcode_url == "https://qr.example/login"
    assert info.uin == "123456"
    assert info.nick == "Tester"
    client.close()
