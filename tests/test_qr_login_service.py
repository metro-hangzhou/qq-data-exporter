from __future__ import annotations

from qq_data_integrations.napcat import NapCatLoginInfo, NapCatLoginStatus, NapCatQrLoginService


class FakeLoginClient:
    def __init__(self) -> None:
        self.refresh_count = 0
        self._statuses = [
            NapCatLoginStatus(is_login=False, qrcode_url=None),
            NapCatLoginStatus(is_login=False, qrcode_url="https://qr.example/1"),
            NapCatLoginStatus(is_login=False, qrcode_url="https://qr.example/1", login_error="二维码已过期，请刷新"),
            NapCatLoginStatus(is_login=False, qrcode_url="https://qr.example/2"),
            NapCatLoginStatus(is_login=True),
        ]

    def check_login_status(self) -> NapCatLoginStatus:
        if not self._statuses:
            return NapCatLoginStatus(is_login=True)
        return self._statuses.pop(0)

    def refresh_qrcode(self) -> None:
        self.refresh_count += 1

    def get_qrcode(self) -> str:
        return "https://qr.example/direct"

    def get_login_info(self) -> NapCatLoginInfo:
        return NapCatLoginInfo(uin="123456", nick="Tester", online=True)


def test_qr_login_service_refreshes_and_succeeds() -> None:
    client = FakeLoginClient()
    qr_urls: list[str] = []
    errors: list[str] = []
    current_time = {"value": 0.0}

    def monotonic() -> float:
        return current_time["value"]

    def sleep(seconds: float) -> None:
        current_time["value"] += seconds

    service = NapCatQrLoginService(client, monotonic=monotonic, sleep=sleep)
    info = service.login_until_success(
        timeout_seconds=20,
        poll_interval=1,
        on_qrcode=qr_urls.append,
        on_status=lambda status: errors.append(status.login_error or ""),
    )

    assert info.uin == "123456"
    assert qr_urls == ["https://qr.example/1", "https://qr.example/2"]
    assert "二维码已过期，请刷新" in errors
    assert client.refresh_count == 2


def test_qr_login_service_treats_already_logged_in_error_as_success() -> None:
    class AlreadyLoggedInClient:
        def check_login_status(self) -> NapCatLoginStatus:
            return NapCatLoginStatus(
                is_login=False,
                login_error="当前账号(2141129832)已登录,无法重复登录",
            )

        def refresh_qrcode(self) -> None:
            raise AssertionError("refresh_qrcode should not be called for already logged-in status")

        def get_qrcode(self) -> str:
            raise AssertionError("get_qrcode should not be called for already logged-in status")

        def get_login_info(self) -> NapCatLoginInfo:
            return NapCatLoginInfo(uin="2141129832", nick="Tester", online=True)

    service = NapCatQrLoginService(AlreadyLoggedInClient())
    info = service.login_until_success(timeout_seconds=1, poll_interval=0.01)

    assert info.uin == "2141129832"
