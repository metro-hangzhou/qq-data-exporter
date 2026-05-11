from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

EXPORT_TIMEZONE = timezone(timedelta(hours=8))


def stable_digest(*parts: object, length: int = 16) -> str:
    material = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:length]


def make_message_uid(
    *,
    source_type: str,
    chat_type: str,
    chat_id: str,
    message_id: str | None,
    message_seq: str | None,
    timestamp_ms: int,
    sender_id_raw: str,
    ordinal: int,
) -> str:
    digest = stable_digest(
        source_type,
        chat_type,
        chat_id,
        message_id,
        message_seq,
        timestamp_ms,
        sender_id_raw,
        ordinal,
    )
    return f"msg_{digest}"


def make_asset_id(
    message_uid: str, asset_type: str, file_name: str | None, ordinal: int
) -> str:
    digest = stable_digest(message_uid, asset_type, file_name, ordinal)
    return f"asset_{digest}"


def parse_iso_to_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def parse_local_timestamp_to_ms(value: str) -> tuple[int, str]:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=EXPORT_TIMEZONE)
    return int(dt.timestamp() * 1000), dt.isoformat()


def preview_text(content: str, limit: int = 120) -> str:
    if len(content) <= limit:
        return content
    return content[: limit - 3] + "..."
