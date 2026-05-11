from __future__ import annotations

import importlib.metadata as importlib_metadata
import locale
import os
import platform
import re
from collections import deque
import shutil
import socket
import struct
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

import orjson

from qq_data_cli.logging_utils import get_cli_log_path, get_cli_logger, get_latest_cli_log_path
from qq_data_cli.terminal_compat import TerminalProbe, TerminalUiDecision
from qq_data_core.paths import atomic_write_bytes, atomic_write_text, build_timestamp_token
from qq_data_integrations.napcat.settings import NapCatSettings

_SESSION_CAPTURE_PATH: Path | None = None
_SESSION_CAPTURE_LOCK = Lock()
_MAX_QQ_ROOT_SNAPSHOTS = 8
_MAX_DIR_ENTRIES = 24
_MAX_NESTED_ENTRIES = 12
_MAX_LOG_TAIL_LINES = 80
_MAX_JSON_STRING = 400
_MAX_ARGV_ENTRIES = 32
_MAX_SYS_PATH_ENTRIES = 24
_PACKAGE_VERSION_KEYS = (
    "typer",
    "rich",
    "prompt-toolkit",
    "httpx",
    "orjson",
    "pydantic",
    "websockets",
)
_ENV_KEYS = (
    "PROJECT_ROOT",
    "EXPORT_DIR",
    "STATE_DIR",
    "QQ_MEDIA_ROOTS",
    "NAPCAT_DIR",
    "NAPCAT_LAUNCHER",
    "NAPCAT_WORKDIR",
    "NAPCAT_HTTP_URL",
    "NAPCAT_WS_URL",
    "NAPCAT_WEBUI_URL",
    "NAPCAT_FAST_HISTORY_MODE",
    "NAPCAT_FAST_HISTORY_URL",
    "NAPCAT_FAST_HISTORY_PLUGIN_ID",
    "NAPCAT_AUTO_START",
    "NAPCAT_USE_SYSTEM_PROXY",
    "CLI_UI_MODE",
    "WT_SESSION",
    "TERM",
    "TERM_PROGRAM",
    "COMSPEC",
    "OS",
    "PROCESSOR_IDENTIFIER",
    "NUMBER_OF_PROCESSORS",
)

_CAPTURE_MODE_ENV = "STARTUP_CAPTURE_MODE"
_CAPTURE_MODE_DEBUG_VALUES = {"debug", "full", "1", "true", "verbose"}
_CAPTURE_MODE_SHARE = "share"
_CAPTURE_MODE_DEBUG = "debug"
_CAPTURE_PROFILE_STARTUP = "startup"
_CAPTURE_PROFILE_FULL = "full"
_SHARE_LOG_TAIL_LINES = 20
_SHARE_MAX_ARGV_ENTRIES = 16
_SHARE_ENV_KEYS = {
    key
    for key in _ENV_KEYS
    if key.startswith("NAPCAT") or key in {"WT_SESSION", "TERM", "TERM_PROGRAM"}
}
_SHARE_MAX_QQ_ROOT_SNAPSHOTS = 3
_SHARE_MAX_DIRECTORY_ENTRIES = 6
_SHARE_MAX_NESTED_ENTRIES = 3
_SHARE_PATH_BASENAME_KEYS = {
    "cli_latest_log_path",
    "config_path",
    "latest_napcat_log_path",
    "log_path",
    "napcat_latest_log_path",
    "napcat_launcher_path",
    "onebot_config_path",
    "startup_capture_dir",
    "webui_config_path",
}
_SHARE_PATH_KEY_SUFFIXES = ("_path", "_dir", "_root")
_PATHISH_CAPTURE_KEYS = {
    "cwd",
    "project_root",
    "export_dir",
    "state_dir",
    "napcat_dir",
    "workdir",
    "launcher_path",
    "onebot_config_path",
    "webui_config_path",
    "path",
    "sample_path",
    "startup_capture_dir",
    "log_path",
    "latest_napcat_log_path",
    "cli_latest_log_path",
    "napcat_latest_log_path",
}
_BASENAME_ONLY_CAPTURE_KEYS = {
    "onebot_config_path",
    "webui_config_path",
    "log_path",
    "latest_napcat_log_path",
    "cli_latest_log_path",
    "napcat_latest_log_path",
}


