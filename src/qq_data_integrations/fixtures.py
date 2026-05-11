from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from qq_data_core.models import EXPORT_TIMEZONE, SourceChatSnapshot


class FixtureSnapshotLoader:
    def load_export(
        self,
        fixture_path: Path,
        *,
        chat_id: str | None = None,
        chat_name: str | None = None,
    ) -> SourceChatSnapshot:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        chat_info = payload.get("chatInfo", {})
        chat_type = "group" if chat_info.get("type") == "group" else "private"
        resolved_chat_id = (
            chat_id
            or str(chat_info.get("id") or payload.get("chatId") or "")
            or self._guess_chat_id(payload, chat_type)
        )
        return SourceChatSnapshot(
            chat_type=chat_type,
            chat_id=resolved_chat_id,
            chat_name=chat_name or chat_info.get("name"),
            exported_at=datetime.now(EXPORT_TIMEZONE),
            metadata={"source": str(fixture_path)},
            messages=payload.get("messages", []),
        )

    def _guess_chat_id(self, payload: dict[str, Any], chat_type: str) -> str:
        messages = payload.get("messages", [])
        if not messages:
            return "unknown"
        first_message = messages[0]
        if chat_type == "private":
            candidates = [
                first_message.get("receiver", {}).get("uin"),
                first_message.get("rawMessage", {}).get("peerUin"),
                first_message.get("peer_id"),
            ]
        else:
            candidates = [
                first_message.get("group_id"),
                first_message.get("rawMessage", {}).get("peerUin"),
            ]
        for value in candidates:
            if value:
                return str(value)
        return "unknown"
