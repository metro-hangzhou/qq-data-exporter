from __future__ import annotations

import json
from pathlib import Path

from ..models import CanonicalAssetRecord, CanonicalMessageRecord, ImportedChatBundle
from ..utils import make_asset_id, make_message_uid, parse_iso_to_ms


class QceJsonAdapter:
    source_type = "qce_json"

    def load(
        self,
        source_path: Path,
        *,
        progress_callback=None,
    ) -> ImportedChatBundle:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        chat_info = payload.get("chatInfo", {})
        chat_type = "group" if chat_info.get("type") == "group" else "private"
        chat_id = str(chat_info.get("id") or source_path.stem)
        messages: list[CanonicalMessageRecord] = []

        for ordinal, item in enumerate(payload.get("messages", [])):
            timestamp_iso = item.get("timestamp")
            timestamp_ms = parse_iso_to_ms(timestamp_iso)
            sender = item.get("sender", {})
            sender_id = str(sender.get("uin") or f"qce_sender::{ordinal}")
            message_uid = make_message_uid(
                source_type=self.source_type,
                chat_type=chat_type,
                chat_id=chat_id,
                message_id=item.get("messageId"),
                message_seq=item.get("messageSeq"),
                timestamp_ms=timestamp_ms,
                sender_id_raw=sender_id,
                ordinal=ordinal,
            )
            messages.append(
                CanonicalMessageRecord(
                    message_uid=message_uid,
                    import_source="qce_json",
                    fidelity="compat",
                    chat_type=chat_type,
                    chat_id=chat_id,
                    chat_name=chat_info.get("name"),
                    sender_id_raw=sender_id,
                    sender_name_raw=sender.get("name"),
                    message_id=item.get("messageId"),
                    message_seq=item.get("messageSeq"),
                    timestamp_ms=timestamp_ms,
                    timestamp_iso=timestamp_iso.replace("Z", "+00:00"),
                    content=item.get("content", {}).get("text", ""),
                    text_content=item.get("content", {}).get("text", ""),
                    assets=self._extract_assets(message_uid=message_uid, item=item),
                    extra={"source_payload": item},
                )
            )

        if not messages:
            raise ValueError(f"No messages found in QCE JSON: {source_path}")

        return ImportedChatBundle(
            source_type="qce_json",
            fidelity="compat",
            source_path=source_path,
            chat_type=chat_type,
            chat_id=chat_id,
            chat_name=chat_info.get("name"),
            messages=messages,
        )

    def _extract_assets(
        self, *, message_uid: str, item: dict
    ) -> list[CanonicalAssetRecord]:
        assets: list[CanonicalAssetRecord] = []
        raw_message = item.get("rawMessage", {})
        for index, element in enumerate(raw_message.get("elements", [])):
            element_type = int(element.get("elementType", 0))
            if element_type == 2:
                pic = element.get("picElement", {})
                file_name = pic.get("fileName")
                assets.append(
                    CanonicalAssetRecord(
                        asset_id=make_asset_id(message_uid, "image", file_name, index),
                        message_uid=message_uid,
                        asset_type="image",
                        file_name=file_name,
                        path=pic.get("sourcePath"),
                        md5=pic.get("md5HexStr"),
                        extra={
                            "width": pic.get("picWidth"),
                            "height": pic.get("picHeight"),
                        },
                    )
                )
            elif element_type == 3:
                file_element = element.get("fileElement", {})
                file_name = file_element.get("fileName")
                assets.append(
                    CanonicalAssetRecord(
                        asset_id=make_asset_id(message_uid, "file", file_name, index),
                        message_uid=message_uid,
                        asset_type="file",
                        file_name=file_name,
                        path=file_element.get("filePath"),
                        md5=file_element.get("fileMd5"),
                    )
                )
        return assets