def _capture_mode() -> str:
    raw = (os.getenv(_CAPTURE_MODE_ENV) or "").strip().casefold()
    if raw in _CAPTURE_MODE_DEBUG_VALUES:
        return _CAPTURE_MODE_DEBUG
    return _CAPTURE_MODE_SHARE


def _capture_mode_is_debug(mode: str) -> bool:
    return mode == _CAPTURE_MODE_DEBUG


def _log_tail_lines_for_mode(share_safe: bool) -> int:
    return _SHARE_LOG_TAIL_LINES if share_safe else _MAX_LOG_TAIL_LINES


def _selected_env_payload(share_safe: bool) -> dict[str, str]:
    keys = _SHARE_ENV_KEYS if share_safe else _ENV_KEYS
    payload: dict[str, str] = {}
    for key in keys:
        value = os.getenv(key)
        if value is None:
            continue
        payload[key] = _share_env_value(key, value, share_safe)
    return payload


def _share_env_value(key: str, value: str, share_safe: bool) -> str:
    trimmed = _trim_text(value, limit=_MAX_JSON_STRING)
    if not share_safe:
        return trimmed
    if key.endswith(_SHARE_PATH_KEY_SUFFIXES) or key in {"PROJECT_ROOT", "EXPORT_DIR", "STATE_DIR"}:
        return _mask_path_for_sharing(trimmed, share_safe)
    return trimmed


def _mask_path_for_sharing(value: str | None, share_safe: bool, *, basename_only: bool = False) -> str | None:
    if value is None or not share_safe:
        return value
    try:
        candidate = Path(value)
    except Exception:
        return "<redacted>"
    if basename_only:
        return candidate.name or "<redacted>"
    return _share_path_label(candidate)


