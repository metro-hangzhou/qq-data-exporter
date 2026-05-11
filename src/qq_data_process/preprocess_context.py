from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from .adapters import ExporterJsonlAdapter
from .models import CanonicalAssetRecord, CanonicalMessageRecord, ImportedChatBundle
from .preprocess_models import PreprocessDirective
import qq_data_process.models as _models

if not hasattr(_models, "PROCESS_TIMEZONE"):
    _models.PROCESS_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
if not hasattr(_models, "ResourceState"):
    _models.ResourceState = Literal[
        "available",
        "missing",
        "expired",
        "placeholder",
        "unsupported",
    ]
if not hasattr(_models, "CorpusLineage"):
    class _CompatCorpusLineage(BaseModel):
        model_config = ConfigDict(extra="allow")

        source_export_id: str | None = None
        source_message_id: str | None = None
        source_asset_key: str | None = None
        source_chat_id: str | None = None
        extra: dict[str, Any] = Field(default_factory=dict)

    _models.CorpusLineage = _CompatCorpusLineage
if not hasattr(_models, "CorpusProvenance"):
    class _CompatCorpusProvenance(BaseModel):
        model_config = ConfigDict(extra="allow")

        build_profile: str = "preprocess"
        created_at: datetime = Field(
            default_factory=lambda: datetime.now(_models.PROCESS_TIMEZONE)
        )
        source_type: str | None = None
        source_path: str | None = None
        extra: dict[str, Any] = Field(default_factory=dict)

    _models.CorpusProvenance = _CompatCorpusProvenance

CorpusLineage = _models.CorpusLineage
CorpusProvenance = _models.CorpusProvenance


class ImportedBundleSegment(BaseModel):
    type: str
    token: str | None = None
    text: str | None = None
    file_name: str | None = None
    path: str | None = None
    md5: str | None = None
    emoji_id: str | None = None
    emoji_package_id: str | None = None
    summary: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ImportedBundleMessage(BaseModel):
    message_uid: str
    message_id: str | None = None
    message_seq: str | None = None
    chat_type: str
    chat_id: str
    sender_id: str
    sender_name: str | None = None
    sender_card: str | None = None
    timestamp_ms: int
    timestamp_iso: str
    content: str
    text_content: str
    segments: list[ImportedBundleSegment] = Field(default_factory=list)
    reply_to: dict[str, Any] | None = None
    image_file_names: list[str] = Field(default_factory=list)
    uploaded_file_names: list[str] = Field(default_factory=list)
    emoji_tokens: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage | None = None


class ImportedBundleAsset(BaseModel):
    asset_id: str
    message_uid: str
    message_id: str | None = None
    message_seq: str | None = None
    asset_type: str
    file_name: str | None = None
    source_path: str | None = None
    exported_rel_path: str | None = None
    digest: str | None = None
    resource_state: str = "unsupported"
    materialization_status: str | None = None
    lineage: CorpusLineage | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ImportedBundleThread(BaseModel):
    thread_id: str
    message_ids: list[str] = Field(default_factory=list)
    participant_ids: list[str] = Field(default_factory=list)


class ImportedBundleManifest(BaseModel):
    corpus_id: str
    chat_type: str
    chat_id: str
    chat_name: str | None = None
    source_exports: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage
    provenance: CorpusProvenance


class ImportedBundlePreprocessContext(BaseModel):
    context_id: str
    manifest: ImportedBundleManifest
    messages: list[ImportedBundleMessage] = Field(default_factory=list)
    assets: list[ImportedBundleAsset] = Field(default_factory=list)
    threads: list[ImportedBundleThread] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    directive: PreprocessDirective | None = None
    lineage: CorpusLineage | None = None
    provenance: CorpusProvenance


def build_preprocess_context_from_exporter_jsonl(
    source_path: str | Path,
    *,
    context_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    directive: PreprocessDirective | None = None,
) -> ImportedBundlePreprocessContext:
    bundle = ExporterJsonlAdapter().load(Path(source_path))
    return build_preprocess_context_from_bundle(
        bundle,
        source_exports=[str(Path(source_path).resolve())],
        context_id=context_id,
        metadata=metadata,
        directive=directive,
    )


