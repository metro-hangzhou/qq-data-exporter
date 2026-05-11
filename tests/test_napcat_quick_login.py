from __future__ import annotations

from qq_data_integrations.napcat.login import NapCatQrLoginService
from qq_data_integrations.napcat.models import NapCatLoginInfo, NapCatLoginStatus, NapCatQuickLoginAccount


class _QuickLoginClient:
    def __init__(self, *, fail: bool = False, already_logged_in: bool = False) -> None:
        self.fail = fail
        self.already_logged_in = already_logged_in
        self.selected_uin: str | None = None
        self.requested_uin: str | None = None
        self._checks = 0

    def check_login_status(self) -> NapCatLoginStatus:
        self._checks += 1
        if self.already_logged_in:
            return NapCatLoginStatus(is_login=True)
        if self.fail and self._checks > 1:
            return NapCatLoginStatus(login_error="快速登录失败")
        if not self.fail and self.requested_uin and self._checks > 1:
            return NapCatLoginStatus(is_login=True)
        return NapCatLoginStatus()

    def get_login_info(self) -> NapCatLoginInfo:
        return NapCatLoginInfo(uin=self.requested_uin or "3956020260", nick="wiki", online=True)

    def get_quick_login_list(self) -> list[NapCatQuickLoginAccount]:
        return [
            NapCatQuickLoginAccount(uin="111", nick_name="alpha"),
            NapCatQuickLoginAccount(uin="222", nick_name="beta"),
        ]

    def get_quick_login_uin(self) -> str | None:
        return "222"

    def set_quick_login_uin(self, uin: str) -> None:
        self.selected_uin = uin

    def request_quick_login(self, uin: str) -> None:
        self.requested_uin = uin


def test_try_quick_login_prefers_default_uin_and_returns_login_info() -> None:
    client = _QuickLoginClient()
    service = NapCatQrLoginService(client, sleep=lambda _seconds: None)

    info = service.try_quick_login()

    assert info is not None
    assert info.uin == "222"
    assert client.selected_uin == "222"
    assert client.requested_uin == "222"


def test_try_quick_login_returns_none_when_quick_login_fails() -> None:
    client = _QuickLoginClient(fail=True)
    service = NapCatQrLoginService(client, sleep=lambda _seconds: None)

    info = service.try_quick_login(preferred_uin="111")

    assert info is None
    assert client.selected_uin == "111"
    assert client.requested_uin == "111"


def test_try_quick_login_returns_existing_session_without_requesting_quick_login() -> None:
    client = _QuickLoginClient(already_logged_in=True)
    service = NapCatQrLoginService(client, sleep=lambda _seconds: None)

    info = service.try_quick_login(preferred_uin="111")

    assert info is not None
    assert info.uin == "3956020260"
    assert client.selected_uin is None
    assert client.requested_uin is None
