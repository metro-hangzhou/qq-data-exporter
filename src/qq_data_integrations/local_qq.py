from __future__ import annotations

import ctypes
import os
import time
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Callable

_DISCOVERY_CACHE_LOCK = Lock()
_DISCOVERY_CACHE_TTL_S = 30.0
_DISCOVERY_CACHE: tuple[float, list[Path]] | None = None


def discover_qq_media_roots() -> list[Path]:
    cached = _get_cached_roots()
    if cached is not None:
        return cached

    env_value = os.getenv("QQ_MEDIA_ROOTS")
    if env_value:
        roots = [Path(part).expanduser().resolve() for part in env_value.split(";") if part.strip()]
        discovered = [root for root in roots if root.exists()]
        _store_cached_roots(discovered)
        return discovered

    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved.exists() and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    for drive_root in _candidate_drive_roots():
        add(drive_root / "QQ")
        add(drive_root / "Tencent Files")
        add(drive_root / "QQ Files")
        _add_nested_targets(drive_root, add)

    user_profile = os.getenv("USERPROFILE")
    local_app_data = os.getenv("LOCALAPPDATA")
    app_data = os.getenv("APPDATA")

    if user_profile:
        home = Path(user_profile)
        add(home / "Documents" / "Tencent Files")
        add(home / "Documents" / "QQ Files")
    if local_app_data:
        local = Path(local_app_data)
        add(local / "Tencent" / "QQ")
        add(local / "Tencent" / "QQNT")
    if app_data:
        roaming = Path(app_data)
        add(roaming / "Tencent" / "QQ")
        add(roaming / "Tencent" / "QQNT")

    _store_cached_roots(candidates)
    return candidates


def _add_nested_targets(root: Path, add: Callable[[Path], None]) -> None:
    with suppress(OSError):
        for child in root.iterdir():
            if not child.is_dir():
                continue
            add(child / "QQ")
            add(child / "Tencent Files")
            add(child / "QQ Files")


def _candidate_drive_roots() -> list[Path]:
    if os.name != "nt":
        return [Path("/")]
    try:
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:
        mask = 0
    roots = [Path(f"{chr(code)}:/") for code in range(ord("A"), ord("Z") + 1) if mask & (1 << (code - ord("A")))]
    if roots:
        return roots
    return [Path(f"{drive}:/") for drive in ["C", "D", "E", "F", "G"]]


def _get_cached_roots() -> list[Path] | None:
    global _DISCOVERY_CACHE
    with _DISCOVERY_CACHE_LOCK:
        if _DISCOVERY_CACHE is None:
            return None
        cached_at, roots = _DISCOVERY_CACHE
        if time.monotonic() - cached_at > _DISCOVERY_CACHE_TTL_S:
            _DISCOVERY_CACHE = None
            return None
        return list(roots)


def _store_cached_roots(roots: list[Path]) -> None:
    global _DISCOVERY_CACHE
    with _DISCOVERY_CACHE_LOCK:
        _DISCOVERY_CACHE = (time.monotonic(), list(roots))