def build_preprocess_context_from_bundle(
    bundle: ImportedChatBundle,
    *,
    source_exports: Iterable[str] | None = None,
    context_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    directive: PreprocessDirective | None = None,
) -> ImportedBundlePreprocessContext:
    resolved_source_exports = [str(item) for item in (source_exports or [str(bundle.source_path.resolve())])]
    base_lineage = CorpusLineage(
        source_export_id=_stable_token(*resolved_source_exports),
        source_chat_id=bundle.chat_id,
        extra={
            "source_type": bundle.source_type,
            "fidelity": bundle.fidelity,
        },
    )
    provenance = CorpusProvenance(
        build_profile="imported_bundle",
        source_type=bundle.source_type,
        source_path=str(bundle.source_path),
    )
    prepared_messages = _build_messages(bundle, base_lineage=base_lineage)
    prepared_assets = _build_assets(bundle, base_lineage=base_lineage)
    prepared_threads = _build_threads(prepared_messages)
    message_thread_lookup = {
        message_id: thread.thread_id
        for thread in prepared_threads
        for message_id in thread.message_ids
    }
    for message in prepared_messages:
        message_key = message.message_id or (f"seq:{message.message_seq}" if message.message_seq else None)
        if message_key and message_key in message_thread_lookup:
            message.extra["thread_id"] = message_thread_lookup[message_key]

    resolved_context_id = context_id or (
        f"bundle_{bundle.chat_type}_{bundle.chat_id}_"
        f"{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')}"
    )
    merged_metadata = {
        "source_type": bundle.source_type,
        "fidelity": bundle.fidelity,
        "source_path": str(bundle.source_path),
        "source_exports": resolved_source_exports,
        "delivery_profile": "raw_plus_processed",
        **dict(bundle.metadata or {}),
        **dict(metadata or {}),
    }
    manifest = ImportedBundleManifest(
        corpus_id=f"{bundle.chat_type}_{bundle.chat_id}",
        chat_type=bundle.chat_type,
        chat_id=bundle.chat_id,
        chat_name=bundle.chat_name,
        source_exports=resolved_source_exports,
        metadata=merged_metadata,
        lineage=base_lineage,
        provenance=provenance,
    )
    return ImportedBundlePreprocessContext(
        context_id=resolved_context_id,
        manifest=manifest,
        messages=prepared_messages,
        assets=prepared_assets,
        threads=prepared_threads,
        metadata=merged_metadata,
        directive=directive,
        lineage=base_lineage,
        provenance=provenance,
    )


def _build_messages(
    bundle: ImportedChatBundle,
    *,
    base_lineage: CorpusLineage,
) -> list[ImportedBundleMessage]:
    prepared: list[ImportedBundleMessage] = []
    for message in bundle.messages:
        source_payload = _source_payload(message)
        prepared.append(
            ImportedBundleMessage(
                message_uid=message.message_uid,
                message_id=message.message_id,
                message_seq=message.message_seq,
                chat_type=message.chat_type,
                chat_id=message.chat_id,
                sender_id=message.sender_id_raw,
                sender_name=message.sender_name_raw,
                timestamp_ms=message.timestamp_ms,
                timestamp_iso=message.timestamp_iso,
                content=message.content,
                text_content=message.text_content or message.content,
                segments=_segments_from_payload(source_payload.get("segments")),
                reply_to=_reply_to_from_payload(source_payload.get("reply_to")),
                image_file_names=_segment_file_names(source_payload.get("segments"), segment_type="image"),
                uploaded_file_names=_segment_file_names(source_payload.get("segments"), segment_type="file"),
                emoji_tokens=_segment_emoji_tokens(source_payload.get("segments")),
                extra={
                    "source_payload": source_payload,
                    "import_source": message.import_source,
                    "fidelity": message.fidelity,
                },
                lineage=base_lineage.model_copy(
                    update={
                        "source_message_id": message.message_id,
                        "extra": {
                            **dict(base_lineage.extra or {}),
                            "message_uid": message.message_uid,
                            "message_seq": message.message_seq,
                        },
                    }
                ),
            )
        )
    return prepared


def _build_assets(
    bundle: ImportedChatBundle,
    *,
    base_lineage: CorpusLineage,
) -> list[ImportedBundleAsset]:
    prepared: list[ImportedBundleAsset] = []
    for message in bundle.messages:
        for asset in message.assets:
            prepared.append(
                ImportedBundleAsset(
                    asset_id=asset.asset_id,
                    message_uid=asset.message_uid,
                    message_id=message.message_id,
                    message_seq=message.message_seq,
                    asset_type=asset.asset_type,
                    file_name=asset.file_name,
                    source_path=_asset_source_path(asset),
                    exported_rel_path=_string_or_none(asset.extra.get("materialization_exported_rel_path")),
                    digest=_string_or_none(asset.md5),
                    resource_state=_resource_state_from_asset(asset),
                    materialization_status=_string_or_none(asset.extra.get("materialization_status")),
                    lineage=base_lineage.model_copy(
                        update={
                            "source_message_id": message.message_id,
                            "source_asset_key": asset.asset_id,
                            "extra": {
                                **dict(base_lineage.extra or {}),
                                "message_uid": message.message_uid,
                                "message_seq": message.message_seq,
                            },
                        }
                    ),
                    extra=dict(asset.extra),
                )
            )
    return prepared


