from __future__ import annotations

import re
from pathlib import Path

from ..models import CanonicalAssetRecord, CanonicalMessageRecord, ImportedChatBundle
from ..utils import make_asset_id, make_message_uid, parse_local_timestamp_to_ms

CHAT_NAME_RE = re.compile(r"^聊天名称:\s*(.+)$")
CHAT_TYPE_RE = re.compile(r"^聊天类型:\s*(.+)$")
SENDER_ID_RE = re.compile(r"^发送者ID:\s*(.+)$")
SENDER_RE = re.compile(r"^(.+):$")
TIME_RE = re.compile(r"^时间:\s*(.+)$")
CONTENT_RE = re.compile(r"^内容:\s*(.*)$")
RESOURCE_RE = re.compile(r"^\s*-\s*(\w+):\s*(.+)$")


class TxtTranscriptAdapter:
    source_type = "qq_txt"

    def load(
        self,
        source_path: Path,
        *,
        progress_callback=None,
    ) -> ImportedChatBundle:
        lines = source_path.read_text(encoding="utf-8").splitlines()
        chat_name = self._match_first(lines, CHAT_NAME_RE) or source_path.stem
        chat_type_raw = self._match_first(lines, CHAT_TYPE_RE) or "私聊"
        chat_type = "group" if "群" in chat_type_raw else "private"
        chat_id = f"txt::{source_path.stem}"

        messages: list[CanonicalMessageRecord] = []
        idx = 0
        ordinal = 0
        while idx < len(lines):
            sender_match = SENDER_RE.match(lines[idx].strip())
            if not sender_match:
                idx += 1
                continue
            sender_name = sender_match.group(1).strip()
            idx += 1

            while idx < len(lines) and not lines[idx].strip():
                idx += 1

            if idx >= len(lines):
                break
            sender_id = f"txt_sender::{sender_name}"
            sender_id_match = SENDER_ID_RE.match(lines[idx].strip())
            if sender_id_match:
                sender_id = sender_id_match.group(1).strip()
                idx += 1

            if idx >= len(lines):
                break
            time_match = TIME_RE.match(lines[idx].strip())
            if not time_match:
                idx += 1
                continue
            timestamp_value = time_match.group(1)
            idx += 1

            content_lines: list[str] = []
            if idx < len(lines):
                content_match = CONTENT_RE.match(lines[idx].strip())
                if content_match:
                    content_lines.append(content_match.group(1))
                    idx += 1

            resources: list[tuple[str, str]] = []
            while idx < len(lines):
                stripped = lines[idx].strip()
                resource_match = RESOURCE_RE.match(stripped)
                if resource_match:
                    resources.append((resource_match.group(1), resource_match.group(2)))
                    idx += 1
                    continue
                if not stripped:
                    idx += 1
                    break
                if SENDER_RE.match(stripped):
                    break
                if stripped.startswith("资源:"):
                    idx += 1
                    continue
                content_lines.append(stripped)
                idx += 1

            timestamp_ms, timestamp_iso = parse_local_timestamp_to_ms(timestamp_value)
            content_value = "\n".join(line for line in content_lines if line)
            message_uid = make_message_uid(
                source_type=self.source_type,
                chat_type=chat_type,
                chat_id=chat_id,
                message_id=None,
                message_seq=None,
                timestamp_ms=timestamp_ms,
                sender_id_raw=sender_id,
                ordinal=ordinal,
            )
            messages.append(
                CanonicalMessageRecord(
                    message_uid=message_uid,
                    import_source="qq_txt",
                    fidelity="lossy",
                    chat_type=chat_type,
                    chat_id=chat_id,
                    chat_name=chat_name,
                    sender_id_raw=sender_id,
                    sender_name_raw=sender_name,
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso,
                    content=content_value,
                    text_content=content_value
                    if not content_value.startswith("[")
                    else "",
                    assets=self._extract_assets(
                        message_uid=message_uid,
                        content_value=content_value,
                        resources=resources,
                    ),
                    extra={"lossy_sender_id": True},
                )
            )
            ordinal += 1

        if not messages:
            raise ValueError(f"No messages parsed from TXT transcript: {source_path}")

        return ImportedChatBundle(
            source_type="qq_txt",
            fidelity="lossy",
            source_path=source_path,
            chat_type=chat_type,
            chat_id=chat_id,
            chat_name=chat_name,
            messages=messages,
        )

    def _match_first(self, lines: list[str], pattern: re.Pattern[str]) -> str | None:
        for line in lines:
            match = pattern.match(line.strip())
            if match:
                return match.group(1).strip()
        return None

    def _extract_assets(
        self,
        *,
        message_uid: str,
        content_value: str,
        resources: list[tuple[str, str]],
    ) -> list[CanonicalAssetRecord]:
        assets: list[CanonicalAssetRecord] = []
        for index, (resource_type, name) in enumerate(resources):
            normalized = "unknown"
            if resource_type.lower() == "image":
                normalized = "image"
            elif resource_type.lower() == "file":
                normalized = "file"
            assets.append(
                CanonicalAssetRecord(
                    asset_id=make_asset_id(message_uid, normalized, name, index),
                    message_uid=message_uid,
                    asset_type=normalized,
                    file_name=name,
                    extra={"resource_type": resource_type},
                )
            )
        if assets:
            return assets

        bracket_match = re.match(r"^\[(图片|文件):\s*(.+)\]$", content_value)
        if not bracket_match:
            return []
        resource_label = bracket_match.group(1)
        name = bracket_match.group(2)
        normalized = "image" if resource_label == "图片" else "file"
        return [
            CanonicalAssetRecord(
                asset_id=make_asset_id(message_uid, normalized, name, 0),
                message_uid=message_uid,
                asset_type=normalized,
                file_name=name,
            )
        ]
