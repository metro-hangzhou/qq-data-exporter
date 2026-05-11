from __future__ import annotations

import time
from collections.abc import Callable

from .models import NapCatLoginInfo, NapCatLoginStatus, NapCatQuickLoginAccount
from .webui_client import NapCatWebUiClient


QrCallback = Callable[[str], None]
StatusCallback = Callable[[NapCatLoginStatus], None]


class NapCatQrLoginService:
    def __init__(
        self,
        client: NapCatWebUiClient,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._client = client
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep

    def check_status(self) -> NapCatLoginStatus:
        return self._client.check_login_status()

    def get_login_info(self) -> NapCatLoginInfo:
        return self._client.get_login_info()

    def get_ready_login_info(self) -> NapCatLoginInfo | None:
        info = self.get_login_info()
        if info.is_usable_session():
            return info
        return None

    def get_quick_login_candidates(self) -> list[NapCatQuickLoginAccount]:
        return self._client.get_quick_login_list()

    def get_default_quick_login_uin(self) -> str | None:
        return self._client.get_quick_login_uin()

    def resolve_desired_quick_login_uin(self, *, preferred_uin: str | None = None) -> str | None:
        candidates = self.get_quick_login_candidates()
        if not candidates:
            return None
        return _choose_quick_login_uin(
            preferred_uin=preferred_uin,
            default_uin=self.get_default_quick_login_uin(),
            candidates=candidates,
        )

    def try_quick_login(
        self,
        *,
        preferred_uin: str | None = None,
        timeout_seconds: float = 25.0,
        poll_interval: float = 1.0,
        on_status: StatusCallback | None = None,
    ) -> NapCatLoginInfo | None:
        status = self._client.check_login_status()
        if status.effectively_logged_in():
            return self.get_ready_login_info()

        chosen_uin = self.resolve_desired_quick_login_uin(preferred_uin=preferred_uin)
        if chosen_uin is None:
            return None

        self._client.set_quick_login_uin(chosen_uin)
        self._client.request_quick_login(chosen_uin)

        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            self._sleep(poll_interval)
            status = self._client.check_login_status()
            if on_status:
                on_status(status)
            if status.effectively_logged_in():
                info = self.get_ready_login_info()
                if info is not None:
                    return info
            if status.login_error and "快速登录" in status.login_error:
                return None
        return None

    def login_until_success(
        self,
        *,
        timeout_seconds: float = 300.0,
        poll_interval: float = 3.0,
        refresh: bool = False,
        on_qrcode: QrCallback | None = None,
        on_status: StatusCallback | None = None,
    ) -> NapCatLoginInfo:
        status = self._client.check_login_status()
        if status.effectively_logged_in():
            ready_info = self.get_ready_login_info()
            if ready_info is not None:
                return ready_info

        if refresh or not status.qrcode_url:
            self._client.refresh_qrcode()
            status = self._client.check_login_status()

        qrcode_url = status.qrcode_url or self._client.get_qrcode()
        if on_qrcode:
            on_qrcode(qrcode_url)
        if on_status:
            on_status(status)

        last_qrcode = qrcode_url
        last_error = status.login_error or ""
        deadline = self._monotonic() + timeout_seconds

        while self._monotonic() < deadline:
            self._sleep(poll_interval)
            status = self._client.check_login_status()
            if status.effectively_logged_in():
                ready_info = self.get_ready_login_info()
                if ready_info is not None:
                    return ready_info

            next_error = status.login_error or ""
            if next_error != last_error:
                last_error = next_error
                if on_status:
                    on_status(status)

            if status.qr_expired():
                self._client.refresh_qrcode()
                status = self._client.check_login_status()

            next_qrcode = status.qrcode_url or last_qrcode
            if next_qrcode and next_qrcode != last_qrcode:
                last_qrcode = next_qrcode
                if on_qrcode:
                    on_qrcode(next_qrcode)

        raise TimeoutError("Timed out waiting for QQ QR login confirmation")


def build_session_mismatch_message(*, current_uin: str, requested_uin: str) -> str:
    return (
        "QQ session mismatch. "
        f"current_uin={current_uin} requested_uin={requested_uin}. "
        "Close the current NapCat/QQ session or switch the QQ account, then retry /login."
    )


def detect_session_mismatch(
    service: NapCatQrLoginService,
    *,
    expected_uin: str | None,
) -> str | None:
    desired = str(expected_uin or "").strip()
    if not desired:
        return None
    info = service.get_ready_login_info()
    current_uin = str(getattr(info, "uin", "") or "").strip() if info is not None else ""
    if not current_uin or current_uin == desired:
        return None
    return build_session_mismatch_message(
        current_uin=current_uin,
        requested_uin=desired,
    )


def _choose_quick_login_uin(
    *,
    preferred_uin: str | None,
    default_uin: str | None,
    candidates: list[NapCatQuickLoginAccount],
) -> str | None:
    preferred = str(preferred_uin or "").strip()
    if preferred:
        for candidate in candidates:
            if candidate.uin == preferred:
                return candidate.uin
        return None
    default_value = str(default_uin or "").strip()
    if default_value:
        for candidate in candidates:
            if candidate.uin == default_value:
                return candidate.uin
    return candidates[0].uin if candidates else None
