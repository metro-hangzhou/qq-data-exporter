from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol

from qq_data_core.models import WatchRequest


class EventStreamClient(Protocol):
    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        ...


class NapCatRealtimeProvider:
    _SUPPORTED_NOTICE_TYPES = frozenset(
        {
            "friend_recall",
            "group_recall",
            "group_upload",
            "online_file_send",
            "online_file_receive",
            "group_admin",
            "group_increase",
            "group_decrease",
            "group_ban",
            "friend_add",
            "group_msg_emoji_like",
            "essence",
            "group_card",
            "notify",
        }
    )

    def __init__(self, client: EventStreamClient) -> None:
        self._client = client

    async def watch(self, request: WatchRequest) -> AsyncIterator[dict[str, Any]]:
        async for event in self._client.iter_events():
            if not self._is_watch_event(event):
                continue
            if self._resolve_chat_type(event) != request.chat_type:
                continue
            if self._resolve_chat_id(event) != request.chat_id:
                continue
            yield event

    def _is_watch_event(self, event: dict[str, Any]) -> bool:
        post_type = event.get("post_type")
        if post_type in {"message", "message_sent"}:
            return event.get("message_type") in {"group", "private"}
        if post_type != "notice":
            return False
        notice_type = str(event.get("notice_type") or "").strip()
        if notice_type not in self._SUPPORTED_NOTICE_TYPES:
            return False
        chat_type = self._resolve_chat_type(event)
        chat_id = self._resolve_chat_id(event)
        return chat_type in {"group", "private"} and bool(chat_id)

    def _resolve_chat_type(self, event: dict[str, Any]) -> str:
        if event.get("post_type") in {"message", "message_sent"}:
            return "group" if event.get("message_type") == "group" else "private"
        notice_type = str(event.get("notice_type") or "").strip()
        if event.get("group_id") is not None or notice_type in {
            "group_recall",
            "group_upload",
            "group_admin",
            "group_increase",
            "group_decrease",
            "group_ban",
            "group_msg_emoji_like",
            "essence",
            "group_card",
        }:
            return "group"
        if notice_type in {"friend_recall", "friend_add", "online_file_send", "online_file_receive"}:
            return "private"
        return ""

    def _resolve_chat_id(self, event: dict[str, Any]) -> str:
        if event.get("post_type") in {"message", "message_sent"}:
            value = event.get("group_id") if event.get("message_type") == "group" else event.get("user_id")
            return str(value or "")
        chat_type = self._resolve_chat_type(event)
        notice_type = str(event.get("notice_type") or "").strip()
        if chat_type == "group" and event.get("group_id") is not None:
            return str(event.get("group_id") or "")
        if chat_type != "private":
            return ""
        key_order = {
            "friend_recall": ("user_id", "sender_id"),
            "friend_add": ("user_id",),
            "online_file_send": ("peer_id", "user_id"),
            "online_file_receive": ("peer_id", "user_id"),
        }.get(notice_type, ())
        for key in key_order:
            value = event.get(key)
            if value is not None:
                return str(value or "")
        return ""
