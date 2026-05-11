from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import BaseModel, Field

from .fast_history_client import FAST_HISTORY_PLUGIN_ID, derive_fast_history_url


class NapCatSettings(BaseModel):
    project_root: Path = Field(default_factory=Path.cwd)
    napcat_dir: Path | None = None
    napcat_launcher_path: Path | None = None
    auto_start_napcat: bool = True
    auto_configure_onebot: bool = True
    quick_login_uin: str | None = None
    http_url: str = Field(default="http://127.0.0.1:3000")
    ws_url: str = Field(default="ws://127.0.0.1:3001")
    access_token: str | None = None
    webui_url: str = Field(default="http://127.0.0.1:6099/api")
    webui_token: str | None = None
    use_system_proxy: bool = False
    fast_history_mode: Literal["auto", "off", "force"] = Field(default="auto")
    fast_history_url: str | None = None
    fast_history_plugin_id: str = Field(default=FAST_HISTORY_PLUGIN_ID)
    export_dir: Path = Field(default=Path("exports"))
    state_dir: Path = Field(default=Path("state"))
    workdir: Path | None = None
    onebot_config_path: Path | None = None
    webui_config_path: Path | None = None

    @classmethod
    def from_env(cls) -> "NapCatSettings":
        project_root = _resolve_project_root()
        napcat_dir = _resolve_napcat_dir(project_root)
        launcher_path = _resolve_launcher_path(project_root, napcat_dir)
        workdir = _resolve_workdir(project_root, napcat_dir)
        state_dir = _resolve_relative_path(
            os.getenv("STATE_DIR", "state"),
            project_root=project_root,
            base_dir=project_root,
        )
        try:
            onebot_config_path = _resolve_config_path(
                os.getenv("NAPCAT_ONEBOT_CONFIG"),
                candidates=_candidate_config_paths(
                    "onebot11.json",
                    workdir=workdir,
                    project_root=project_root,
                    napcat_dir=napcat_dir,
                    extra_glob_patterns=["onebot11_*.json"],
                    extra_relative_paths=[
                        Path("config") / "onebot11.json",
                        Path("napcat") / "config" / "onebot11.json",
                        Path("NapCatQQ") / "config" / "onebot11.json",
                        Path("NapCat") / "napcat" / "config" / "onebot11.json",
                        Path("NapCatRuntime") / "napcat" / "config" / "onebot11.json",
                        Path("NapCatQQ") / "packages" / "napcat-develop" / "config" / "onebot11.json",
                    ],
                ),
                project_root=project_root,
                base_dir=workdir or napcat_dir or project_root,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"explicit NAPCAT_ONEBOT_CONFIG path {os.getenv('NAPCAT_ONEBOT_CONFIG')} does not exist"
            ) from exc
        try:
            webui_config_path = _resolve_config_path(
                os.getenv("NAPCAT_WEBUI_CONFIG"),
                candidates=_candidate_config_paths(
                    "webui.json",
                    workdir=workdir,
                    project_root=project_root,
                    napcat_dir=napcat_dir,
                    extra_relative_paths=[
                        Path("config") / "webui.json",
                        Path("napcat") / "config" / "webui.json",
                        Path("NapCatQQ") / "config" / "webui.json",
                        Path("NapCat") / "napcat" / "config" / "webui.json",
                        Path("NapCatRuntime") / "napcat" / "config" / "webui.json",
                        Path("NapCatQQ") / "packages" / "napcat-webui-backend" / "webui.json",
                    ],
                ),
                project_root=project_root,
                base_dir=workdir or napcat_dir or project_root,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"explicit NAPCAT_WEBUI_CONFIG path {os.getenv('NAPCAT_WEBUI_CONFIG')} does not exist"
            ) from exc
        onebot_config = _read_json_file(onebot_config_path)
        webui_config = _read_json_file(webui_config_path)

        http_defaults = _extract_http_server_defaults(onebot_config)
        ws_defaults = _extract_ws_server_defaults(onebot_config)
        webui_defaults = _extract_webui_defaults(webui_config)
        access_token_env = os.getenv("NAPCAT_TOKEN")
        webui_token_env = os.getenv("NAPCAT_WEBUI_TOKEN")
        fast_history_plugin_id = os.getenv("NAPCAT_FAST_HISTORY_PLUGIN_ID", FAST_HISTORY_PLUGIN_ID)
        webui_url = os.getenv("NAPCAT_WEBUI_URL", webui_defaults["url"])
        quick_login_uin = _first_non_none(
            _normalize_optional_text(os.getenv("NAPCAT_QUICK_LOGIN_UIN")),
            _read_local_quick_login_uin(project_root=project_root, state_dir=state_dir),
        )
        return cls(
            project_root=project_root,
            napcat_dir=napcat_dir,
            napcat_launcher_path=launcher_path,
            auto_start_napcat=_parse_bool_env(os.getenv("NAPCAT_AUTO_START"), default=True),
            auto_configure_onebot=_parse_bool_env(
                os.getenv("NAPCAT_AUTO_CONFIGURE_ONEBOT"),
                default=True,
            ),
            quick_login_uin=quick_login_uin,
            http_url=os.getenv("NAPCAT_HTTP_URL", http_defaults["url"]),
            ws_url=os.getenv("NAPCAT_WS_URL", ws_defaults["url"]),
            access_token=_first_non_none(access_token_env, http_defaults["token"], ws_defaults["token"]),
            webui_url=webui_url,
            webui_token=_first_non_none(webui_token_env, webui_defaults["token"]),
            use_system_proxy=_parse_bool_env(os.getenv("NAPCAT_USE_SYSTEM_PROXY"), default=False),
            fast_history_mode=_parse_fast_history_mode(os.getenv("NAPCAT_FAST_HISTORY_MODE")),
            fast_history_url=os.getenv(
                "NAPCAT_FAST_HISTORY_URL",
                derive_fast_history_url(webui_url, plugin_id=fast_history_plugin_id),
            ),
            fast_history_plugin_id=fast_history_plugin_id,
            export_dir=_resolve_relative_path(
                os.getenv("EXPORT_DIR", "exports"),
                project_root=project_root,
                base_dir=project_root,
            ),
            state_dir=state_dir,
            workdir=workdir,
            onebot_config_path=onebot_config_path,
            webui_config_path=webui_config_path,
        )


