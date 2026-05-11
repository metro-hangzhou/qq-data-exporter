from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Callable

from ..models import CanonicalAssetRecord, CanonicalMessageRecord, ImportedChatBundle
from ..runtime_control import maybe_cooperative_yield
from ..utils import make_asset_id, make_message_uid


class ExporterJsonlAdapter:
    source_type = "exporter_jsonl"

    def load(
        self,
        source_path: Path,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> ImportedChatBundle:
        messages: list[CanonicalMessageRecord] = []
        manifest_payload = self._load_manifest_payload(source_path)
        manifest_asset_index = self._build_manifest_asset_index(manifest_payload)
        with source_path.open("r", encoding="utf-8") as handle:
            for ordinal, line in enumerate(handle):
                if not line.strip():
                    continue
                payload = self._normalize_payload(
                    source_path=source_path,
                    payload=json.loads(line),
                    ordinal=ordinal,
                )
                message_uid = make_message_uid(
                    source_type=self.source_type,
                    chat_type=payload["chat_type"],
                    chat_id=payload["chat_id"],
                    message_id=payload.get("message_id"),
                    message_seq=payload.get("message_seq"),
                    timestamp_ms=int(payload["timestamp_ms"]),
                    sender_id_raw=payload["sender_id"],
                    ordinal=ordinal,
                )
                messages.append(
                    CanonicalMessageRecord(
                        message_uid=message_uid,
                        import_source="exporter_jsonl",
                        fidelity="high",
                        chat_type=payload["chat_type"],
                        chat_id=payload["chat_id"],
                        chat_name=payload.get("chat_name"),
                        sender_id_raw=payload["sender_id"],
                        sender_name_raw=payload.get("sender_name"),
                        message_id=payload.get("message_id"),
                        message_seq=payload.get("message_seq"),
                        timestamp_ms=int(payload["timestamp_ms"]),
                        timestamp_iso=payload["timestamp_iso"],
                        content=payload.get("content", ""),
                        text_content=payload.get("text_content", ""),
                        assets=self._extract_assets(
                            message_uid=message_uid,
                            payload=payload,
                            manifest_asset_index=manifest_asset_index,
                        ),
                        extra={"source_payload": self._compact_source_payload(payload)},
                    )
                )
                maybe_cooperative_yield(ordinal + 1)
                if progress_callback is not None and (ordinal + 1) % 1000 == 0:
                    progress_callback(
                        {
                            "phase": "load_parse",
                            "current": ordinal + 1,
                            "total": 0,
                            "message": f"Parsed {(ordinal + 1)} JSONL messages",
                        }
                    )

        if not messages:
            raise ValueError(f"No messages found in exporter JSONL: {source_path}")

        first = messages[0]
        return ImportedChatBundle(
            source_type="exporter_jsonl",
            fidelity="high",
            source_path=source_path,
            chat_type=first.chat_type,
            chat_id=first.chat_id,
            chat_name=first.chat_name,
            messages=messages,
            metadata=self._extract_manifest_metadata(manifest_payload),
        )

    def _load_manifest_payload(self, source_path: Path) -> dict[str, Any] | None:
        manifest_path = source_path.with_suffix("").with_suffix(".manifest.json")
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def _build_manifest_asset_index(
        self, manifest_payload: dict[str, Any] | None
    ) -> dict[tuple[str, str, str, str], list[dict[str, Any]]]:
        if not manifest_payload:
            return {}
        output: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        for item in self._iter_manifest_assets(manifest_payload):
            output[self._manifest_key(item)].append(item)
        return dict(output)

    def _iter_manifest_assets(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        top_level_assets = payload.get("assets")
        if isinstance(top_level_assets, list):
            return [item for item in top_level_assets if isinstance(item, dict)]
        content_summary = payload.get("content_summary")
        if isinstance(content_summary, dict):
            nested_assets = content_summary.get("assets")
            if isinstance(nested_assets, list):
                return [item for item in nested_assets if isinstance(item, dict)]
        return []

    def _extract_manifest_metadata(
        self, manifest_payload: dict[str, Any] | None
    ) -> dict[str, Any]:
        if not manifest_payload:
            return {}
        content_summary = manifest_payload.get("content_summary")
        summary = content_summary if isinstance(content_summary, dict) else manifest_payload
        metadata = summary.get("metadata")
        return {
            "manifest_shape": (
                "content_summary_wrapped"
                if isinstance(content_summary, dict)
                else "top_level_v1"
            ),
            "manifest_schema_version": manifest_payload.get("schema_version"),
            "history_source": (
                metadata.get("source") if isinstance(metadata, dict) else None
            ),
            "asset_summary": summary.get("asset_summary") or {},
            "missing_breakdown": summary.get("missing_breakdown") or {},
        }

    def _normalize_payload(
        self,
        *,
        source_path: Path,
        payload: dict[str, Any],
        ordinal: int,
    ) -> dict[str, Any]:
        if "chat_type" in payload and "chat_id" in payload and "timestamp_ms" in payload:
            return payload
        normalized = dict(payload)
        local_summary = self._load_local_corpus_summary(source_path)
        timestamp_iso = str(normalized.get("timestamp_iso") or "").strip()
        if "chat_type" not in normalized:
            normalized["chat_type"] = "group"
        if "chat_id" not in normalized:
            normalized["chat_id"] = str(
                local_summary.get("group_id")
                or local_summary.get("chat_id")
                or normalized.get("group_id")
                or ""
            )
        if "timestamp_ms" not in normalized:
            if not timestamp_iso:
                raise KeyError(
                    f"Unsupported JSONL message without timestamp_ms/timestamp_iso at {source_path}:{ordinal + 1}"
                )
            normalized["timestamp_ms"] = int(
                datetime.fromisoformat(timestamp_iso).timestamp() * 1000
            )
        if "sender_id" not in normalized and "sender_id_raw" in normalized:
            normalized["sender_id"] = normalized["sender_id_raw"]
        if "sender_name" not in normalized and "sender_name_raw" in normalized:
            normalized["sender_name"] = normalized["sender_name_raw"]
        if "chat_name" not in normalized:
            normalized["chat_name"] = (
                local_summary.get("group_name")
                or local_summary.get("chat_name")
                or None
            )
        return normalized

    def _load_local_corpus_summary(self, source_path: Path) -> dict[str, Any]:
        summary_path = source_path.parent / "summary.json"
        if not summary_path.exists():
            return {}
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
        return {}

    def _extract_assets(
        self,
        *,
        message_uid: str,
        payload: dict,
        manifest_asset_index: dict[tuple[str, str, str, str], list[dict[str, Any]]],
    ) -> list[CanonicalAssetRecord]:
        assets: list[CanonicalAssetRecord] = []
        for index, segment in enumerate(payload.get("segments", [])):
            segment_type = segment.get("type")
            if segment_type not in {"image", "file", "video"}:
                continue
            file_name = segment.get("file_name")
            manifest_asset = self._pop_manifest_asset(
                payload=payload,
                segment_type=segment_type,
                file_name=file_name,
                manifest_asset_index=manifest_asset_index,
            )
            extra = dict(segment.get("extra", {}))
            path = segment.get("path")
            if manifest_asset is not None:
                materialized = manifest_asset.get("status") in {"copied", "reused"}
                extra.update(
                    {
                        "materialized": materialized,
                        "materialization_status": manifest_asset.get("status"),
                        "materialization_resolver": manifest_asset.get("resolver"),
                        "materialization_exported_rel_path": manifest_asset.get(
                            "exported_rel_path"
                        ),
                        "materialization_note": manifest_asset.get("note"),
                        "materialization_asset_role": manifest_asset.get("asset_role"),
                        "materialization_source_path": manifest_asset.get(
                            "source_path"
                        ),
                        "materialization_resolved_source_path": manifest_asset.get(
                            "resolved_source_path"
                        ),
                        "materialization_timestamp_iso": manifest_asset.get(
                            "timestamp_iso"
                        ),
                    }
                )
                path = (
                    path
                    or manifest_asset.get("resolved_source_path")
                    or manifest_asset.get("source_path")
                )
            assets.append(
                CanonicalAssetRecord(
                    asset_id=make_asset_id(message_uid, segment_type, file_name, index),
                    message_uid=message_uid,
                    asset_type=segment_type,
                    file_name=file_name,
                    path=path,
                    md5=segment.get("md5"),
                    extra=extra,
                )
            )
        return assets

    def _pop_manifest_asset(
        self,
        *,
        payload: dict[str, Any],
        segment_type: str,
        file_name: str | None,
        manifest_asset_index: dict[tuple[str, str, str, str], list[dict[str, Any]]],
    ) -> dict[str, Any] | None:
        if not manifest_asset_index:
            return None
        file_key = str(file_name or "")
        message_id = str(payload.get("message_id") or "")
        message_seq = str(payload.get("message_seq") or "")
        for key in (
            (message_id, message_seq, segment_type, file_key),
            (message_id, "", segment_type, file_key),
            ("", message_seq, segment_type, file_key),
        ):
            bucket = manifest_asset_index.get(key)
            if bucket:
                return bucket.pop(0)
        return None

    def _manifest_key(self, asset: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(asset.get("message_id") or ""),
            str(asset.get("message_seq") or ""),
            str(asset.get("asset_type") or "unknown"),
            str(asset.get("file_name") or ""),
        )

    def _compact_source_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        segments = payload.get("segments", []) or []
        compact_segments = []
        for segment in segments:
            compact = self._compact_segment(segment)
            if compact is not None:
                compact_segments.append(compact)
        return {
            "reply_to": payload.get("reply_to"),
            "segments": compact_segments,
        }

    def _compact_segment(self, segment: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(segment, dict):
            return None
        segment_type = str(segment.get("type") or "")
        if not segment_type:
            return None
        compact: dict[str, Any] = {"type": segment_type}
        extra = segment.get("extra") or {}
        for source_key in (
            "token",
            "text",
            "file_name",
            "path",
            "md5",
            "emoji_id",
            "emoji_package_id",
            "summary",
        ):
            value = segment.get(source_key)
            if value not in (None, "", [], {}):
                compact[source_key] = value

        compact_extra: dict[str, Any] = {}
        for source_key in (
            "url",
            "file_id",
            "fileUuid",
            "fileSize",
            "file_biz_id",
            "fileBizId",
            "remote_url",
            "remote_file_name",
            "static_path",
            "dynamic_path",
            "message_id_raw",
            "element_id",
            "peer_uid",
            "chat_type_raw",
            "forward_id",
            "res_id",
            "source_name",
            "summary_text",
            "preview_text",
            "forwarded_count",
        ):
            value = extra.get(source_key)
            if value not in (None, "", [], {}):
                compact_extra[source_key] = value
        preview_lines = extra.get("preview_lines")
        if isinstance(preview_lines, list) and preview_lines:
            compact_extra["preview_lines"] = preview_lines

        children = extra.get("children")
        if not isinstance(children, list):
            children = self._compact_forward_messages(extra.get("forward_messages"))
        if children:
            compact_extra["children"] = children

        forward_depth = extra.get("forward_depth")
        if isinstance(forward_depth, int) and forward_depth > 0:
            compact_extra["forward_depth"] = forward_depth
        if compact_extra:
            compact["extra"] = compact_extra
        return compact

    def _compact_forward_messages(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        compact_nodes: list[dict[str, Any]] = []
        for node in value:
            if not isinstance(node, dict):
                continue
            node_segments = []
            for segment in node.get("segments") or []:
                compact = self._compact_segment(segment)
                if compact is not None:
                    node_segments.append(compact)
            compact_node: dict[str, Any] = {}
            for source_key in (
                "sender_id",
                "sender_name",
                "raw_sender_id",
                "raw_sender_name",
                "alias_sender_id",
                "alias_sender_name",
                "avatar_url",
                "content",
                "text_content",
                "timestamp_iso",
            ):
                value = node.get(source_key)
                if value not in (None, "", [], {}):
                    compact_node[source_key] = value
            reply_to = node.get("reply_to")
            if isinstance(reply_to, dict) and reply_to:
                compact_node["reply_to"] = dict(reply_to)
            if node_segments:
                compact_node["segments"] = node_segments
            if compact_node:
                compact_nodes.append(compact_node)
        return compact_nodes
