from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from types import TracebackType

from qq_data_core.paths import atomic_write_text, build_timestamp_token


_LOGGER_ROOT = "qq_data_cli"
_SESSION_LOG_PATH: Path | None = None
_SESSION_LOG_STATE_DIR: Path | None = None
_INITIALIZED = False


def setup_cli_logging(state_dir: Path) -> Path:
    global _SESSION_LOG_PATH, _SESSION_LOG_STATE_DIR, _INITIALIZED
    resolved_state_dir = state_dir.resolve()
    if (
        _INITIALIZED
        and _SESSION_LOG_PATH is not None
        and _SESSION_LOG_STATE_DIR == resolved_state_dir
        and _SESSION_LOG_PATH.exists()
    ):
        return _SESSION_LOG_PATH

    logs_dir = resolved_state_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = build_timestamp_token(include_pid=True)
    session_path = logs_dir / f"cli_{stamp}.log"
    latest_pointer_path = logs_dir / "latest.path"

    logger = logging.getLogger(_LOGGER_ROOT)
    _clear_handlers(logger)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = logging.FileHandler(session_path, mode="a", encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    atomic_write_text(latest_pointer_path, str(session_path), encoding="utf-8")

    _SESSION_LOG_PATH = session_path
    _SESSION_LOG_STATE_DIR = resolved_state_dir
    _INITIALIZED = True

    _install_exception_hooks()
    logger.info("cli_logging_ready session_log=%s latest_log_path=%s", session_path, latest_pointer_path)
    return session_path


def get_cli_logger(name: str | None = None) -> logging.Logger:
    if name:
        return logging.getLogger(f"{_LOGGER_ROOT}.{name}")
    return logging.getLogger(_LOGGER_ROOT)


def get_cli_log_path() -> Path | None:
    return _SESSION_LOG_PATH


def get_latest_cli_log_path(state_dir: Path) -> Path | None:
    resolved_state_dir = state_dir.resolve()
    session_path = _SESSION_LOG_PATH
    if (
        session_path is not None
        and session_path.exists()
        and _SESSION_LOG_STATE_DIR == resolved_state_dir
    ):
        return session_path
    latest_pointer_path = resolved_state_dir / "logs" / "latest.path"
    if not latest_pointer_path.exists():
        return None
    try:
        raw_value = latest_pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw_value:
        return None
    candidate = Path(raw_value)
    if not candidate.is_absolute():
        candidate = (latest_pointer_path.parent / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        allowed_dir = (resolved_state_dir / "logs").resolve()
    except OSError:
        allowed_dir = resolved_state_dir / "logs"
    if candidate.suffix.casefold() != ".log":
        return None
    if allowed_dir not in candidate.parents:
        return None
    return candidate if candidate.exists() else None


def _install_exception_hooks() -> None:
    def _sys_excepthook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        get_cli_logger("uncaught").exception(
            "uncaught_exception",
            exc_info=(exc_type, exc_value, exc_traceback),
        )
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        get_cli_logger("thread").exception(
            "thread_exception thread=%s",
            getattr(args.thread, "name", "<unknown>"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        threading.__excepthook__(args)

    sys.excepthook = _sys_excepthook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_excepthook


def reset_cli_logging_for_tests() -> None:
    global _SESSION_LOG_PATH, _SESSION_LOG_STATE_DIR, _INITIALIZED
    logger = logging.getLogger(_LOGGER_ROOT)
    _clear_handlers(logger)
    _SESSION_LOG_PATH = None
    _SESSION_LOG_STATE_DIR = None
    _INITIALIZED = False


def _clear_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