def build_settings_resolution_diagnostics(settings: "NapCatSettings") -> dict[str, Any]:
    project_root = settings.project_root
    napcat_dir_candidates = [
        project_root / "NapCat",
        project_root / "NapCatRuntime",
        project_root / "NapCatQQ",
        project_root / "vendor" / "NapCat",
        project_root / "vendor" / "NapCatQQ",
    ]
    workdir_candidates: list[Path] = []
    if settings.napcat_dir is not None:
        workdir_candidates.extend([settings.napcat_dir, settings.napcat_dir / "napcat"])
    workdir_candidates.extend(
        [
            project_root / "NapCat",
            project_root / "NapCat" / "napcat",
            project_root / "NapCatRuntime",
            project_root / "NapCatRuntime" / "napcat",
        ]
    )
    napcat_dir_candidate_paths = [
        candidate for candidate in _dedupe_paths(napcat_dir_candidates) if candidate is not None
    ]
    workdir_candidate_paths = [
        candidate for candidate in _dedupe_paths(workdir_candidates) if candidate is not None
    ]
    onebot_candidates = _candidate_config_paths(
        "onebot11.json",
        workdir=settings.workdir,
        project_root=project_root,
        napcat_dir=settings.napcat_dir,
        extra_glob_patterns=["onebot11_*.json"],
        extra_relative_paths=[
            Path("config") / "onebot11.json",
            Path("napcat") / "config" / "onebot11.json",
            Path("NapCatQQ") / "config" / "onebot11.json",
            Path("NapCat") / "napcat" / "config" / "onebot11.json",
            Path("NapCatRuntime") / "napcat" / "config" / "onebot11.json",
            Path("NapCatQQ") / "packages" / "napcat-develop" / "config" / "onebot11.json",
        ],
    )
    webui_candidates = _candidate_config_paths(
        "webui.json",
        workdir=settings.workdir,
        project_root=project_root,
        napcat_dir=settings.napcat_dir,
        extra_relative_paths=[
            Path("config") / "webui.json",
            Path("napcat") / "config" / "webui.json",
            Path("NapCatQQ") / "config" / "webui.json",
            Path("NapCat") / "napcat" / "config" / "webui.json",
            Path("NapCatRuntime") / "napcat" / "config" / "webui.json",
            Path("NapCatQQ") / "packages" / "napcat-webui-backend" / "webui.json",
        ],
    )
    return {
        "selected": {
            "project_root": str(project_root),
            "napcat_dir": str(settings.napcat_dir) if settings.napcat_dir is not None else None,
            "workdir": str(settings.workdir) if settings.workdir is not None else None,
            "launcher_path": str(settings.napcat_launcher_path) if settings.napcat_launcher_path is not None else None,
            "onebot_config_path": str(settings.onebot_config_path) if settings.onebot_config_path is not None else None,
            "webui_config_path": str(settings.webui_config_path) if settings.webui_config_path is not None else None,
        },
        "napcat_dir_candidates": [
            {
                "path": str(candidate),
                "exists": candidate.exists(),
                "score": _runtime_candidate_score(candidate),
            }
            for candidate in napcat_dir_candidate_paths
        ],
        "workdir_candidates": [
            {
                "path": str(candidate),
                "exists": candidate.exists(),
                "looks_like_runtime_workdir": _looks_like_runtime_workdir(candidate),
                "looks_like_runtime_container": _looks_like_runtime_container(candidate),
                "looks_like_runtime_launcher_root": _looks_like_runtime_launcher_root(candidate),
                "score": _workdir_candidate_score(candidate),
            }
            for candidate in workdir_candidate_paths
        ],
        "onebot_config_candidates": [
            {
                "path": str(candidate),
                "exists": candidate.exists(),
            }
            for candidate in onebot_candidates
            if candidate is not None
        ],
        "webui_config_candidates": [
            {
                "path": str(candidate),
                "exists": candidate.exists(),
            }
            for candidate in webui_candidates
            if candidate is not None
        ],
        "napcat_dir_ambiguous": _candidates_are_ambiguous(
            [
                _runtime_candidate_score(candidate)
                for candidate in napcat_dir_candidate_paths
                if _runtime_candidate_score(candidate) > 0
            ]
        ),
        "workdir_ambiguous": _candidates_are_ambiguous(
            [
                _workdir_candidate_score(candidate)
                for candidate in workdir_candidate_paths
                if _workdir_candidate_score(candidate) > 0
            ]
        ),
    }


