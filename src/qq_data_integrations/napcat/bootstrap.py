from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from pathlib import Path

from .diagnostics import probe_endpoint
from .runtime import EndpointName, NapCatStartResult, NapCatRuntimeStarter, _pin_runtime_environment
from .settings import NapCatSettings
from .webui_client import NapCatWebUiClient, NapCatWebUiError


class NapCatBootstrapper:
    def __init__(
        self,
        settings: NapCatSettings,
        *,
        runtime_starter: NapCatRuntimeStarter | None = None,
        settings_loader: Callable[[], NapCatSettings] | None = None,
        webui_client_factory: Callable[[NapCatSettings], NapCatWebUiClient] | None = None,
        probe: Callable[[str, str, float], Any] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings
        self._runtime_starter = runtime_starter or NapCatRuntimeStarter(settings)
        self._settings_loader = settings_loader or NapCatSettings.from_env
        self._webui_client_factory = webui_client_factory or _default_webui_client_factory
        self._probe = probe or _default_probe
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep

    def ensure_endpoint(
        self,
        endpoint: EndpointName,
        *,
        timeout_seconds: float = 20.0,
        poll_interval: float = 0.5,
        quick_login_uin: str | None = None,
    ) -> NapCatStartResult:
        result = self._runtime_starter.ensure_endpoint(
            endpoint,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            quick_login_uin=quick_login_uin,
        )
        if endpoint == "webui":
            if not result.ready:
                return result
            active_settings = self._load_settings_for_active_runtime()
            client = self._webui_client_factory(active_settings)
            try:
                client.ensure_authenticated()
            except NapCatWebUiError as exc:
                return NapCatStartResult(
                    endpoint=endpoint,
                    attempted_start=result.attempted_start,
                    launcher_path=active_settings.napcat_launcher_path,
                    napcat_log_path=result.napcat_log_path,
                    message=str(exc) + _napcat_log_hint(result.napcat_log_path),
                )
            finally:
                client.close()
            return result
        if result.ready:
            return result

        webui_result = self._runtime_starter.ensure_endpoint(
            "webui",
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            quick_login_uin=quick_login_uin,
        )
        if not webui_result.ready:
            return result
        if not self._settings.auto_configure_onebot:
            return NapCatStartResult(
                endpoint=endpoint,
                attempted_start=result.attempted_start or webui_result.attempted_start,
                launcher_path=self._settings.napcat_launcher_path,
                napcat_log_path=result.napcat_log_path or webui_result.napcat_log_path,
                message=(
                    f"NapCat WebUI is running, but {endpoint} is not listening. "
                    "Automatic OneBot configuration is disabled; enable it with "
                    "NAPCAT_AUTO_CONFIGURE_ONEBOT=1 or configure the OneBot server manually."
                )
                + _napcat_log_hint(result.napcat_log_path or webui_result.napcat_log_path),
            )

        active_settings = self._load_settings_for_active_runtime()
        client = self._webui_client_factory(active_settings)
        try:
            status = client.check_login_status()
            if not status.effectively_logged_in():
                return NapCatStartResult(
                    endpoint=endpoint,
                    attempted_start=result.attempted_start or webui_result.attempted_start,
                    launcher_path=active_settings.napcat_launcher_path,
                    napcat_log_path=result.napcat_log_path or webui_result.napcat_log_path,
                    message=(
                        "NapCat WebUI is running, but QQ is not logged in. "
                        "Run /login first, then retry the command."
                        + _napcat_log_hint(result.napcat_log_path or webui_result.napcat_log_path)
                    ),
                )
            login_info = client.get_login_info()
            if not login_info.is_usable_session():
                return NapCatStartResult(
                    endpoint=endpoint,
                    attempted_start=result.attempted_start or webui_result.attempted_start,
                    launcher_path=active_settings.napcat_launcher_path,
                    napcat_log_path=result.napcat_log_path or webui_result.napcat_log_path,
                    message=(
                        "NapCat WebUI reports a logged-in state, but did not return usable QQ session info. "
                        "This usually means an old runtime/session is stuck or the login state is inconsistent. "
                        "Restart NapCat or run /login again."
                    )
                    + _napcat_log_hint(result.napcat_log_path or webui_result.napcat_log_path),
                )

            changed = client.ensure_default_onebot_servers(
                http_url=active_settings.http_url,
                ws_url=active_settings.ws_url,
                token=active_settings.access_token,
            )
        except NapCatWebUiError as exc:
            return NapCatStartResult(
                endpoint=endpoint,
                attempted_start=result.attempted_start or webui_result.attempted_start,
                launcher_path=active_settings.napcat_launcher_path,
                napcat_log_path=result.napcat_log_path or webui_result.napcat_log_path,
                message=str(exc) + _napcat_log_hint(result.napcat_log_path or webui_result.napcat_log_path),
            )
        finally:
            client.close()

        _pin_runtime_environment(active_settings)
        active_settings = self._load_settings_for_active_runtime()
        deadline = self._monotonic() + timeout_seconds
        endpoint_url = _endpoint_url(active_settings, endpoint)
        while self._monotonic() < deadline:
            probe = self._probe_endpoint(active_settings, endpoint, 0.25)
            if probe.listening:
                return NapCatStartResult(
                    endpoint=endpoint,
                    attempted_start=result.attempted_start or webui_result.attempted_start,
                    attempted_configure=changed,
                    ready=True,
                    launcher_path=active_settings.napcat_launcher_path,
                    napcat_log_path=result.napcat_log_path or webui_result.napcat_log_path,
                    message=(
                        f"{endpoint} is ready at {endpoint_url}"
                        if not changed
                        else f"Enabled default OneBot HTTP/WS servers and {endpoint} is ready at {endpoint_url}"
                    )
                    + _napcat_log_hint(result.napcat_log_path or webui_result.napcat_log_path),
                )
            self._sleep(poll_interval)

        return NapCatStartResult(
            endpoint=endpoint,
            attempted_start=result.attempted_start or webui_result.attempted_start,
            attempted_configure=changed,
            launcher_path=active_settings.napcat_launcher_path,
            napcat_log_path=result.napcat_log_path or webui_result.napcat_log_path,
            message=(
                f"NapCat WebUI is running, but {endpoint} is still not listening at {endpoint_url}. "
                "Check the OneBot network config in NapCat."
            )
            + _napcat_log_hint(result.napcat_log_path or webui_result.napcat_log_path),
        )

    def _probe_endpoint(self, settings: NapCatSettings, endpoint: EndpointName, timeout: float):
        probe = self._probe
        try:
            return probe(
                endpoint,
                _endpoint_url(settings, endpoint),
                timeout=timeout,
                access_token=settings.webui_token if endpoint == "webui" else settings.access_token,
                use_system_proxy=settings.use_system_proxy,
            )
        except TypeError:
            return probe(endpoint, _endpoint_url(settings, endpoint), timeout)

    def _load_settings_for_active_runtime(self) -> NapCatSettings:
        refreshed = self._settings_loader()
        if _same_runtime_identity(refreshed, self._settings):
            return refreshed
        return self._settings


def _default_webui_client_factory(settings: NapCatSettings) -> NapCatWebUiClient:
    return NapCatWebUiClient(
        settings.webui_url,
        raw_token=settings.webui_token,
        use_system_proxy=settings.use_system_proxy,
    )


def _default_probe(name: str, url: str, timeout: float):
    return probe_endpoint(name, url, timeout=timeout)


def _endpoint_url(settings: NapCatSettings, endpoint: EndpointName) -> str:
    if endpoint == "webui":
        return settings.webui_url
    if endpoint == "onebot_http":
        return settings.http_url
    return settings.ws_url


def _napcat_log_hint(log_path) -> str:
    if not log_path:
        return ""
    return f" NapCat log: {log_path}"


def _same_runtime_identity(left: NapCatSettings, right: NapCatSettings) -> bool:
    return _settings_path_key(left.napcat_dir) == _settings_path_key(right.napcat_dir) and _settings_path_key(
        left.workdir
    ) == _settings_path_key(right.workdir) and _settings_path_key(left.napcat_launcher_path) == _settings_path_key(
        right.napcat_launcher_path
    )


def _settings_path_key(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.resolve())
    except OSError:
        return str(path)
