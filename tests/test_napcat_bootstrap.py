from __future__ import annotations

from qq_data_integrations.napcat.bootstrap import NapCatBootstrapper
from qq_data_integrations.napcat.models import NapCatLoginInfo, NapCatLoginStatus
from qq_data_integrations.napcat.runtime import NapCatStartResult
from qq_data_integrations.napcat.settings import NapCatSettings
from qq_data_integrations.napcat.webui_client import NapCatWebUiAuthError


class _ReadyRuntimeStarter:
    def __init__(self, results: dict[str, NapCatStartResult]) -> None:
        self._results = results

    def ensure_endpoint(self, endpoint: str, **_kwargs) -> NapCatStartResult:
        return self._results[endpoint]


class _AuthFailingWebUiClient:
    def __init__(self, _settings: NapCatSettings) -> None:
        self.closed = False

    def ensure_authenticated(self) -> str:
        raise NapCatWebUiAuthError("bad token")

    def close(self) -> None:
        self.closed = True


class _GhostLoggedInWebUiClient:
    def __init__(self, _settings: NapCatSettings) -> None:
        self.closed = False

    def check_login_status(self) -> NapCatLoginStatus:
        return NapCatLoginStatus(is_login=True)

    def get_login_info(self) -> NapCatLoginInfo:
        return NapCatLoginInfo(uin=None, nick=None, online=None)

    def ensure_default_onebot_servers(self, **_kwargs) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


def test_bootstrap_webui_requires_authentication_when_runtime_reports_ready(monkeypatch) -> None:
    settings = NapCatSettings.from_env()
    runtime = _ReadyRuntimeStarter(
        {
            "webui": NapCatStartResult(endpoint="webui", ready=True, message="ready"),
        }
    )
    bootstrapper = NapCatBootstrapper(
        settings,
        runtime_starter=runtime,
        webui_client_factory=_AuthFailingWebUiClient,
    )

    result = bootstrapper.ensure_endpoint("webui")

    assert result.ready is False
    assert "bad token" in result.message


def test_bootstrap_rejects_logged_in_status_without_usable_session_info(monkeypatch) -> None:
    settings = NapCatSettings.from_env()
    runtime = _ReadyRuntimeStarter(
        {
            "onebot_http": NapCatStartResult(endpoint="onebot_http", ready=False, message="http missing"),
            "webui": NapCatStartResult(endpoint="webui", ready=True, message="webui ready"),
        }
    )
    bootstrapper = NapCatBootstrapper(
        settings,
        runtime_starter=runtime,
        webui_client_factory=_GhostLoggedInWebUiClient,
    )

    result = bootstrapper.ensure_endpoint("onebot_http")

    assert result.ready is False
    assert "usable QQ session info" in result.message
