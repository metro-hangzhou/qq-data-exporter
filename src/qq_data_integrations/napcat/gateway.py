from __future__ import annotations

from collections.abc import AsyncIterator
from threading import RLock
from time import monotonic

from qq_data_core.models import ExportRequest, SourceChatSnapshot, WatchRequest

from .directory import NapCatMetadataDirectory
from .fast_history_client import NapCatFastHistoryClient
from .http_client import NapCatHttpClient
from .media_downloader import NapCatMediaDownloader
from .models import ChatHistoryBounds, ChatTarget, normalize_chat_type
from .provider import NapCatHistoryProvider
from .realtime import NapCatRealtimeProvider
from .settings import NapCatSettings
from .webui_client import NapCatWebUiClient, NapCatWebUiError
from .websocket_client import NapCatWebSocketClient


class NapCatGateway:
    HISTORY_BOUNDS_CACHE_TTL_S = 15.0

    def __init__(
        self,
        settings: NapCatSettings,
        *,
        http_client: NapCatHttpClient | None = None,
        ws_client: NapCatWebSocketClient | None = None,
        metadata_directory: NapCatMetadataDirectory | None = None,
    ) -> None:
        self._settings = settings
        self._http_client = http_client or NapCatHttpClient(
            settings.http_url,
            access_token=settings.access_token,
            use_system_proxy=settings.use_system_proxy,
        )
        self._fast_history_client = (
            NapCatFastHistoryClient(
                settings.fast_history_url,
                headers_provider=lambda: _build_fast_history_headers(settings),
                use_system_proxy=settings.use_system_proxy,
            )
            if settings.fast_history_mode != "off" and settings.fast_history_url
            else None
        )
        self._history_provider = NapCatHistoryProvider(
            self._http_client,
            fast_client=self._fast_history_client,
            fast_mode=settings.fast_history_mode,
        )
        self._metadata_directory = metadata_directory or NapCatMetadataDirectory(
            self._http_client,
            state_dir=settings.state_dir,
        )
        self._ws_client = ws_client
        self._realtime_provider: NapCatRealtimeProvider | None = None
        self._history_bounds_cache: dict[
            tuple[str, str, bool, bool],
            tuple[float, ChatHistoryBounds],
        ] = {}
        self._sync_lock = RLock()
        self._media_downloader = NapCatMediaDownloader(
            self._http_client,
            fast_client=self._fast_history_client,
            remote_cache_dir=settings.state_dir / "media_downloads",
            remote_base_url=settings.http_url,
            use_system_proxy=settings.use_system_proxy,
        )

    def close(self) -> None:
        with self._sync_lock:
            self._media_downloader.close()
            self._http_client.close()
            if self._fast_history_client is not None:
                self._fast_history_client.close()
            self._realtime_provider = None
            self._ws_client = None

    def list_targets(
        self,
        chat_type: str,
        keyword: str | None = None,
        *,
        refresh: bool = False,
        limit: int = 8,
    ) -> list[ChatTarget]:
        normalized_chat_type = self._normalize_chat_type(chat_type)
        with self._sync_lock:
            return self._metadata_directory.search(
                normalized_chat_type,
                keyword,
                refresh=refresh,
                limit=limit,
            )

    def list_cached_targets(
        self,
        chat_type: str,
        keyword: str | None = None,
        *,
        limit: int = 8,
    ) -> list[ChatTarget]:
        normalized_chat_type = self._normalize_chat_type(chat_type)
        with self._sync_lock:
            return self._metadata_directory.search_cached(
                normalized_chat_type,
                keyword,
                limit=limit,
            )

    def resolve_target(
        self,
        chat_type: str,
        query: str,
        *,
        refresh_if_missing: bool = True,
    ) -> ChatTarget:
        normalized_chat_type = self._normalize_chat_type(chat_type)
        with self._sync_lock:
            return self._metadata_directory.resolve(
                normalized_chat_type,
                query,
                refresh_if_missing=refresh_if_missing,
            )

    def count_targets(self, chat_type: str) -> int:
        with self._sync_lock:
            return self._metadata_directory.count(self._normalize_chat_type(chat_type))

    def count_cached_targets(self, chat_type: str) -> int:
        with self._sync_lock:
            return self._metadata_directory.count_cached(self._normalize_chat_type(chat_type))

    def fetch_snapshot(self, request: ExportRequest, *, progress_callback=None) -> SourceChatSnapshot:
        with self._sync_lock:
            return self._history_provider.fetch_snapshot(request, progress_callback=progress_callback)

    def fetch_snapshot_tail(
        self,
        request: ExportRequest,
        *,
        data_count: int,
        page_size: int = 100,
        progress_callback=None,
    ) -> SourceChatSnapshot:
        with self._sync_lock:
            return self._history_provider.fetch_snapshot_tail(
                request,
                data_count=data_count,
                page_size=page_size,
                progress_callback=progress_callback,
            )

    def fetch_history_before(
        self,
        request: ExportRequest,
        *,
        before_message_seq: str | None,
        count: int | None = None,
        progress_callback=None,
    ) -> SourceChatSnapshot:
        with self._sync_lock:
            return self._history_provider.fetch_snapshot_before(
                request,
                before_message_seq=before_message_seq,
                count=count,
                progress_callback=progress_callback,
            )

    def get_history_bounds(
        self,
        request: ExportRequest,
        *,
        page_size: int = 100,
        need_earliest: bool = True,
        need_final: bool = True,
        progress_callback=None,
        refresh: bool = False,
    ) -> ChatHistoryBounds:
        cache_key = (request.chat_type, request.chat_id, need_earliest, need_final)
        with self._sync_lock:
            if not refresh and cache_key in self._history_bounds_cache:
                cached_at, cached_bounds = self._history_bounds_cache[cache_key]
                if monotonic() - cached_at <= self.HISTORY_BOUNDS_CACHE_TTL_S:
                    return cached_bounds
                self._history_bounds_cache.pop(cache_key, None)
            bounds = self._history_provider.get_history_bounds(
                request,
                page_size=page_size,
                need_earliest=need_earliest,
                need_final=need_final,
                progress_callback=progress_callback,
            )
            self._history_bounds_cache[cache_key] = (monotonic(), bounds)
            return bounds

    def fetch_snapshot_between(
        self,
        request: ExportRequest,
        *,
        page_size: int = 100,
        progress_callback=None,
    ) -> SourceChatSnapshot:
        with self._sync_lock:
            return self._history_provider.fetch_snapshot_between(
                request,
                page_size=page_size,
                progress_callback=progress_callback,
            )

    def fetch_snapshot_tail_between(
        self,
        request: ExportRequest,
        *,
        data_count: int,
        page_size: int = 100,
        progress_callback=None,
    ) -> SourceChatSnapshot:
        with self._sync_lock:
            return self._history_provider.fetch_snapshot_tail_between(
                request,
                data_count=data_count,
                page_size=page_size,
                progress_callback=progress_callback,
            )

    def fetch_full_snapshot(
        self,
        request: ExportRequest,
        *,
        page_size: int = 100,
        progress_callback=None,
    ) -> SourceChatSnapshot:
        with self._sync_lock:
            return self._history_provider.fetch_full_snapshot(
                request,
                page_size=page_size,
                progress_callback=progress_callback,
            )

    async def watch(self, request: WatchRequest) -> AsyncIterator[dict]:
        provider = self._ensure_realtime_provider()
        async for event in provider.watch(request):
            yield event

    def build_media_download_callback(self):
        return self._media_downloader.download_for_export

    def build_media_download_manager(self):
        return self._media_downloader

    def cleanup_media_download_cache(self) -> dict[str, object]:
        with self._sync_lock:
            self._history_bounds_cache.clear()
            self._history_provider.reset_export_state()
            return self._media_downloader.cleanup_remote_cache()

    def reset_export_state(self) -> None:
        with self._sync_lock:
            self._history_bounds_cache.clear()
            self._history_provider.reset_export_state()
            self._media_downloader.reset_export_state()

    def _ensure_realtime_provider(self) -> NapCatRealtimeProvider:
        if self._realtime_provider is None:
            if self._ws_client is None:
                self._ws_client = NapCatWebSocketClient(
                    self._settings.ws_url,
                    access_token=self._settings.access_token,
                    use_system_proxy=self._settings.use_system_proxy,
                )
            self._realtime_provider = NapCatRealtimeProvider(self._ws_client)
        return self._realtime_provider

    def _normalize_chat_type(self, chat_type: str) -> str:
        return normalize_chat_type(chat_type)


def _build_fast_history_headers(settings: NapCatSettings) -> dict[str, str] | None:
    if not settings.webui_url or not settings.webui_token:
        return None
    client = NapCatWebUiClient(
        settings.webui_url,
        raw_token=settings.webui_token,
        use_system_proxy=settings.use_system_proxy,
    )
    try:
        credential = client.ensure_authenticated()
    except NapCatWebUiError:
        return None
    finally:
        client.close()
    return {"Authorization": f"Bearer {credential}"}
