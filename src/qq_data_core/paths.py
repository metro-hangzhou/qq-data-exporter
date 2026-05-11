from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .models import EXPORT_TIMEZONE


def build_timestamp_token(*, include_pid: bool = False) -> str:
    token = datetime.now(EXPORT_TIMEZONE).strftime("%Y%m%d_%H%M%S_%f")
    if include_pid:
        return f"{token}_{os.getpid()}"
    return token


def build_default_output_path(
    output_dir: Path,
    *,
    chat_type: str,
    chat_id: str,
    fmt: str,
) -> Path:
    timestamp = build_timestamp_token()
    prefix = "group" if chat_type == "group" else "friend"
    return output_dir / f"{prefix}_{chat_id}_{timestamp}.{fmt}"


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _temporary_sibling_path(path)
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
    return path


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, content.encode(encoding))


def _temporary_sibling_path(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
    )
