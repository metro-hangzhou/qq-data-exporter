from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from qq_data_core import WatchRequest
from qq_data_integrations.napcat import NapCatRealtimeProvider


class FakeEventClient:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    async def iter_events(self) -> AsyncIterator[dict]:
        for event in self._events:
            yield event


def test_realtime_provider_filters_matching_chat_messages() -> None:
    provider = NapCatRealtimeProvider(
        FakeEventClient(
            [
                {"post_type": "meta_event", "meta_event_type": "heartbeat"},
                {"post_type": "message", "message_type": "private", "user_id": 42},
                {"post_type": "message_sent", "message_type": "group", "group_id": 10001},
                {"post_type": "message", "message_type": "group", "group_id": 10001},
            ]
        )
    )

    async def collect() -> list[dict]:
        items: list[dict] = []
        async for event in provider.watch(WatchRequest(chat_type="group", chat_id="10001")):
            items.append(event)
        return items

    matched = asyncio.run(collect())
    assert len(matched) == 2
    assert all(event["message_type"] == "group" for event in matched)


def test_realtime_provider_includes_matching_notice_events() -> None:
    provider = NapCatRealtimeProvider(
        FakeEventClient(
            [
                {"post_type": "notice", "notice_type": "friend_recall", "user_id": 42, "message_id": 1},
                {"post_type": "notice", "notice_type": "group_recall", "group_id": 10001, "user_id": 42, "message_id": 2},
                {"post_type": "notice", "notice_type": "friend_recall", "user_id": 99, "message_id": 3},
            ]
        )
    )

    async def collect_private() -> list[dict]:
        items: list[dict] = []
        async for event in provider.watch(WatchRequest(chat_type="private", chat_id="42")):
            items.append(event)
        return items

    matched = asyncio.run(collect_private())
    assert len(matched) == 1
    assert matched[0]["notice_type"] == "friend_recall"