def _resolve_project_root() -> Path:
    env_value = os.getenv("PROJECT_ROOT")
    if env_value:
        return Path(env_value).resolve()
    for root in _search_roots():
        if (root / "pyproject.toml").exists() or (root / "AGENTS.md").exists():
            return root
    return Path(__file__).resolve().parents[3]


def _normalize_optional_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _read_local_quick_login_uin(*, project_root: Path, state_dir: Path) -> str | None:
    explicit = _normalize_optional_text(os.getenv("NAPCAT_QUICK_LOGIN_UIN_FILE"))
    candidates: list[Path] = []
    if explicit:
        candidates.append(
            _resolve_relative_path(explicit, project_root=project_root, base_dir=project_root)
        )
    candidates.extend(
        [
            state_dir / "config" / "napcat_quick_login_uin.txt",
            project_root / "state" / "config" / "napcat_quick_login_uin.txt",
        ]
    )
    for candidate in _dedupe_paths(candidates):
        if candidate is None or not candidate.exists() or not candidate.is_file():
            continue
        try:
            return _normalize_optional_text(candidate.read_text(encoding="utf-8"))
        except OSError:
            continue
    return None


def _resolve_napcat_dir(project_root: Path) -> Path | None:
    explicit = os.getenv("NAPCAT_DIR")
    if explicit:
        resolved = _resolve_relative_path(explicit, project_root=project_root)
        return resolved if resolved.exists() else resolved
    candidates = [
        project_root / "NapCat",
        project_root / "NapCatRuntime",
        project_root / "NapCatQQ",
        project_root / "vendor" / "NapCat",
        project_root / "vendor" / "NapCatQQ",
    ]
    ranked_candidates: list[tuple[int, Path]] = []
    for candidate in candidates:
        score = _runtime_candidate_score(candidate)
        if score > 0:
            ranked_candidates.append((score, candidate.resolve()))
    if ranked_candidates:
        ranked_candidates.sort(key=lambda item: (-item[0], len(str(item[1]))))
        return _select_unique_best_candidate(ranked_candidates)
    return None