def _share_path_label(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    anchor = resolved.anchor or resolved.drive
    name = resolved.name
    if anchor and name and name.casefold() != anchor.rstrip("\\/").casefold():
        return f"{anchor}...\\{name}"
    if anchor:
        return anchor
    return name or "<redacted>"


def _share_path_summary(path: Path, share_safe: bool) -> str:
    if not share_safe:
        return str(path)
    return _share_path_label(path)


def _log_path_summary(path: Path | None, share_safe: bool) -> str | None:
    if path is None:
        return None
    if share_safe:
        return path.name or "<redacted>"
    return str(path)


def _argv_payload(share_safe: bool) -> list[str]:
    payload: list[str] = []
    limit = _SHARE_MAX_ARGV_ENTRIES if share_safe else _MAX_ARGV_ENTRIES
    for index, item in enumerate(sys.argv[:limit]):
        payload.append(_share_argument(item, share_safe, index=index))
    return payload


def _share_argument(value: str, share_safe: bool, *, index: int) -> str:
    trimmed = _trim_text(str(value), limit=_MAX_JSON_STRING)
    if not share_safe:
        return trimmed
    if index == 0:
        return _mask_path_for_sharing(trimmed, True, basename_only=True) or "<redacted>"
    if trimmed.startswith("--") or trimmed.startswith("/"):
        prefix, separator, suffix = trimmed.partition("=")
        if separator and _looks_like_pathish_value(suffix):
            masked = _mask_path_for_sharing(suffix, True, basename_only=True) or "<redacted>"
            return f"{prefix}={masked}"
        return trimmed
    if _looks_like_pathish_value(trimmed):
        return _mask_path_for_sharing(trimmed, True, basename_only=True) or "<redacted>"
    return trimmed


def _looks_like_pathish_value(value: str) -> bool:
    stripped = value.strip()
    if "://" in stripped:
        return False
    if stripped.startswith("\\\\"):
        return True
    if len(stripped) >= 3 and stripped[1:3] == ":\\":
        return True
    return "\\" in stripped or "/" in stripped


def _sanitize_log_lines(lines: list[str], share_safe: bool) -> list[str]:
    if not share_safe:
        return lines
    return [_sanitize_windows_paths_in_line(line) for line in lines]


def _sanitize_windows_paths_in_line(line: str) -> str:
    sanitized = re.sub(r"file:///[A-Za-z]:/[^\s\"')]+", "<path>", line)
    sanitized = re.sub(r"[A-Za-z]:[\\/][^\s\"')]+", "<path>", sanitized)
    return sanitized


def _sanitize_capture_value(value: Any, share_safe: bool, *, key: str | None = None) -> Any:
    if not share_safe:
        return value
    if isinstance(value, dict):
        return {
            item_key: _sanitize_capture_value(item_value, True, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_capture_value(item, True, key=key) for item in value]
    if isinstance(value, str) and key in _PATHISH_CAPTURE_KEYS:
        return _mask_path_for_sharing(
            value,
            True,
            basename_only=key in _BASENAME_ONLY_CAPTURE_KEYS,
        )
    return value


def capture_startup_snapshot(
    settings: NapCatSettings,
    *,
    terminal_probe: TerminalProbe | None = None,
    ui_decision: TerminalUiDecision | None = None,
    force_refresh: bool = False,
    capture_profile: str = _CAPTURE_PROFILE_STARTUP,
) -> Path | None:
    global _SESSION_CAPTURE_PATH

    with _SESSION_CAPTURE_LOCK:
        capture_dir = settings.state_dir / "startup_capture"
        if (
            not force_refresh
            and _SESSION_CAPTURE_PATH is not None
            and _SESSION_CAPTURE_PATH.exists()
            and capture_dir.resolve() in _SESSION_CAPTURE_PATH.resolve().parents
        ):
            return _SESSION_CAPTURE_PATH
        try:
            capture_dir.mkdir(parents=True, exist_ok=True)
            stamp = build_timestamp_token(include_pid=True)
            capture_path = capture_dir / f"startup_{stamp}.json"
            mode = _capture_mode()
            payload = _build_startup_payload(
                settings,
                terminal_probe=terminal_probe,
                ui_decision=ui_decision,
                capture_mode=mode,
                capture_profile=capture_profile,
            )
            encoded = orjson.dumps(payload, option=orjson.OPT_INDENT_2)
            atomic_write_bytes(capture_path, encoded)
            atomic_write_text(capture_dir / "latest.path", str(capture_path), encoding="utf-8")
            _SESSION_CAPTURE_PATH = capture_path
            get_cli_logger("startup_capture").info(
                "startup_capture_ready path=%s mode=%s profile=%s",
                capture_path,
                mode,
                capture_profile,
            )
            return capture_path
        except Exception:
            get_cli_logger("startup_capture").exception("startup_capture_failed")
            return None


def get_session_startup_capture_path() -> Path | None:
    return _SESSION_CAPTURE_PATH


def get_latest_startup_capture_path(state_dir: Path) -> Path | None:
    session_path = _SESSION_CAPTURE_PATH
    if (
        session_path is not None
        and session_path.exists()
        and (state_dir / "startup_capture").resolve() in session_path.resolve().parents
    ):
        return session_path
    latest_path = state_dir / "startup_capture" / "latest.path"
    try:
        if latest_path.exists():
            resolved = _read_latest_pointer(
                latest_path,
                allowed_dir=state_dir / "startup_capture",
                allowed_suffix=".json",
            )
            if resolved is not None:
                return resolved
    except OSError:
        return None
    return None


def _build_startup_payload(
    settings: NapCatSettings,
    *,
    terminal_probe: TerminalProbe | None,
    ui_decision: TerminalUiDecision | None,
    capture_mode: str,
    capture_profile: str,
) -> dict[str, Any]:
    full_capture = capture_profile == _CAPTURE_PROFILE_FULL
    roots: list[Path] = []
    settings_resolution_diagnostics: dict[str, Any] | None = None
    preflight_evidence: dict[str, Any] | None = None
    if full_capture:
        from qq_data_integrations import discover_qq_media_roots
        from qq_data_integrations.napcat.diagnostics import collect_debug_preflight_evidence
        from qq_data_integrations.napcat.settings import build_settings_resolution_diagnostics

        roots = discover_qq_media_roots()
        settings_resolution_diagnostics = build_settings_resolution_diagnostics(settings)
        preflight_evidence = collect_debug_preflight_evidence(settings)
    else:
        from qq_data_integrations.napcat.settings import build_settings_resolution_diagnostics

        settings_resolution_diagnostics = build_settings_resolution_diagnostics(settings)
    probe_payload = terminal_probe
    decision_payload = ui_decision
    share_safe = not _capture_mode_is_debug(capture_mode)
    qq_media_roots_payload = _qq_media_roots_payload(
        roots,
        share_safe,
        deferred=not full_capture,
    )
    env_payload = _selected_env_payload(share_safe)
    napcat_payload: dict[str, Any] = {
        "project_root": _mask_path_for_sharing(str(settings.project_root), share_safe),
        "export_dir": _mask_path_for_sharing(str(settings.export_dir), share_safe),
        "state_dir": _mask_path_for_sharing(str(settings.state_dir), share_safe),
        "http_url": settings.http_url,
        "ws_url": settings.ws_url,
        "webui_url": settings.webui_url,
        "auto_start_napcat": settings.auto_start_napcat,
        "auto_configure_onebot": settings.auto_configure_onebot,
        "use_system_proxy": settings.use_system_proxy,
        "fast_history_mode": settings.fast_history_mode,
        "fast_history_url": settings.fast_history_url,
        "fast_history_plugin_id": settings.fast_history_plugin_id,
        "latest_napcat_log_path": _log_path_summary(_get_latest_napcat_log_path(settings.state_dir), share_safe),
        "settings_resolution_diagnostics": _sanitize_capture_value(
            settings_resolution_diagnostics,
            share_safe,
        ),
    }
    if full_capture:
        napcat_payload["config_snapshots"] = _napcat_config_snapshots(settings, share_safe)
        napcat_payload["plugin_directory_snapshots"] = _napcat_plugin_snapshots(settings, share_safe)
        napcat_payload.update(_sanitize_capture_value(preflight_evidence or {}, share_safe))
    else:
        napcat_payload["config_snapshots"] = []
        napcat_payload["plugin_directory_snapshots"] = []
        napcat_payload["preflight_deferred"] = True
        napcat_payload["qq_media_roots_deferred"] = True
        napcat_payload["endpoint_probes"] = []
        napcat_payload["route_probes"] = []
    return {
        "captured_at": datetime.now().astimezone().isoformat(),
        "capture_mode": capture_mode,
        "capture_profile": capture_profile,
        "process": {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "cwd": _mask_path_for_sharing(str(Path.cwd()), share_safe),
            "argv": _argv_payload(share_safe),
        },
        "system": {
            "platform_system": platform.system(),
            "platform_release": platform.release(),
            "platform_version": platform.version(),
            "platform_machine": platform.machine(),
            "platform_processor": platform.processor(),
            "hostname": None if share_safe else socket.gethostname(),
            "username": None if share_safe else (os.getenv("USERNAME") or os.getenv("USER") or ""),
            "preferred_encoding": locale.getpreferredencoding(False) or "",
            "filesystem_encoding": sys.getfilesystemencoding() or "",
        },
        "hardware": {
            "cpu_count_logical": os.cpu_count() or 0,
            "python_architecture_bits": struct.calcsize("P") * 8,
            "memory": _memory_snapshot(),
        },
        "python": {
            "executable": _mask_path_for_sharing(sys.executable, share_safe, basename_only=True),
            "version": sys.version,
            "version_info": {
                "major": sys.version_info.major,
                "minor": sys.version_info.minor,
                "micro": sys.version_info.micro,
                "releaselevel": sys.version_info.releaselevel,
                "serial": sys.version_info.serial,
            },
            "implementation": platform.python_implementation(),
            "prefix": _mask_path_for_sharing(sys.prefix, share_safe),
            "base_prefix": _mask_path_for_sharing(getattr(sys, "base_prefix", sys.prefix), share_safe),
            "in_virtual_env": getattr(sys, "base_prefix", sys.prefix) != sys.prefix,
            "packages": _package_versions(),
            "sys_path": [
                _mask_path_for_sharing(str(item), share_safe, basename_only=True)
                for item in sys.path[:_MAX_SYS_PATH_ENTRIES]
            ],
        },
        "selected_env": env_payload,
        "cli": {
            "log_path": _log_path_summary(get_cli_log_path(), share_safe),
            "startup_capture_dir": _mask_path_for_sharing(str(settings.state_dir / "startup_capture"), share_safe),
        },
        "storage": _storage_snapshot(settings, roots, share_safe),
        "napcat": napcat_payload,
        "logs": _log_snapshot(settings, share_safe, include_tails=full_capture),
        "terminal": {
            "probe": asdict(probe_payload) if probe_payload is not None else None,
            "ui_decision": asdict(decision_payload) if decision_payload is not None else None,
        },
        "qq_media_roots": qq_media_roots_payload,
    }


def _qq_media_roots_payload(
    roots: list[Path],
    share_safe: bool,
    *,
    deferred: bool = False,
) -> dict[str, Any]:
    if deferred:
        return {"deferred": True}
    limit = _SHARE_MAX_QQ_ROOT_SNAPSHOTS if share_safe else _MAX_QQ_ROOT_SNAPSHOTS
    payload: dict[str, Any] = {
        "count": len(roots),
        "roots": [_share_path_summary(root, share_safe) for root in roots[:limit]],
    }
    if not share_safe:
        payload["snapshots"] = [_snapshot_qq_root(root) for root in roots[:_MAX_QQ_ROOT_SNAPSHOTS]]
    return payload


def _memory_snapshot() -> dict[str, int] | None:
    if platform.system() != "Windows":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return {
            "memory_load_percent": int(status.dwMemoryLoad),
            "total_physical_bytes": int(status.ullTotalPhys),
            "available_physical_bytes": int(status.ullAvailPhys),
            "total_pagefile_bytes": int(status.ullTotalPageFile),
            "available_pagefile_bytes": int(status.ullAvailPageFile),
            "total_virtual_bytes": int(status.ullTotalVirtual),
            "available_virtual_bytes": int(status.ullAvailVirtual),
        }
    except Exception:
        return None


def _package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package_name in _PACKAGE_VERSION_KEYS:
        try:
            versions[package_name] = importlib_metadata.version(package_name)
        except importlib_metadata.PackageNotFoundError:
            versions[package_name] = "missing"
        except Exception as exc:
            versions[package_name] = f"error:{exc}"
    return versions


def _snapshot_qq_root(root: Path) -> dict[str, Any]:
    return {
        "root": str(root),
        "exists": root.exists(),
        "entries": _directory_entries(root, limit=_MAX_DIR_ENTRIES),
        "interesting_paths": _interesting_qq_paths(root),
    }


def _interesting_qq_paths(root: Path) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    seen: set[Path] = set()
    candidate_bases = [root]
    try:
        for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
            if not child.is_dir():
                continue
            child_name = child.name.casefold()
            if child.name.isdigit() or child_name in {"nt_qq", "qq", "qqnt"}:
                candidate_bases.append(child)
    except OSError:
        return snapshots

    suffixes = [
        (),
        ("nt_qq",),
        ("nt_qq", "nt_data"),
        ("nt_qq", "nt_data", "Pic"),
        ("nt_qq", "nt_data", "Video"),
        ("nt_qq", "nt_data", "FileRecv"),
        ("nt_qq", "nt_data", "Emoji"),
        ("nt_qq", "nt_data", "Record"),
        ("nt_data",),
        ("nt_data", "Pic"),
        ("nt_data", "Video"),
        ("nt_data", "FileRecv"),
        ("Pic",),
        ("Video",),
        ("FileRecv",),
        ("Emoji",),
    ]

    for base in candidate_bases:
        for suffix in suffixes:
            candidate = base.joinpath(*suffix)
            try:
                resolved = candidate.resolve()
            except OSError:
                resolved = candidate
            if resolved in seen or not candidate.exists() or not candidate.is_dir():
                continue
            seen.add(resolved)
            snapshots.append(
                {
                    "path": str(candidate),
                    "entries": _directory_entries(candidate, limit=_MAX_NESTED_ENTRIES),
                }
            )
            if len(snapshots) >= _MAX_QQ_ROOT_SNAPSHOTS:
                return snapshots
    return snapshots


def _directory_entries(path: Path, *, limit: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(path.iterdir(), key=lambda child: (not child.is_dir(), child.name.lower()))
    except OSError as exc:
        return [{"name": "<error>", "detail": str(exc)}]

    for child in children[:limit]:
        entry: dict[str, Any] = {
            "name": child.name,
            "is_dir": child.is_dir(),
            "is_file": child.is_file(),
        }
        try:
            stat = child.stat()
        except OSError as exc:
            entry["detail"] = str(exc)
            entries.append(entry)
            continue
        if child.is_file():
            entry["size_bytes"] = int(stat.st_size)
        entry["modified_at"] = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
        entries.append(entry)
    return entries


def _directory_entry_count(path: Path, *, limit: int) -> int:
    count = 0
    try:
        for _ in path.iterdir():
            count += 1
            if count >= limit:
                break
    except OSError:
        pass
    return count


def _storage_snapshot(settings: NapCatSettings, roots: list[Path], share_safe: bool) -> list[dict[str, Any]]:
    candidates = [
        settings.project_root,
        settings.export_dir,
        settings.state_dir,
        *(roots[:_MAX_QQ_ROOT_SNAPSHOTS]),
    ]
    snapshots: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        anchor = _storage_anchor(resolved)
        if anchor in seen:
            continue
        seen.add(anchor)
        usage = _disk_usage(resolved)
        entry: dict[str, Any] = {
            "anchor": anchor,
            **usage,
        }
        if not share_safe:
            entry["sample_path"] = str(resolved)
        snapshots.append(entry)
    return snapshots


def _storage_anchor(path: Path) -> str:
    drive = path.drive
    if drive:
        return drive.upper()
    return str(path.anchor or path)


def _disk_usage(path: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        return {
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
        }
    except OSError as exc:
        return {"detail": str(exc)}


def _napcat_config_snapshots(settings: NapCatSettings, share_safe: bool) -> list[dict[str, Any]]:
    candidates = [
        settings.onebot_config_path,
        settings.webui_config_path,
        _workdir_config_path(settings, "plugins.json"),
        _workdir_config_path(settings, "napcat.json"),
    ]
    snapshots: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        snapshots.append(
            _config_file_snapshot(
                resolved,
                include_content=not share_safe,
                share_safe=share_safe,
            )
        )
    return snapshots


def _napcat_plugin_snapshots(settings: NapCatSettings, share_safe: bool) -> list[dict[str, Any]]:
    paths: list[Path] = []
    if settings.workdir is not None:
        paths.append(settings.workdir / "plugins")
        paths.append(settings.workdir / "config" / "plugins")
    snapshots: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_dir():
            continue
        entry: dict[str, Any] = {"path": _share_path_summary(path, share_safe)}
        if share_safe:
            entry["entry_count"] = _directory_entry_count(path, limit=_MAX_DIR_ENTRIES)
        else:
            entry["entries"] = _directory_entries(path, limit=_MAX_DIR_ENTRIES)
        snapshots.append(entry)
    return snapshots


def _workdir_config_path(settings: NapCatSettings, filename: str) -> Path | None:
    if settings.workdir is None:
        return None
    return settings.workdir / "config" / filename


def _config_file_snapshot(
    path: Path,
    *,
    include_content: bool = True,
    share_safe: bool = False,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "path": _mask_path_for_sharing(
            str(path),
            share_safe,
            basename_only=share_safe,
        )
        or "<redacted>",
        "exists": path.exists(),
    }
    if not path.exists():
        return snapshot
    try:
        stat = path.stat()
        snapshot["size_bytes"] = int(stat.st_size)
        snapshot["modified_at"] = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
        if include_content:
            payload = orjson.loads(path.read_bytes())
            snapshot["content"] = _redact_json(payload)
        return snapshot
    except Exception as exc:
        snapshot["detail"] = str(exc)
        return snapshot


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if "token" in lowered or "password" in lowered or "secret" in lowered:
                redacted[key] = "<redacted>"
            else:
                redacted[key] = _redact_json(item)
        return redacted
    if isinstance(value, list):
        return [_redact_json(item) for item in value[:64]]
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING:
            return value[:_MAX_JSON_STRING] + "...<trimmed>"
        return value
    return value


def _trim_text(value: str, *, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<trimmed>"


def _log_snapshot(settings: NapCatSettings, share_safe: bool, *, include_tails: bool) -> dict[str, Any]:
    cli_latest = get_latest_cli_log_path(settings.state_dir)
    napcat_latest = _get_latest_napcat_log_path(settings.state_dir)
    max_lines = _log_tail_lines_for_mode(share_safe) if include_tails else 0
    return {
        "cli_latest_log_path": _log_path_summary(cli_latest, share_safe),
        "cli_latest_log_tail": _sanitize_log_lines(
            _tail_text(cli_latest, max_lines=max_lines) if include_tails else [],
            share_safe,
        ),
        "napcat_latest_log_path": _log_path_summary(napcat_latest, share_safe),
        "napcat_latest_log_tail": _sanitize_log_lines(
            _tail_text(napcat_latest, max_lines=max_lines)
            if include_tails and napcat_latest is not None
            else [],
            share_safe,
        ),
    }


def _get_latest_napcat_log_path(state_dir: Path) -> Path | None:
    latest_path = state_dir / "napcat_logs" / "latest.path"
    try:
        if latest_path.exists():
            return _read_latest_pointer(
                latest_path,
                allowed_dir=state_dir / "napcat_logs",
                allowed_suffix=".log",
            )
    except OSError:
        return None
    return None


def _tail_text(path: Path | None, *, max_lines: int = _MAX_LOG_TAIL_LINES) -> list[str]:
    if path is None or not path.exists():
        return []
    try:
        return _tail_text_efficient(path, max_lines=max_lines)
    except OSError as exc:
        return [f"<error reading log: {exc}>"]


def _tail_text_efficient(path: Path, *, max_lines: int) -> list[str]:
    chunk_size = 8192
    collected = deque()
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        buffer = b""
        while position > 0 and len(collected) <= max_lines:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            buffer = handle.read(read_size) + buffer
            lines = buffer.splitlines()
            if position > 0 and lines:
                buffer = lines[0]
                lines = lines[1:]
            else:
                buffer = b""
            for line in reversed(lines):
                collected.appendleft(line.decode("utf-8", errors="replace"))
                if len(collected) > max_lines:
                    collected.popleft()
        if buffer:
            collected.appendleft(buffer.decode("utf-8", errors="replace"))
            while len(collected) > max_lines:
                collected.popleft()
    return list(collected)


def _read_latest_pointer(
    latest_path: Path,
    *,
    allowed_dir: Path,
    allowed_suffix: str,
) -> Path | None:
    raw_value = latest_path.read_text(encoding="utf-8").strip()
    if not raw_value:
        return None
    candidate = Path(raw_value)
    if not candidate.is_absolute():
        candidate = (latest_path.parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        allowed_resolved = allowed_dir.resolve()
    except OSError:
        allowed_resolved = allowed_dir
    if candidate.suffix.casefold() != allowed_suffix.casefold():
        return None
    if candidate.parent != allowed_resolved:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    return candidate