def _build_threads(messages: list[ImportedBundleMessage]) -> list[ImportedBundleThread]:
    thread_members: dict[str, list[ImportedBundleMessage]] = {}
    for message in messages:
        reply_to = message.reply_to or {}
        referenced_id = _string_or_none(reply_to.get("referenced_message_id"))
        if not referenced_id:
            continue
        thread_id = f"reply_thread_{sha1(referenced_id.encode('utf-8')).hexdigest()[:10]}"
        thread_members.setdefault(thread_id, []).append(message)
    threads: list[ImportedBundleThread] = []
    for thread_id, items in sorted(thread_members.items()):
        message_ids = [
            message.message_id or f"seq:{message.message_seq}"
            for message in items
            if message.message_id or message.message_seq
        ]
        participant_ids = sorted({message.sender_id for message in items if message.sender_id})
        if not message_ids:
            continue
        threads.append(
            ImportedBundleThread(
                thread_id=thread_id,
                message_ids=message_ids,
                participant_ids=participant_ids,
            )
        )
    return threads


def _source_payload(message: CanonicalMessageRecord) -> dict[str, Any]:
    source_payload = message.extra.get("source_payload") if isinstance(message.extra, dict) else None
    if isinstance(source_payload, dict):
        return source_payload
    return {}


def _segments_from_payload(value: Any) -> list[ImportedBundleSegment]:
    segments: list[ImportedBundleSegment] = []
    if not isinstance(value, list):
        return segments
    for item in value:
        if not isinstance(item, dict):
            continue
        extra = dict(item.get("extra") or {})
        for key, raw_value in item.items():
            if key in {
                "type",
                "token",
                "text",
                "file_name",
                "path",
                "md5",
                "emoji_id",
                "emoji_package_id",
                "summary",
                "extra",
            }:
                continue
            extra[key] = raw_value
        segments.append(
            ImportedBundleSegment(
                type=str(item.get("type") or "text"),
                token=_string_or_none(item.get("token")),
                text=_string_or_none(item.get("text")),
                file_name=_string_or_none(item.get("file_name")),
                path=_string_or_none(item.get("path")),
                md5=_string_or_none(item.get("md5")),
                emoji_id=_string_or_none(item.get("emoji_id")),
                emoji_package_id=_string_or_none(item.get("emoji_package_id")),
                summary=_string_or_none(item.get("summary")),
                extra=extra,
            )
        )
    return segments


def _reply_to_from_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    return None


def _segment_file_names(value: Any, *, segment_type: str) -> list[str]:
    names: list[str] = []
    if not isinstance(value, list):
        return names
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != segment_type:
            continue
        file_name = _string_or_none(item.get("file_name"))
        if file_name:
            names.append(file_name)
    return names


def _segment_emoji_tokens(value: Any) -> list[str]:
    tokens: list[str] = []
    if not isinstance(value, list):
        return tokens
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") != "emoji":
            continue
        token = _string_or_none(item.get("token"))
        if token:
            tokens.append(token)
    return tokens


def _asset_source_path(asset: CanonicalAssetRecord) -> str | None:
    return (
        _string_or_none(asset.extra.get("materialization_resolved_source_path"))
        or _string_or_none(asset.extra.get("materialization_source_path"))
        or _string_or_none(asset.path)
    )


def _resource_state_from_asset(asset: CanonicalAssetRecord) -> str:
    materialization_status = _string_or_none(asset.extra.get("materialization_status")) or ""
    resolver = (_string_or_none(asset.extra.get("materialization_resolver")) or "").lower()
    note = (_string_or_none(asset.extra.get("materialization_note")) or "").lower()
    if materialization_status in {"copied", "reused"}:
        return "available"
    if "placeholder" in resolver:
        return "placeholder"
    if "expired" in resolver:
        return "expired"
    if materialization_status == "missing":
        if "placeholder" in note:
            return "placeholder"
        return "missing"
    return "unsupported"


def _stable_token(*parts: str) -> str:
    payload = "|".join(part for part in parts if part)
    return sha1(payload.encode("utf-8")).hexdigest()[:16]


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