def _resolve_launcher_path(project_root: Path, napcat_dir: Path | None) -> Path | None:
    explicit = os.getenv("NAPCAT_LAUNCHER")
    if explicit:
        base_dir = napcat_dir if napcat_dir is not None else project_root
        return _resolve_relative_path(explicit, project_root=project_root, base_dir=base_dir)
    if napcat_dir is None:
        return None
    for relative_path in [
        Path("napcat") / "launcher-win10.bat",
        Path("napcat") / "launcher.bat",
        Path("napcat") / "launcher-win10-user.bat",
        Path("napcat") / "launcher-user.bat",
        Path("napcat.bat"),
        Path("napcat.quick.bat"),
        Path("launcher-win10.bat"),
        Path("launcher.bat"),
        Path("launcher-win10-user.bat"),
        Path("launcher-user.bat"),
        Path("packages") / "napcat-shell-loader" / "launcher-win10.bat",
        Path("packages") / "napcat-shell-loader" / "launcher.bat",
        Path("packages") / "napcat-shell-loader" / "launcher-win10-user.bat",
        Path("packages") / "napcat-shell-loader" / "launcher-user.bat",
    ]:
        candidate = napcat_dir / relative_path
        if candidate.exists():
            return candidate.resolve()
    return None


def _resolve_workdir(project_root: Path, napcat_dir: Path | None) -> Path | None:
    env_value = os.getenv("NAPCAT_WORKDIR")
    if env_value:
        return _resolve_relative_path(env_value, project_root=project_root, base_dir=project_root)
    workdir_candidates: list[Path] = []
    if napcat_dir is not None:
        workdir_candidates.extend([napcat_dir, napcat_dir / "napcat"])
    workdir_candidates.extend(
        [
            project_root / "NapCat",
            project_root / "NapCat" / "napcat",
            project_root / "NapCatRuntime",
            project_root / "NapCatRuntime" / "napcat",
        ]
    )
    ranked_candidates: list[tuple[int, Path]] = []
    for candidate in _dedupe_paths(workdir_candidates):
        if candidate is None:
            continue
        score = _workdir_candidate_score(candidate)
        if score > 0:
            ranked_candidates.append((score, candidate.resolve()))
    if ranked_candidates:
        ranked_candidates.sort(key=lambda item: (-item[0], len(str(item[1]))))
        return _select_unique_best_candidate(ranked_candidates)
    if napcat_dir is not None:
        if _looks_like_runtime_workdir(napcat_dir):
            return napcat_dir
        nested_workdir = napcat_dir / "napcat"
        if _looks_like_runtime_workdir(nested_workdir):
            return nested_workdir.resolve()
    return None


def _resolve_config_path(
    explicit: str | None,
    *,
    project_root: Path,
    base_dir: Path,
    candidates: list[Path | None],
) -> Path | None:
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_absolute():
            resolved = explicit_path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"explicit config path {resolved} does not exist")
            return resolved
        resolution_candidates = [
            (base_dir / explicit_path).resolve(),
            (project_root / explicit_path).resolve(),
        ]
        for candidate in resolution_candidates:
            if candidate.exists():
                return candidate
        raise FileNotFoundError(f"explicit config path {explicit_path} could not be resolved to an existing file")
    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return candidate.resolve()
    return None


