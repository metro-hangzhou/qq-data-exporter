from __future__ import annotations

from qq_data_integrations.napcat import (
    NapCatBootstrapper,
    NapCatLoginStatus,
    NapCatSettings,
    NapCatStartResult,
)


class _FakeRuntimeStarter:
    def __init__(self, results: dict[str, list[NapCatStartResult]]) -> None:
        self._results = results

    def ensure_endpoint(self, endpoint: str, **_: object) -> NapCatStartResult:
        values = self._results[endpoint]
        if len(values) > 1:
            return values.pop(0)
        return values[0]


class _FakeWebUiClient:
    def __init__(self, *, is_login: bool, on_configure, login_error: str | None = None) -> None:
        self._status = NapCatLoginStatus(is_login=is_login, login_error=login_error)
        self._on_configure = on_configure

    def close(self) -> None:
        return None

    def check_login_status(self) -> NapCatLoginStatus:
        return self._status

    def ensure_default_onebot_servers(self, *, http_url: str, ws_url: str, token: str | None) -> bool:
        self._on_configure(http_url=http_url, ws_url=ws_url, token=token)
        return True


def test_bootstrapper_auto_configures_onebot_when_logged_in() -> None:
    state = {"configured": False}
    settings = NapCatSettings(
        http_url="http://127.0.0.1:3000",
        ws_url="ws://127.0.0.1:3001",
        webui_url="http://127.0.0.1:6099/api",
    )
    starter = _FakeRuntimeStarter(
        {
            "onebot_http": [
                NapCatStartResult(endpoint="onebot_http", message="not ready"),
            ],
            "webui": [
                NapCatStartResult(endpoint="webui", already_running=True, ready=True, message="webui ready"),
            ],
        }
    )

    def fake_probe(name: str, url: str, timeout: float = 0.25):
        listening = name == "onebot_http" and state["configured"]
        return type(
            "Probe",
            (),
            {
                "name": name,
                "url": url,
                "listening": listening,
            },
        )()

    def on_configure(**_: object) -> None:
        state["configured"] = True

    bootstrapper = NapCatBootstrapper(
        settings,
        runtime_starter=starter,
        settings_loader=lambda: settings,
        webui_client_factory=lambda _: _FakeWebUiClient(is_login=True, on_configure=on_configure),
        probe=fake_probe,
        monotonic=iter([0.0, 0.1, 0.2]).__next__,
        sleep=lambda _: None,
    )

    result = bootstrapper.ensure_endpoint("onebot_http", timeout_seconds=1.0, poll_interval=0.0)

    assert result.ready is True
    assert result.attempted_configure is True
    assert "Enabled default OneBot HTTP/WS servers" in result.message


def test_bootstrapper_requires_login_before_configuring_onebot() -> None:
    settings = NapCatSettings(
        http_url="http://127.0.0.1:3000",
        ws_url="ws://127.0.0.1:3001",
        webui_url="http://127.0.0.1:6099/api",
    )
    starter = _FakeRuntimeStarter(
        {
            "onebot_ws": [
                NapCatStartResult(endpoint="onebot_ws", message="not ready"),
            ],
            "webui": [
                NapCatStartResult(endpoint="webui", already_running=True, ready=True, message="webui ready"),
            ],
        }
    )

    bootstrapper = NapCatBootstrapper(
        settings,
        runtime_starter=starter,
        settings_loader=lambda: settings,
        webui_client_factory=lambda _: _FakeWebUiClient(is_login=False, on_configure=lambda **_: None),
        probe=lambda name, url, timeout: type("Probe", (), {"name": name, "url": url, "listening": False})(),
        monotonic=iter([0.0, 0.1]).__next__,
        sleep=lambda _: None,
    )

    result = bootstrapper.ensure_endpoint("onebot_ws", timeout_seconds=1.0, poll_interval=0.0)

    assert result.ready is False
    assert "Run /login first" in result.message


def test_bootstrapper_accepts_already_logged_in_webui_status() -> None:
    state = {"configured": False}
    settings = NapCatSettings(
        http_url="http://127.0.0.1:3000",
        ws_url="ws://127.0.0.1:3001",
        webui_url="http://127.0.0.1:6099/api",
    )
    starter = _FakeRuntimeStarter(
        {
            "onebot_http": [
                NapCatStartResult(endpoint="onebot_http", message="not ready"),
            ],
            "webui": [
                NapCatStartResult(endpoint="webui", already_running=True, ready=True, message="webui ready"),
            ],
        }
    )

    def fake_probe(name: str, url: str, timeout: float = 0.25):
        listening = name == "onebot_http" and state["configured"]
        return type("Probe", (), {"name": name, "url": url, "listening": listening})()

    def on_configure(**_: object) -> None:
        state["configured"] = True

    bootstrapper = NapCatBootstrapper(
        settings,
        runtime_starter=starter,
        settings_loader=lambda: settings,
        webui_client_factory=lambda _: _FakeWebUiClient(
            is_login=False,
            login_error="当前账号(2141129832)已登录,无法重复登录",
            on_configure=on_configure,
        ),
        probe=fake_probe,
        monotonic=iter([0.0, 0.1, 0.2]).__next__,
        sleep=lambda _: None,
    )

    result = bootstrapper.ensure_endpoint("onebot_http", timeout_seconds=1.0, poll_interval=0.0)

    assert result.ready is True
    assert result.attempted_configure is True
