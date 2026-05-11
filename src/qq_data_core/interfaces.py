from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from .models import ExportRequest, SourceChatSnapshot, WatchRequest


class HistoryProvider(Protocol):
    def fetch_snapshot(self, request: ExportRequest) -> SourceChatSnapshot:
        ...


class RealtimeProvider(Protocol):
    async def watch(self, request: WatchRequest) -> AsyncIterator[dict[str, Any]]:
        ...