def _select_unique_best_candidate(ranked_candidates: list[tuple[int, Path]]) -> Path | None:
    if not ranked_candidates:
        return None
    best_score = ranked_candidates[0][0]
    best = [candidate for score, candidate in ranked_candidates if score == best_score]
    if len(best) == 1:
        return best[0]
    return None


def _candidates_are_ambiguous(scores: list[int]) -> bool:
    if not scores:
        return False
    best = max(scores)
    return sum(1 for score in scores if score == best) > 1


def _candidate_config_paths(
    filename: str,
    *,
    workdir: Path | None,
    project_root: Path,
    napcat_dir: Path | None,
    extra_glob_patterns: list[str] | None = None,
    extra_relative_paths: list[Path],
) -> list[Path | None]:
    candidates: list[Path | None] = []
    if workdir is not None:
        candidates.extend(_glob_config_candidates(workdir / "config", extra_glob_patterns or []))
        candidates.append(workdir / "config" / filename)
    if napcat_dir is not None:
        candidates.extend(_glob_config_candidates(napcat_dir / "config", extra_glob_patterns or []))
        candidates.extend(_glob_config_candidates(napcat_dir / "napcat" / "config", extra_glob_patterns or []))
        candidates.append(napcat_dir / "config" / filename)
        candidates.append(napcat_dir / "napcat" / "config" / filename)
    for root in [project_root]:
        for relative_path in extra_relative_paths:
            candidates.append(root / relative_path)
    return _dedupe_paths(candidates)


