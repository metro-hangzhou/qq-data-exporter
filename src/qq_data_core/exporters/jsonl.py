from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import orjson

from ..models import NormalizedSnapshot
from ..paths import build_timestamp_token


def write_jsonl(snapshot: NormalizedSnapshot, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(
        f".{output_path.stem}.{build_timestamp_token(include_pid=True)}{output_path.suffix}.tmp"
    )
    try:
        with temp_path.open("wb") as handle:
            for message in snapshot.messages:
                handle.write(orjson.dumps(message.model_dump(exclude_none=True)))
                handle.write(b"\n")
        temp_path.replace(output_path)
        return output_path
    finally:
        with suppress(OSError):
            temp_path.unlink()
