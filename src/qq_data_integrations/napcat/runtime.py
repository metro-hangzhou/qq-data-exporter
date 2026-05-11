from __future__ import annotations

import os
import subprocess
import time
import shlex
from contextlib import suppress
from pathlib import Path
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel

from qq_data_core.paths import atomic_write_text, build_timestamp_token

from .diagnostics import probe_endpoint
from .settings import NapCatSettings


EndpointName = Literal["webui", "onebot_http", "onebot_ws"]
_MAX_LAUNCH_WRAPPERS = 8


class NapCatLaunchInfo(BaseModel):
    napcat_dir: Path | None = None
    launcher_path: Path | None = None
    launchable: bool = False
    reason: str | None = None


class NapCatStartResult(BaseModel):
    endpoint: EndpointName
    already_running: bool = False
    attempted_start: bool = False
    attempted_configure: bool = False
    ready: bool = False
    launcher_path: Path | None = None
    napcat_log_path: Path | None = None
    message: str = ""


class NapCatRuntimeStarter:
    def __init__(
        self,
        settings: NapCatSettings,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        launch_process: Callable[[Path, str | None], Path | None] | None = None,
    ) -> None:
        self._settings = settings
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._launch_process = launch_process or (
            lambda launcher_path, quick_login_uin: _launch_napcat_process(
                launcher_path,
                settings.state_dir,
                quick_login_uin=quick_login_uin or settings.quick_login_uin,
            )
        )

    def describe_launch(self) -> NapCatLaunchInfo:
        launcher_path = self._settings.napcat_launcher_path
        napcat_dir = self._settings.napcat_dir
        if napcat_dir is None:
            return NapCatLaunchInfo(reason="No NapCat directory configured.")
        if launcher_path is None:
            return NapCatLaunchInfo(
                napcat_dir=napcat_dir,
                reason="No launcher script found under the configured NapCat directory.",
            )
        if not _looks_like_launchable_runtime(launcher_path):
            return NapCatLaunchInfo(
                napcat_dir=napcat_dir,
                launcher_path=launcher_path,
                reason="Launcher exists, but the directory does not look like a runnable NapCat release.",
            )
        return NapCatLaunchInfo(
            napcat_dir=napcat_dir,
            launcher_path=launcher_path,
            launchable=True,
        )

    def ensure_endpoint(
        self,
        endpoint: EndpointName,
        *,
        timeout_seconds: float = 20.0,
        poll_interval: float = 0.5,
        quick_login_uin: str | None = None,
    ) -> NapCatStartResult:
        url = _endpoint_url(self._settings, endpoint)
        current = _probe_configured_endpoint(self._settings, endpoint)
        if current.listening:
            _pin_runtime_environment(self._settings)
            return NapCatStartResult(
                endpoint=endpoint,
                already_running=True,
                ready=True,
                launcher_path=self._settings.napcat_launcher_path,
                message=f"{endpoint} already listening at {url}",
            )
        if current.transport_listening and not current.protocol_identified:
            warmed_probe = self._retry_protocol_identification(
                endpoint,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
            if warmed_probe:
                _pin_runtime_environment(self._settings)
                return NapCatStartResult(
                    endpoint=endpoint,
                    launcher_path=self._settings.napcat_launcher_path,
                    ready=warmed_probe.listening,
                    message=(
                        f"{endpoint} port at {url} is already occupied; NapCat identified after a short warm-up. "
                        f"detail={warmed_probe.detail or 'protocol signature matched'}"
                    ),
                )
            return NapCatStartResult(
                endpoint=endpoint,
                launcher_path=self._settings.napcat_launcher_path,
                message=(
                    f"{endpoint} port is already occupied at {url}, but the service does not look like NapCat. "
                    f"detail={current.detail or 'unknown'} "
                    "Observed the port briefly but the protocol signature never matched."
                ),
            )
        if current.protocol_identified:
            return NapCatStartResult(
                endpoint=endpoint,
                launcher_path=self._settings.napcat_launcher_path,
                message=(
                    f"{endpoint} is responding at {url}, but it is not ready for the current CLI settings. "
                    f"detail={current.detail or 'protocol check failed'}"
                ),
            )

        if endpoint != "webui":
            for sibling in ("webui", "onebot_http", "onebot_ws"):
                if sibling == endpoint:
                    continue
                sibling_probe = _probe_configured_endpoint(self._settings, sibling)
                if sibling_probe.protocol_identified:
                    _pin_runtime_environment(self._settings)
                    return NapCatStartResult(
                        endpoint=endpoint,
                        launcher_path=self._settings.napcat_launcher_path,
                        message=(
                            f"NapCat is already running, but {endpoint} is not listening at {url}. "
                            "Enable the matching OneBot server in NapCat, or let the CLI configure it after /login."
                        ),
                    )

        launch_info = self.describe_launch()
        if not self._settings.auto_start_napcat:
            return NapCatStartResult(
                endpoint=endpoint,
                launcher_path=launch_info.launcher_path,
                message=f"{endpoint} is not listening and auto-start is disabled.",
            )
        if not launch_info.launchable or launch_info.launcher_path is None:
            return NapCatStartResult(
                endpoint=endpoint,
                launcher_path=launch_info.launcher_path,
                message=launch_info.reason or f"{endpoint} is not listening and no launchable NapCat runtime was found.",
            )

        napcat_log_path = self._launch_process(
            launch_info.launcher_path,
            quick_login_uin,
        )
        effective_launcher = _read_napcat_effective_launcher(napcat_log_path) or launch_info.launcher_path
        deadline = self._monotonic() + timeout_seconds
        while self._monotonic() < deadline:
            self._sleep(poll_interval)
            probe = _probe_configured_endpoint(self._settings, endpoint)
            if probe.listening:
                _pin_runtime_environment(self._settings)
                return NapCatStartResult(
                    endpoint=endpoint,
                    attempted_start=True,
                    ready=True,
                    launcher_path=effective_launcher,
                    napcat_log_path=napcat_log_path,
                    message=_append_napcat_log_hint(
                        f"Started NapCat via {effective_launcher}",
                        napcat_log_path,
                    ),
                )
        return NapCatStartResult(
            endpoint=endpoint,
            attempted_start=True,
            launcher_path=effective_launcher,
            napcat_log_path=napcat_log_path,
            message=_append_napcat_log_hint(
                f"Tried to start NapCat via {effective_launcher}, but {endpoint} at {url} "
                "did not become ready in time.",
                napcat_log_path,
            ),
        )

    def _retry_protocol_identification(
        self,
        endpoint: EndpointName,
        *,
        timeout_seconds: float,
        poll_interval: float,
    ):
        deadline = self._monotonic() + min(timeout_seconds, 2.0)
        sleep_interval = min(poll_interval, 0.25)
        while self._monotonic() < deadline:
            self._sleep(sleep_interval)
            probe = _probe_configured_endpoint(self._settings, endpoint)
            if probe.protocol_identified:
                return probe
            if not probe.transport_listening:
                return None
        return None


def _endpoint_url(settings: NapCatSettings, endpoint: EndpointName) -> str:
    if endpoint == "webui":
        return settings.webui_url
    if endpoint == "onebot_http":
        return settings.http_url
    return settings.ws_url


def _looks_like_launchable_runtime(launcher_path: Path) -> bool:
    launcher_dir = launcher_path.parent
    node_runtime_files = [
        launcher_dir / "node.exe",
        launcher_dir / "index.js",
        launcher_dir / "wrapper.node",
        launcher_dir / "napcat" / "napcat.mjs",
    ]
    if all(path.exists() for path in node_runtime_files):
        return True
    release_files = [
        launcher_dir / "NapCatWinBootMain.exe",
        launcher_dir / "NapCatWinBootHook.dll",
    ]
    if all(path.exists() for path in release_files):
        return True
    source_files = [
        launcher_dir / "NapCatWinBootMain.exe",
        launcher_dir / "NapCatWinBootHook.dll",
        launcher_dir / "qqnt.json",
        launcher_dir / "napcat.mjs",
    ]
    return all(path.exists() for path in source_files)


def _launch_napcat_process(
    launcher_path: Path,
    state_dir: Path,
    *,
    quick_login_uin: str | None = None,
) -> Path | None:
    log_path, wrapper_path, latest_pointer_path = _prepare_napcat_launch_artifacts(
        launcher_path,
        state_dir,
        quick_login_uin=quick_login_uin,
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        ["cmd.exe", "/d", "/c", str(wrapper_path)],
        cwd=str(launcher_path.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    atomic_write_text(latest_pointer_path, str(log_path), encoding="utf-8")
    return log_path


def _prepare_napcat_launch_artifacts(
    launcher_path: Path,
    state_dir: Path,
    *,
    quick_login_uin: str | None = None,
) -> tuple[Path, Path, Path]:
    logs_dir = state_dir / "napcat_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    _prune_old_launch_wrappers(logs_dir)
    stamp = build_timestamp_token(include_pid=True)
    log_path = logs_dir / f"napcat_{stamp}.log"
    wrapper_path = logs_dir / f"launch_napcat_{stamp}.cmd"
    latest_pointer_path = logs_dir / "latest.path"
    effective_launcher, launcher_override = _resolve_effective_launch_entrypoint(launcher_path, state_dir)
    launch_command = f'call "{effective_launcher}"'
    normalized_uin = str(quick_login_uin or "").strip()
    if normalized_uin:
        launch_command = f'{launch_command} -q {shlex.quote(normalized_uin)}'
    wrapper_lines = [
        "@echo off",
        f'cd /d "{effective_launcher.parent}"',
    ]
    if launcher_override is not None:
        wrapper_lines.append(f'set "NAPCAT_LAUNCHER_OVERRIDE={launcher_override}"')
    if normalized_uin:
        wrapper_lines.append(f'set "NAPCAT_QUICK_ACCOUNT={normalized_uin}"')
        wrapper_lines.append(f'echo wrapper_quick_account_env={normalized_uin} >> "{log_path}"')
    wrapper_lines.extend(
        [
            f'echo wrapper_effective_launcher={effective_launcher} >> "{log_path}"',
            (
                f'echo wrapper_launcher_override={launcher_override} >> "{log_path}"'
                if launcher_override is not None
                else "rem no launcher override"
            ),
            f'echo wrapper_launch_command={launch_command} >> "{log_path}"',
            f'{launch_command} >> "{log_path}" 2>&1',
        ]
    )
    atomic_write_text(
        wrapper_path,
        "\n".join(wrapper_lines) + "\n",
        encoding="utf-8",
    )
    return log_path, wrapper_path, latest_pointer_path


def _resolve_effective_launch_entrypoint(launcher_path: Path, state_dir: Path) -> tuple[Path, str | None]:
    try:
        project_root = state_dir.resolve().parent
    except OSError:
        project_root = state_dir.parent
    helper_launcher = project_root / "start_napcat_logged.bat"
    try:
        resolved_launcher = launcher_path.resolve()
    except OSError:
        resolved_launcher = launcher_path
    if helper_launcher.exists():
        try:
            resolved_helper = helper_launcher.resolve()
        except OSError:
            resolved_helper = helper_launcher
        if resolved_helper != resolved_launcher:
            return resolved_helper, str(resolved_launcher)
    return resolved_launcher, None


def _prune_old_launch_wrappers(logs_dir: Path) -> None:
    with suppress(OSError):
        wrappers = sorted(
            logs_dir.glob("launch_napcat_*.cmd"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for stale in wrappers[_MAX_LAUNCH_WRAPPERS:]:
            with suppress(OSError):
                stale.unlink()


def _append_napcat_log_hint(message: str, log_path: Path | None) -> str:
    diagnosis = _read_napcat_launch_diagnosis(log_path)
    if diagnosis:
        message = f"{message} detail={diagnosis}"
    if log_path is None:
        return message
    return f"{message} NapCat log: {log_path}"


def _read_napcat_launch_diagnosis(log_path: Path | None) -> str | None:
    if log_path is None or not log_path.exists() or not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    if not lines:
        return None
    joined = "\n".join(lines[-80:])
    if "Please run this script in administrator mode." in joined:
        return "NapCat launcher exited immediately because administrator mode is required."
    if "Cannot find module" in joined:
        return "NapCat runtime is missing a required local module/dependency."
    return None


def _read_napcat_effective_launcher(log_path: Path | None) -> Path | None:
    if log_path is None or not log_path.exists() or not log_path.is_file():
        return None
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("wrapper_effective_launcher="):
            continue
        value = line.partition("=")[2].strip()
        if not value:
            return None
        candidate = Path(value)
        try:
            return candidate.resolve()
        except OSError:
            return candidate
    return None


def get_latest_napcat_launch_log_path(state_dir: Path) -> Path | None:
    latest_pointer_path = state_dir / "napcat_logs" / "latest.path"
    if not latest_pointer_path.exists():
        return None
    try:
        raw = latest_pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (latest_pointer_path.parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        allowed_dir = (state_dir / "napcat_logs").resolve()
    except OSError:
        allowed_dir = state_dir / "napcat_logs"
    if candidate.parent != allowed_dir:
        return None
    if candidate.suffix.casefold() != ".log":
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate


def _probe_configured_endpoint(settings: NapCatSettings, endpoint: EndpointName):
    token = settings.webui_token if endpoint == "webui" else settings.access_token
    return probe_endpoint(
        endpoint,
        _endpoint_url(settings, endpoint),
        access_token=token,
        use_system_proxy=settings.use_system_proxy,
    )


def _pin_runtime_environment(settings: NapCatSettings) -> None:
    _set_path_env("NAPCAT_DIR", settings.napcat_dir)
    _set_path_env("NAPCAT_LAUNCHER", settings.napcat_launcher_path)
    _set_path_env("NAPCAT_WORKDIR", settings.workdir)
    _set_path_env("NAPCAT_ONEBOT_CONFIG", settings.onebot_config_path)
    _set_path_env("NAPCAT_WEBUI_CONFIG", settings.webui_config_path)


def _set_path_env(key: str, value: Path | None) -> None:
    if value is None:
        return
    os.environ[key] = str(value)
