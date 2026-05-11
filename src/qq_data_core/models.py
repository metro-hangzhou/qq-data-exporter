from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

EXPORT_TIMEZONE = timezone(timedelta(hours=8))


class ReplyRef(BaseModel):
    referenced_message_id: str | None = None
    referenced_sender_id: str | None = None
    referenced_timestamp: str | None = None
    preview_text: str | None = None


class NormalizedSegment(BaseModel):
    type: Literal[
        "text",
        "image",
        "speech",
        "file",
        "emoji",
        "sticker",
        "reply",
        "video",
        "forward",
        "system",
        "share",
        "unsupported",
    ]
    token: str | None = None
    text: str | None = None
    file_name: str | None = None
    path: str | None = None
    md5: str | None = None
    emoji_id: str | None = None
    emoji_package_id: int | None = None
    summary: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class NormalizedMessage(BaseModel):
    chat_type: Literal["group", "private"]
    chat_id: str
    group_id: str | None = None
    peer_id: str | None = None
    chat_name: str | None = None
    sender_id: str
    sender_name: str | None = None
    sender_card: str | None = None
    message_id: str | None = None
    message_seq: str | None = None
    timestamp_ms: int
    timestamp_iso: str
    content: str
    text_content: str
    image_file_names: list[str] = Field(default_factory=list)
    uploaded_file_names: list[str] = Field(default_factory=list)
    emoji_tokens: list[str] = Field(default_factory=list)
    segments: list[NormalizedSegment] = Field(default_factory=list)
    reply_to: ReplyRef | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    raw_message: dict[str, Any] | None = None


class SourceChatSnapshot(BaseModel):
    chat_type: Literal["group", "private"]
    chat_id: str
    chat_name: str | None = None
    exported_at: datetime = Field(default_factory=lambda: datetime.now(EXPORT_TIMEZONE))
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)


class NormalizedSnapshot(BaseModel):
    chat_type: Literal["group", "private"]
    chat_id: str
    chat_name: str | None = None
    exported_at: datetime = Field(default_factory=lambda: datetime.now(EXPORT_TIMEZONE))
    metadata: dict[str, Any] = Field(default_factory=dict)
    messages: list[NormalizedMessage] = Field(default_factory=list)


class MaterializedAsset(BaseModel):
    message_id: str | None = None
    message_seq: str | None = None
    sender_id: str
    timestamp_iso: str
    asset_type: Literal["image", "video", "speech", "file", "sticker"]
    asset_role: str | None = None
    file_name: str | None = None
    source_path: str | None = None
    resolved_source_path: str | None = None
    exported_rel_path: str | None = None
    status: Literal["copied", "reused", "missing", "error"] = "missing"
    resolver: str | None = None
    missing_kind: str | None = None
    note: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ExportBundleResult(BaseModel):
    data_path: Path
    manifest_path: Path
    assets_dir: Path
    record_count: int
    copied_asset_count: int = 0
    reused_asset_count: int = 0
    missing_asset_count: int = 0
    error_asset_count: int = 0
    forensic_run_dir: Path | None = None
    forensic_summary_path: Path | None = None
    forensic_incident_count: int = 0
    assets: list[MaterializedAsset] = Field(default_factory=list)


class ExportRequest(BaseModel):
    chat_type: Literal["group", "private"]
    chat_id: str
    chat_name: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None
    include_raw: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


class WatchRequest(BaseModel):
    chat_type: Literal["group", "private"]
    chat_id: str
    chat_name: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