def _search_roots(project_root: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()
    seeds = [Path.cwd(), project_root or Path(__file__).resolve().parents[3], Path(__file__).resolve().parents[3]]
    for seed in seeds:
        for root in [seed, *seed.parents]:
            resolved = root.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(resolved)
    return candidates


def _looks_like_runtime_workdir(path: Path) -> bool:
    config_dir = path / "config"
    if not config_dir.exists() or not config_dir.is_dir():
        return False
    if (config_dir / "webui.json").exists():
        return True
    if (config_dir / "plugins.json").exists() or (config_dir / "napcat.json").exists():
        return True
    return any(config_dir.glob("onebot11*.json")) or any(config_dir.glob("napcat_protocol*.json"))


def _looks_like_runtime_launcher_root(path: Path) -> bool:
    return any(
        (path / relative_path).exists()
        for relative_path in [
            Path("napcat") / "launcher-win10.bat",
            Path("napcat") / "launcher.bat",
            Path("napcat") / "launcher-win10-user.bat",
            Path("napcat") / "launcher-user.bat",
            Path("napcat.bat"),
            Path("napcat.quick.bat"),
            Path("NapCatWinBootMain.exe"),
            Path("launcher-win10.bat"),
            Path("launcher.bat"),
            Path("launcher-win10-user.bat"),
            Path("launcher-user.bat"),
            Path("packages") / "napcat-shell-loader" / "launcher-win10.bat",
            Path("packages") / "napcat-shell-loader" / "launcher.bat",
            Path("packages") / "napcat-shell-loader" / "launcher-win10-user.bat",
            Path("packages") / "napcat-shell-loader" / "launcher-user.bat",
        ]
    )


def _runtime_candidate_score(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    score = 0
    if _looks_like_runtime_workdir(path):
        score += 6
    if _looks_like_runtime_workdir(path / "napcat"):
        score += 5
    if _looks_like_runtime_launcher_root(path):
        score += 4
    if _looks_like_runtime_launcher_root(path / "napcat"):
        score += 3
    if _looks_like_runtime_container(path):
        score += 4
    if _looks_like_runtime_container(path / "napcat"):
        score += 3
    return score


def _workdir_candidate_score(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    score = 0
    if _looks_like_runtime_workdir(path):
        score += 8
    config_dir = path / "config"
    if (config_dir / "webui.json").exists():
        score += 3
    if (config_dir / "plugins.json").exists():
        score += 2
    if (config_dir / "napcat.json").exists():
        score += 2
    if any(config_dir.glob("onebot11*.json")):
        score += 2
    if any(config_dir.glob("napcat_protocol*.json")):
        score += 2
    if _looks_like_runtime_launcher_root(path):
        score += 2
    if _looks_like_runtime_container(path):
        score += 1
    return score


def _looks_like_runtime_container(path: Path) -> bool:
    return any(
        candidate.exists()
        for candidate in [
            path / "node.exe",
            path / "wrapper.node",
            path / "index.js",
            path / "NapCatWinBootMain.exe",
            path / "NapCatWinBootHook.dll",
            path / "napcat" / "napcat.mjs",
            path / "napcat" / "config",
            path / "config" / "plugins.json",
            path / "config" / "napcat.json",
        ]
    )


def _resolve_relative_path(
    value: str,
    *,
    project_root: Path,
    base_dir: Path | None = None,
) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path.resolve()
    if base_dir is not None:
        return (base_dir / path).resolve()
    return (project_root / path).resolve()


def _dedupe_paths(paths: list[Path | None]) -> list[Path | None]:
    result: list[Path | None] = []
    seen: set[Path] = set()
    for path in paths:
        if path is None:
            result.append(None)
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def _glob_config_candidates(directory: Path, patterns: list[str]) -> list[Path]:
    if not directory.exists():
        return []
    matches: list[Path] = []
    for pattern in patterns:
        matches.extend(sorted(directory.glob(pattern), reverse=True))
    return matches


def _read_json_file(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        payload = orjson.loads(path.read_bytes())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_http_server_defaults(config: dict[str, Any] | None) -> dict[str, str]:
    item = _select_network_item(config, "httpServers")
    host = _normalize_host(_string(item.get("host") if item else None), fallback="127.0.0.1")
    port = _string(item.get("port") if item else None) or "3000"
    return {
        "url": f"http://{host}:{port}",
        "token": _string(item.get("token") if item else None),
    }


def _extract_ws_server_defaults(config: dict[str, Any] | None) -> dict[str, str]:
    item = _select_network_item(config, "websocketServers")
    host = _normalize_host(_string(item.get("host") if item else None), fallback="127.0.0.1")
    port = _string(item.get("port") if item else None) or "3001"
    return {
        "url": f"ws://{host}:{port}",
        "token": _string(item.get("token") if item else None),
    }


def _extract_webui_defaults(config: dict[str, Any] | None) -> dict[str, str]:
    host = _normalize_host(_string(config.get("host") if config else None), fallback="127.0.0.1")
    port = _string(config.get("port") if config else None) or "6099"
    return {
        "url": f"http://{host}:{port}/api",
        "token": _string(config.get("token") if config else None),
    }


def _select_network_item(config: dict[str, Any] | None, key: str) -> dict[str, Any] | None:
    if config is None:
        return None
    network = config.get("network")
    if not isinstance(network, dict):
        return None
    items = network.get(key)
    if not isinstance(items, list):
        return None
    enabled_items = [item for item in items if isinstance(item, dict) and item.get("enable") is True]
    if enabled_items:
        return enabled_items[0]
    return next((item for item in items if isinstance(item, dict)), None)


def _normalize_host(value: str, *, fallback: str) -> str:
    if value in {"", "0.0.0.0", "::", "[::]"}:
        return fallback
    return value


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_non_none(*values: str | None) -> str | None:
    for value in values:
        if value is not None:
            return value
    return None


def _parse_bool_env(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_fast_history_mode(value: str | None) -> Literal["auto", "off", "force"]:
    normalized = str(value or "auto").strip().lower()
    if normalized in {"auto", "off", "force"}:
        return normalized
    return "auto"
