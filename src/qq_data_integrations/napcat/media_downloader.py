from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
from threading import Event, Lock, Thread
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FutureTimeoutError,
    as_completed,
)
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from qq_data_core.paths import build_timestamp_token

from .fast_history_client import (
    NapCatFastHistoryClient,
    NapCatFastHistoryError,
    NapCatFastHistoryTimeoutError,
    NapCatFastHistoryUnavailable,
)
from .http_client import NapCatApiError, NapCatApiTimeoutError, NapCatHttpClient

logging.getLogger(__name__).addHandler(logging.NullHandler())


class NapCatMediaDownloader:
    OLD_CONTEXT_BUCKET_MIN_AGE_DAYS = 120
    OLD_CONTEXT_BUCKET_FAILURE_LIMIT = 5
    SHARED_MISS_CACHE_MIN_AGE_DAYS = 30
    FORWARD_TIMEOUT_STORM_MIN_AGE_DAYS = 45
    FORWARD_TIMEOUT_STORM_GLOBAL_MIN_AGE_DAYS = 180
    FORWARD_TIMEOUT_STORM_LIMIT = 6
    FORWARD_TIMEOUT_STORM_SLOW_NOOP_ELAPSED_S = 10.0
    OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S = 4.0
    OLD_FORWARD_EXPENSIVE_DIRECT_FILE_ID_TIMEOUT_S = 4.0
    OLD_FORWARD_EXPENSIVE_METADATA_TIMEOUT_S = 6.0
    OLD_FORWARD_EXPENSIVE_MATERIALIZE_TIMEOUT_S = 8.0
    PREFETCH_BATCH_SIZE = 200
    PREFETCH_LARGE_REQUEST_THRESHOLD = 1000
    PREFETCH_LARGE_BATCH_SIZE = 50
    PREFETCH_BATCH_TIMEOUT_S = 20.0
    PREFETCH_BATCH_TIMEOUT_STRIKE_LIMIT = 2
    PREFETCH_TOTAL_BUDGET_S = 30.0
    PREFETCH_SLOW_CHUNK_WARN_S = 15.0
    FORWARD_TARGET_DOWNLOAD_TIMEOUT_MS = 20_000
    DIRECT_FILE_ID_TIMEOUT_S = 12.0
    PUBLIC_TOKEN_ACTION_TIMEOUT_S = 12.0
    PUBLIC_TOKEN_PREFETCH_WAIT_S = 0.15
    REMOTE_PREFETCH_PEEK_WAIT_S = 0.05
    CONTEXT_ROUTE_TIMEOUT_S = 12.0
    FORWARD_CONTEXT_TIMEOUT_S = 12.0
    FORWARD_TARGET_HTTP_TIMEOUT_S = 25.0
    SLOW_REMOTE_SUBSTEP_WARN_S = 3.0
    REMOTE_MEDIA_FETCH_TIMEOUT_S = 8.0
    REMOTE_MEDIA_FETCH_WORKERS = 6
    PUBLIC_TOKEN_PREFETCH_WORKERS = 3
    FORWARD_METADATA_PREFETCH_WORKERS = 6
    REMOTE_PREFETCHABLE_ASSET_TYPES = frozenset({"image", "file", "video", "speech"})

    def __init__(
        self,
        client: NapCatHttpClient,
        *,
        fast_client: NapCatFastHistoryClient | None = None,
        remote_cache_dir: Path | None = None,
        remote_base_url: str | None = None,
        use_system_proxy: bool = False,
        remote_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = client
        self._fast_client = fast_client
        self._logger = logging.getLogger(__name__)
        self._fast_context_route_disabled = False
        self._fast_forward_context_route_disabled = False
        self._old_context_failure_buckets: dict[tuple[str, str], int] = {}
        self._old_context_skip_logged: set[tuple[str, str]] = set()
        self._old_context_expired_buckets: set[tuple[str, str]] = set()
        self._forward_context_timeout_cache: set[tuple[str, ...]] = set()
        self._forward_context_empty_cache: set[tuple[str, ...]] = set()
        self._forward_context_error_cache: set[tuple[str, ...]] = set()
        self._forward_context_unavailable_cache: set[tuple[str, ...]] = set()
        self._forward_context_payload_cache: dict[tuple[str, ...], dict[str, Any]] = {}
        self._request_scoped_public_action_timeout_cache: set[tuple[str, ...]] = set()
        self._forward_parent_public_action_timeout_cache: set[tuple[str, ...]] = set()
        self._direct_file_id_timeout_cache: set[tuple[Any, ...]] = set()
        self._forward_timeout_storm_counts: dict[tuple[str, ...], int] = {}
        self._forward_timeout_storm_open: set[tuple[str, ...]] = set()
        self._remote_cache_root = remote_cache_dir
        self._remote_process_cache_dir = (
            remote_cache_dir / f"pid_{os.getpid()}"
            if remote_cache_dir is not None
            else None
        )
        self._remote_prefetch_runtime_disabled = False
        self._remote_prefetch_runtime_disable_reason: str | None = None
        self._remote_cache_dir: Path | None = None
        self._active_export_cache_token: str | None = None
        self._use_system_proxy = use_system_proxy
        self._remote_transport = remote_transport
        self._prefetched_media: dict[tuple[Any, ...], tuple[Path | None, str | None]] = {}
        self._prefetched_media_payloads: dict[tuple[Any, ...], dict[str, Any] | None] = {}
        self._prefetched_forward_media: dict[tuple[Any, ...], tuple[Path | None, str | None]] = {}
        self._prefetched_forward_media_payloads: dict[tuple[Any, ...], dict[str, Any] | None] = {}
        self._shared_media_outcomes: dict[tuple[Any, ...], tuple[Path | None, str | None]] = {}
        self._remote_media_resolution_cache: dict[tuple[str, str], str | None] = {}
        self._remote_media_resolution_futures: dict[tuple[str, str], Future[str | None]] = {}
        self._remote_media_failure_reasons: dict[str, str] = {}
        self._public_token_prefetch_cache: dict[tuple[str, ...], dict[str, Any] | None] = {}
        self._public_token_prefetch_futures: dict[
            tuple[str, ...],
            Future[dict[str, Any] | None],
        ] = {}
        self._known_bad_public_tokens: dict[tuple[str, str], str] = {}
        self._image_placeholder_missing_cache: dict[str, str | None] = {}
        self._source_local_resolution_cache: dict[str, Path | None] = {}
        self._stale_image_neighbor_cache: dict[str, Path | None] = {}
        self._hint_local_resolution_cache: dict[tuple[str, str, str], Path | None] = {}
        self._ntqq_image_candidate_index_cache: dict[str, dict[str, tuple[Path | None, bool]]] = {}
        self._public_token_action_outcomes: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._fast_capabilities: dict[str, Any] | None = None
        self._fast_capabilities_loaded = False
        self._remote_base_url = (
            remote_base_url
            or getattr(client, "_base_url", None)
            or getattr(fast_client, "_base_url", None)
        )
        self._remote_media_fetch_workers = self._compute_remote_media_fetch_workers()
        self._public_token_prefetch_workers = self._compute_public_token_prefetch_workers()
        self._prefetch_feedback_lock = Lock()
        self._prefetch_state_lock = Lock()
        self._executor_lock = Lock()
        self._download_progress_lock = Lock()
        self._transient_state_generation = 0
        self._download_progress = self._new_download_progress_state()
        self._download_operation_states: dict[tuple[str, str], str] = {}
        self._prefetch_feedback: dict[str, int] = {
            "remote_ok": 0,
            "remote_error": 0,
            "token_payload": 0,
            "token_resolved": 0,
            "token_remote_ok": 0,
            "token_remote_error": 0,
        }
        self._public_token_executor: ThreadPoolExecutor | None = None
        self._remote_loop: asyncio.AbstractEventLoop | None = None
        self._remote_loop_thread: Thread | None = None
        self._remote_async_client: httpx.AsyncClient | None = None
        self._remote_async_semaphore: asyncio.Semaphore | None = None
        self._create_prefetch_executors()

    def close(self) -> None:
        self._rebuild_prefetch_executors(wait=True, recreate=False)

    def reset_export_state(self) -> None:
        self._reset_transient_export_state()

    def cleanup_remote_cache(self) -> dict[str, Any]:
        # Export data and manifest have already been written before cleanup runs.
        # Do not let stale remote-prefetch futures hold the CLI open for tens of
        # seconds while we are only trying to rotate scratch cache state.
        self._rebuild_prefetch_executors(wait=False, recreate=True)
        self._reset_transient_export_state()
        cache_root = self._remote_cache_dir
        stats: dict[str, Any] = {
            "cache_root": str(cache_root) if cache_root is not None else None,
            "removed_files": 0,
            "removed_dirs": 0,
            "freed_bytes": 0,
            "cleared_memory_state": True,
            "cache_cleared": False,
        }
        self._remote_cache_dir = None
        self._active_export_cache_token = None
        if cache_root is None:
            stats["skipped_reason"] = "cache_disabled"
            return stats
        if not cache_root.exists():
            stats["skipped_reason"] = "cache_missing"
            return stats

        for path in cache_root.rglob("*"):
            try:
                if path.is_file():
                    stats["removed_files"] += 1
                    stats["freed_bytes"] += path.stat().st_size
                elif path.is_dir():
                    stats["removed_dirs"] += 1
            except OSError:
                continue

        try:
            shutil.rmtree(cache_root)
        except OSError as exc:
            stats["cleanup_error"] = str(exc)
            self._logger.warning(
                "media_download_cache_cleanup_failed cache_root=%s detail=%s",
                cache_root,
                exc,
            )
            return stats

        stats["cache_cleared"] = True
        return stats

    def _prepare_remote_cache_dir(self) -> Path | None:
        process_root = self._remote_process_cache_dir
        if process_root is None:
            self._remote_cache_dir = None
            self._active_export_cache_token = None
            return None
        if self._remote_cache_dir is not None and self._remote_cache_dir.exists():
            return self._remote_cache_dir
        export_token = build_timestamp_token(include_pid=True)
        cache_dir = process_root / f"export_{export_token}"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._remote_cache_dir = None
            self._active_export_cache_token = None
            return None
        self._remote_cache_dir = cache_dir
        self._active_export_cache_token = export_token
        return cache_dir

    def _reset_transient_export_state(self) -> None:
        with self._prefetch_state_lock:
            self._transient_state_generation += 1
            self._fast_context_route_disabled = False
            self._fast_forward_context_route_disabled = False
            self._prefetched_media.clear()
            self._prefetched_media_payloads.clear()
            self._prefetched_forward_media.clear()
            self._prefetched_forward_media_payloads.clear()
            self._shared_media_outcomes.clear()
            self._public_token_action_outcomes.clear()
            self._remote_media_resolution_cache.clear()
            self._remote_media_resolution_futures.clear()
            self._remote_media_failure_reasons.clear()
            self._public_token_prefetch_cache.clear()
            self._public_token_prefetch_futures.clear()
            self._known_bad_public_tokens.clear()
            self._old_context_failure_buckets.clear()
            self._old_context_skip_logged.clear()
            self._old_context_expired_buckets.clear()
            self._forward_context_timeout_cache.clear()
            self._forward_context_empty_cache.clear()
            self._forward_context_error_cache.clear()
            self._forward_context_unavailable_cache.clear()
            self._forward_context_payload_cache.clear()
            self._request_scoped_public_action_timeout_cache.clear()
            self._forward_parent_public_action_timeout_cache.clear()
            self._direct_file_id_timeout_cache.clear()
            self._forward_timeout_storm_counts.clear()
            self._forward_timeout_storm_open.clear()
            self._image_placeholder_missing_cache.clear()
            self._source_local_resolution_cache.clear()
            self._stale_image_neighbor_cache.clear()
            self._hint_local_resolution_cache.clear()
            self._ntqq_image_candidate_index_cache.clear()
        with self._download_progress_lock:
            self._download_progress = self._new_download_progress_state()
            self._download_operation_states.clear()
        with self._prefetch_feedback_lock:
            self._prefetch_feedback = {
                "remote_ok": 0,
                "remote_error": 0,
                "token_payload": 0,
                "token_resolved": 0,
                "token_remote_ok": 0,
                "token_remote_error": 0,
            }

    @staticmethod
    def _new_download_progress_state() -> dict[str, Any]:
        return {
            "candidate_total": 0,
            "eager_remote_candidates": 0,
            "public_token_candidates": 0,
            "context_candidates": 0,
            "queued": 0,
            "active": 0,
            "completed": 0,
            "failed": 0,
            "cached": 0,
            "timeout_count": 0,
            "forward_context_timeout_count": 0,
            "forward_context_empty_count": 0,
            "forward_context_error_count": 0,
            "forward_context_unavailable_count": 0,
            "forward_timeout_storm_skip_count": 0,
            "last_asset_type": None,
            "last_file_name": None,
            "last_status": None,
        }

    def export_download_progress_snapshot(self) -> dict[str, Any]:
        with self._download_progress_lock:
            return dict(self._download_progress)

    def begin_export_download_tracking(
        self,
        requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self._initialize_download_progress_for_requests(requests)
        return self.export_download_progress_snapshot()

    def settle_export_download_progress(self) -> dict[str, Any]:
        with self._download_progress_lock:
            pending = [
                cache_key
                for cache_key, state in self._download_operation_states.items()
                if state in {"queued", "active"}
            ]
            if pending:
                self._download_progress["queued"] = 0
                self._download_progress["active"] = 0
                for cache_key in pending:
                    self._download_operation_states.pop(cache_key, None)
            return dict(self._download_progress)

    def _initialize_download_progress_for_requests(
        self,
        requests: list[dict[str, Any]],
    ) -> None:
        candidate_total = 0
        eager_remote_candidates = 0
        public_token_candidates = 0
        context_candidates = 0
        for request in requests:
            asset_type = str(request.get("asset_type") or "").strip()
            if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
                continue
            hint = self._request_hint(request)
            if self._resolve_from_source_local_path(request) != (None, None):
                continue
            if self._resolve_from_hint_local_path(hint) != (None, None):
                continue
            candidate_total += 1
            if self._has_context_hint(hint):
                context_candidates += 1
            token = str(request.get("public_file_token") or hint.get("public_file_token") or "").strip()
            if token:
                public_token_candidates += 1
            remote_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
            if self._resolve_remote_url(remote_url):
                eager_remote_candidates += 1
        with self._download_progress_lock:
            self._download_progress = {
                **self._new_download_progress_state(),
                "candidate_total": candidate_total,
                "eager_remote_candidates": eager_remote_candidates,
                "public_token_candidates": public_token_candidates,
                "context_candidates": context_candidates,
            }
            self._download_operation_states.clear()

    def _update_download_progress(
        self,
        cache_key: tuple[str, str],
        *,
        asset_type: str,
        file_name: str | None,
        next_state: str,
    ) -> None:
        normalized_state = str(next_state or "").strip().lower()
        tracked_states = {"queued", "active", "completed", "failed", "cached"}
        if not normalized_state:
            return
        with self._download_progress_lock:
            previous_state = self._download_operation_states.get(cache_key)
            if previous_state != normalized_state:
                if previous_state in tracked_states:
                    previous_count = int(self._download_progress.get(previous_state) or 0)
                    if previous_count > 0:
                        self._download_progress[previous_state] = previous_count - 1
                if normalized_state in tracked_states:
                    self._download_progress[normalized_state] = (
                        int(self._download_progress.get(normalized_state) or 0) + 1
                    )
                    self._download_operation_states[cache_key] = normalized_state
                else:
                    self._download_operation_states.pop(cache_key, None)
            self._download_progress["last_asset_type"] = asset_type or None
            self._download_progress["last_file_name"] = file_name or None
            self._download_progress["last_status"] = normalized_state

    def prepare_for_export(
        self,
        requests: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if requests and not int(self.export_download_progress_snapshot().get("candidate_total") or 0):
            self._initialize_download_progress_for_requests(requests)
        if self._fast_client is None or not requests:
            return
        self._configure_prefetch_pools_for_requests(
            requests,
            progress_callback=progress_callback,
        )
        large_prefetch_run = len(requests) >= int(self.PREFETCH_LARGE_REQUEST_THRESHOLD)
        batch_items: list[tuple[dict[str, Any], dict[str, Any]]] = []
        forward_prefetch_requests: list[dict[str, Any]] = []
        seen: set[tuple[Any, ...]] = set()
        skipped_old_bucket_prefetch = 0
        prefetched_local_count = 0
        prefetched_terminal_count = 0
        prepare_started = monotonic()
        last_prepare_emit = prepare_started
        overall_request_count = len(requests)

        def _emit_prepare_progress(stage: str, scanned_request_count: int) -> None:
            if progress_callback is None:
                return
            progress_callback(
                {
                    "phase": "prefetch_media_prepare",
                    "stage": stage,
                    "overall_request_count": overall_request_count,
                    "scanned_request_count": scanned_request_count,
                    "context_request_count": len(batch_items),
                    "prefetched_local_count": prefetched_local_count,
                    "prefetched_terminal_count": prefetched_terminal_count,
                    "skipped_old_bucket_count": skipped_old_bucket_prefetch,
                    "elapsed_s": round(monotonic() - prepare_started, 4),
                }
            )

        _emit_prepare_progress("start", 0)
        for index, request in enumerate(requests, start=1):
            hint = self._request_hint(request)
            if not self._has_context_hint(hint):
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            if self._has_forward_parent_hint(hint):
                self._schedule_request_direct_public_token_prefetch(request)
                if str(request.get("asset_type") or "").strip() == "image":
                    key = self._request_key(request)
                    if key not in self._prefetched_forward_media and key not in seen:
                        seen.add(key)
                        forward_prefetch_requests.append(request)
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            old_bucket = self._old_context_bucket(
                str(request.get("asset_type") or "").strip(),
                request,
            )
            source_local = self._resolve_from_source_local_path(request)
            if source_local != (None, None):
                key = self._request_key(request)
                self._prefetched_media[key] = source_local
                self._prefetched_media_payloads[key] = None
                self._remember_shared_outcome(self._shared_request_key(request), request, source_local)
                prefetched_local_count += 1
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            stale_local = self._resolve_from_stale_local_neighbors(request)
            if stale_local != (None, None):
                key = self._request_key(request)
                self._prefetched_media[key] = stale_local
                self._prefetched_media_payloads[key] = None
                self._remember_shared_outcome(self._shared_request_key(request), request, stale_local)
                prefetched_local_count += 1
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            hinted_local = self._resolve_from_hint_local_path(hint)
            if hinted_local != (None, None):
                key = self._request_key(request)
                self._prefetched_media[key] = hinted_local
                self._prefetched_media_payloads[key] = None
                self._remember_shared_outcome(self._shared_request_key(request), request, hinted_local)
                prefetched_local_count += 1
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            terminal_missing = self._classify_terminal_missing_from_request_state(request)
            if terminal_missing is not None:
                key = self._request_key(request)
                self._prefetched_media[key] = (None, terminal_missing)
                self._prefetched_media_payloads[key] = None
                self._remember_shared_outcome(
                    self._shared_request_key(request),
                    request,
                    (None, terminal_missing),
                )
                prefetched_terminal_count += 1
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            if not self._should_skip_eager_remote_prefetch(
                request,
                old_bucket=old_bucket,
            ):
                self._schedule_request_remote_prefetch(request)
            self._schedule_request_direct_public_token_prefetch(request)
            if large_prefetch_run and old_bucket is not None:
                skipped_old_bucket_prefetch += 1
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            if self._should_skip_old_bucket(old_bucket):
                if old_bucket is not None:
                    skipped_old_bucket_prefetch += 1
                if progress_callback is not None and (
                    index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
                ):
                    _emit_prepare_progress("progress", index)
                    last_prepare_emit = monotonic()
                continue
            key = self._request_key(request)
            if key in self._prefetched_media or key in seen:
                continue
            seen.add(key)
            item = {
                "message_id_raw": str(hint["message_id_raw"]),
                "element_id": str(hint["element_id"]),
                "peer_uid": str(hint["peer_uid"]),
                "chat_type_raw": int(hint["chat_type_raw"]),
                "asset_type": str(request.get("asset_type") or "").strip() or None,
                "asset_role": str(request.get("asset_role") or "").strip() or None,
                "metadata_only": True,
            }
            batch_items.append((request, {k: v for k, v in item.items() if v not in {None, ""}}))
            if progress_callback is not None and (
                index == overall_request_count or index % 250 == 0 or monotonic() - last_prepare_emit >= 0.75
            ):
                _emit_prepare_progress("progress", index)
                last_prepare_emit = monotonic()
        if not batch_items and not forward_prefetch_requests:
            _emit_prepare_progress("done", overall_request_count)
            return
        if skipped_old_bucket_prefetch > 0:
            self._logger.info(
                "skip_batch_context_prefetch_for_old_assets count=%s overall_requests=%s",
                skipped_old_bucket_prefetch,
                len(requests),
            )
        batch_size = self._prefetch_batch_size_for_request_count(len(requests))
        chunk_count = (len(batch_items) + batch_size - 1) // batch_size
        total_request_count = len(batch_items)
        if progress_callback is not None:
            _emit_prepare_progress("done", overall_request_count)
        chunk_timeout_strikes = 0
        prefetch_started = monotonic()
        for chunk_index, start in enumerate(range(0, len(batch_items), batch_size), start=1):
            prefetch_elapsed_s = monotonic() - prefetch_started
            if prefetch_elapsed_s >= self.PREFETCH_TOTAL_BUDGET_S:
                raise RuntimeError(
                    "media prefetch degraded after exceeding total budget "
                    f"({prefetch_elapsed_s:.1f}s >= {self.PREFETCH_TOTAL_BUDGET_S:.1f}s)"
                )
            chunk = batch_items[start : start + batch_size]
            chunk_started = monotonic()
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "prefetch_media_chunk",
                        "stage": "start",
                        "chunk_index": chunk_index,
                        "chunk_count": chunk_count,
                        "request_count": len(chunk),
                        "total_request_count": total_request_count,
                        "overall_request_count": overall_request_count,
                        "processed_request_count": start,
                        "request_offset": start,
                    }
                )
            try:
                payload = self._fast_client.hydrate_media_batch(
                    [item for _request, item in chunk],
                    timeout=self._prefetch_batch_timeout_s(len(chunk), len(requests)),
                )
            except NapCatFastHistoryTimeoutError as exc:
                chunk_timeout_strikes += 1
                timeout_s = self._prefetch_batch_timeout_s(len(chunk), len(requests))
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "prefetch_media_chunk",
                            "stage": "error",
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "request_count": len(chunk),
                            "total_request_count": total_request_count,
                            "overall_request_count": overall_request_count,
                            "processed_request_count": min(start + len(chunk), total_request_count),
                            "request_offset": start,
                            "elapsed_s": round(monotonic() - chunk_started, 4),
                            "error": str(exc),
                            "reason": "chunk_timeout",
                            "timeout_s": timeout_s,
                        }
                    )
                if chunk_timeout_strikes >= int(self.PREFETCH_BATCH_TIMEOUT_STRIKE_LIMIT):
                    raise RuntimeError(
                        "media prefetch degraded after repeated batch hydrate timeouts "
                        f"(timeout={timeout_s:.1f}s strikes={chunk_timeout_strikes})"
                    ) from exc
                continue
            except NapCatFastHistoryUnavailable as exc:
                self._fast_context_route_disabled = True
                self._logger.info(
                    "fast_context_hydration_unavailable during batch prefetch; disabling /hydrate-media for this process. detail=%s",
                    exc,
                )
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "prefetch_media_chunk",
                            "stage": "error",
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "request_count": len(chunk),
                            "total_request_count": total_request_count,
                            "overall_request_count": overall_request_count,
                            "processed_request_count": min(start + len(chunk), total_request_count),
                            "request_offset": start,
                            "elapsed_s": round(monotonic() - chunk_started, 4),
                            "error": str(exc),
                            "reason": "route_unavailable",
                        }
                    )
                return
            except Exception as exc:
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "prefetch_media_chunk",
                            "stage": "error",
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "request_count": len(chunk),
                            "total_request_count": total_request_count,
                            "overall_request_count": overall_request_count,
                            "processed_request_count": min(start + len(chunk), total_request_count),
                            "request_offset": start,
                            "elapsed_s": round(monotonic() - chunk_started, 4),
                            "error": str(exc),
                            "reason": "chunk_failed",
                        }
                    )
                continue
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                if progress_callback is not None:
                    progress_callback(
                        {
                            "phase": "prefetch_media_chunk",
                            "stage": "error",
                            "chunk_index": chunk_index,
                            "chunk_count": chunk_count,
                            "request_count": len(chunk),
                            "total_request_count": total_request_count,
                            "overall_request_count": overall_request_count,
                            "processed_request_count": min(start + len(chunk), total_request_count),
                            "request_offset": start,
                            "elapsed_s": round(monotonic() - chunk_started, 4),
                            "reason": "invalid_response",
                        }
                    )
                continue
            hydrated_count = 0
            for idx, (request, batch_request) in enumerate(chunk):
                request_key = self._request_key(request)
                batch_key = self._batch_request_key(batch_request)
                result = items[idx] if idx < len(items) else None
                if not isinstance(result, dict):
                    for key in {request_key, batch_key}:
                        self._prefetched_media[key] = (None, None)
                        self._prefetched_media_payloads[key] = None
                    continue
                if result.get("ok") is False:
                    for key in {request_key, batch_key}:
                        self._prefetched_media[key] = (None, None)
                        self._prefetched_media_payloads[key] = None
                    continue
                data = result.get("data") if isinstance(result.get("data"), dict) else result
                resolved, resolver = self._resolve_from_fast_payload(data)
                for key in {request_key, batch_key}:
                    self._prefetched_media[key] = (resolved, resolver)
                    self._prefetched_media_payloads[key] = data if isinstance(data, dict) else None
                if isinstance(data, dict):
                    self._schedule_remote_media_prefetch(
                        request=request,
                        request_data=request,
                        payload=data,
                    )
                    self._schedule_public_token_prefetch(
                        request=request,
                        request_data=request,
                        payload=data,
                    )
                hydrated_count += 1
            if progress_callback is not None:
                chunk_elapsed_s = round(monotonic() - chunk_started, 4)
                progress_callback(
                    {
                        "phase": "prefetch_media_chunk",
                        "stage": "done",
                        "chunk_index": chunk_index,
                        "chunk_count": chunk_count,
                        "request_count": len(chunk),
                        "total_request_count": total_request_count,
                        "overall_request_count": overall_request_count,
                        "processed_request_count": min(start + len(chunk), total_request_count),
                        "request_offset": start,
                        "hydrated_count": hydrated_count,
                        "elapsed_s": chunk_elapsed_s,
                        "slow": chunk_elapsed_s >= self.PREFETCH_SLOW_CHUNK_WARN_S,
                        "slow_threshold_s": self.PREFETCH_SLOW_CHUNK_WARN_S,
                    }
                )
        if forward_prefetch_requests:
            self._prefetch_forward_metadata_requests(
                forward_prefetch_requests,
                progress_callback=progress_callback,
            )

    def _prefetch_batch_size_for_request_count(self, request_count: int) -> int:
        if request_count >= int(self.PREFETCH_LARGE_REQUEST_THRESHOLD):
            return max(1, min(int(self.PREFETCH_BATCH_SIZE), int(self.PREFETCH_LARGE_BATCH_SIZE)))
        return max(1, int(self.PREFETCH_BATCH_SIZE))

    def _prefetch_batch_timeout_s(self, chunk_size: int, request_count: int) -> float:
        _ = chunk_size, request_count
        return float(self.PREFETCH_BATCH_TIMEOUT_S)

    def _prefetch_forward_metadata_request_payload(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        hint = self._request_hint(request)
        parent = hint.get("_forward_parent")
        if not isinstance(parent, dict) or not self._has_context_hint(parent):
            return {"status": "skip"}
        parent_element_id = str(parent.get("element_id") or "").strip()
        if not parent_element_id:
            return {"status": "skip"}
        parent_scoped_metadata = (
            self._should_use_parent_scoped_forward_metadata(request)
            and self._fast_plugin_supports_forward_parent_metadata()
        )
        timeout_s = self._forward_context_timeout_s(request, materialize=False)
        started = monotonic()
        try:
            payload = self._fast_client.hydrate_forward_media(  # type: ignore[union-attr]
                message_id_raw=str(parent["message_id_raw"]),
                element_id=parent_element_id,
                peer_uid=str(parent["peer_uid"]),
                chat_type_raw=int(parent["chat_type_raw"]),
                asset_type=(
                    None if parent_scoped_metadata else str(request.get("asset_type") or "").strip() or None
                ),
                asset_role=(
                    None if parent_scoped_metadata else str(request.get("asset_role") or "").strip() or None
                ),
                file_name=(
                    None if parent_scoped_metadata else str(request.get("file_name") or "").strip() or None
                ),
                md5=(
                    None if parent_scoped_metadata else str(request.get("md5") or "").strip() or None
                ),
                file_id=(
                    None if parent_scoped_metadata else str(hint.get("file_id") or "").strip() or None
                ),
                url=(
                    None
                    if parent_scoped_metadata
                    else str(hint.get("remote_url") or hint.get("url") or "").strip() or None
                ),
                materialize=False,
                timeout=timeout_s,
            )
        except NapCatFastHistoryUnavailable as exc:
            return {
                "status": "unavailable",
                "timeout_s": timeout_s,
                "elapsed_s": monotonic() - started,
                "detail": str(exc),
            }
        except NapCatFastHistoryTimeoutError as exc:
            return {
                "status": "timeout",
                "timeout_s": timeout_s,
                "elapsed_s": monotonic() - started,
                "detail": str(exc),
            }
        except Exception as exc:
            return {
                "status": "error",
                "timeout_s": timeout_s,
                "elapsed_s": monotonic() - started,
                "detail": str(exc),
            }
        return {
            "status": "ok",
            "timeout_s": timeout_s,
            "elapsed_s": monotonic() - started,
            "payload": payload if isinstance(payload, dict) else None,
        }

    def _remember_forward_prefetch_result(
        self,
        request: dict[str, Any],
        outcome: dict[str, Any],
    ) -> None:
        key = self._request_key(request)
        timeout_cache_key = self._forward_context_timeout_key(request, materialize=False)
        status = str(outcome.get("status") or "").strip().lower()
        if status in {"skip", ""}:
            return
        if status == "unavailable":
            self._fast_forward_context_route_disabled = True
            if timeout_cache_key is not None:
                self._forward_context_unavailable_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return
        if status == "timeout":
            if timeout_cache_key is not None:
                self._forward_context_timeout_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            self._note_forward_timeout_storm(request, route="forward_context_metadata")
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return
        if status == "error":
            if timeout_cache_key is not None:
                self._forward_context_error_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return
        payload = outcome.get("payload")
        if not isinstance(payload, dict):
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return
        if timeout_cache_key is not None:
            self._forward_context_timeout_cache.discard(timeout_cache_key)
            self._forward_context_empty_cache.discard(timeout_cache_key)
            self._forward_context_error_cache.discard(timeout_cache_key)
            self._forward_context_unavailable_cache.discard(timeout_cache_key)
            if self._is_forward_context_payload_cacheable(payload):
                self._forward_context_payload_cache[timeout_cache_key] = payload
            else:
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
        assets = payload.get("assets")
        assets_list = assets if isinstance(assets, list) else []
        if not assets_list:
            if timeout_cache_key is not None:
                self._forward_context_empty_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return
        matched, matched_payload = self._pick_forward_asset_match(request, assets_list)
        if isinstance(matched_payload, dict):
            enriched_payload = dict(matched_payload)
            enriched_payload["_forward_targeted_mode"] = str(payload.get("targeted_mode") or "").strip()
            matched_payload = enriched_payload
            self._schedule_remote_media_prefetch(
                request=request,
                request_data=request,
                payload=matched_payload,
            )
            self._schedule_public_token_prefetch(
                request=request,
                request_data=request,
                payload=matched_payload,
            )
        self._note_forward_timeout_storm_success(request, route="forward_context_metadata")
        self._prefetched_forward_media[key] = matched
        self._prefetched_forward_media_payloads[key] = matched_payload

    def _prefetch_forward_metadata_requests(
        self,
        requests: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if self._fast_client is None or not requests:
            return
        grouped_requests: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for request in requests:
            group_key = self._forward_metadata_prefetch_group_key(request)
            grouped_requests.setdefault(group_key, []).append(request)
        grouped_items = list(grouped_requests.items())
        worker_count = max(
            2,
            min(int(self.FORWARD_METADATA_PREFETCH_WORKERS), len(grouped_items)),
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "prefetch_forward_metadata",
                    "stage": "start",
                    "request_count": len(requests),
                    "group_count": len(grouped_items),
                    "worker_count": worker_count,
                }
            )
        started = monotonic()
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="forward-metadata-prefetch",
        ) as executor:
            future_map = {
                executor.submit(
                    self._prefetch_forward_metadata_request_payload,
                    request_group[1][0],
                ): request_group
                for request_group in grouped_items
            }
            processed = 0
            for future in as_completed(future_map):
                request_group = future_map[future]
                try:
                    outcome = future.result()
                except Exception as exc:  # pragma: no cover - defensive
                    outcome = {"status": "error", "detail": str(exc)}
                for request in request_group[1]:
                    self._remember_forward_prefetch_result(request, outcome)
                processed += len(request_group[1])
                if progress_callback is not None and (
                    processed == len(requests) or processed % 25 == 0
                ):
                    progress_callback(
                        {
                            "phase": "prefetch_forward_metadata",
                            "stage": "progress",
                            "request_count": len(requests),
                            "group_count": len(grouped_items),
                            "processed_request_count": processed,
                            "elapsed_s": round(monotonic() - started, 4),
                        }
                    )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "prefetch_forward_metadata",
                    "stage": "done",
                    "request_count": len(requests),
                    "group_count": len(grouped_items),
                    "processed_request_count": len(requests),
                    "elapsed_s": round(monotonic() - started, 4),
                }
            )

    def _forward_metadata_prefetch_group_key(
        self,
        request: dict[str, Any],
    ) -> tuple[Any, ...]:
        hint = self._request_hint(request)
        parent = hint.get("_forward_parent")
        if (
            isinstance(parent, dict)
            and self._should_use_parent_scoped_forward_metadata(request)
            and self._fast_plugin_supports_forward_parent_metadata()
        ):
            return (
                "parent_scoped_forward_metadata",
                str(parent.get("message_id_raw") or "").strip(),
                str(parent.get("element_id") or "").strip(),
                str(parent.get("peer_uid") or "").strip(),
                str(parent.get("chat_type_raw") or "").strip(),
            )
        return ("request_scoped_forward_metadata", self._request_key(request))

    def resolve_for_export(
        self,
        request: dict[str, Any],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None]:
        hint = self._request_hint(request)
        asset_type = str(request.get("asset_type") or "").strip()
        asset_role = str(request.get("asset_role") or "").strip() or None
        file_name = str(request.get("file_name") or "").strip() or None
        shared_key = self._shared_request_key(request)
        old_bucket = self._old_context_bucket(asset_type, request)
        if shared_key is not None:
            shared = self._shared_media_outcomes.get(shared_key)
            if shared is not None:
                return shared
        source_local = self._resolve_from_source_local_path(request)
        if source_local != (None, None):
            return self._remember_shared_outcome(shared_key, request, source_local)
        stale_local_fallback = self._resolve_from_stale_local_neighbors(request)
        if stale_local_fallback != (None, None):
            return self._remember_shared_outcome(shared_key, request, stale_local_fallback)
        hinted_local = self._resolve_from_hint_local_path(hint)
        if hinted_local != (None, None):
            return self._remember_shared_outcome(shared_key, request, hinted_local)
        key = self._request_key(request)
        prefetched = self._prefetched_media.get(key)
        if prefetched is not None:
            payload = self._prefetched_media_payloads.get(key)
            if prefetched != (None, None):
                self._note_old_bucket_success(old_bucket)
                return self._remember_shared_outcome(shared_key, request, prefetched)
            if isinstance(payload, dict):
                if not self._has_forward_parent_hint(hint):
                    direct_prefetched_token_resolved = self._resolve_via_direct_public_token_hint(
                        request,
                        trace_callback=trace_callback,
                    )
                    if direct_prefetched_token_resolved not in {None, (None, None)}:
                        resolved_path, resolver = direct_prefetched_token_resolved
                        if resolved_path is not None:
                            self._note_old_bucket_success(old_bucket)
                        elif resolver == "qq_expired_after_napcat":
                            self._note_old_bucket_expired_like(old_bucket)
                        return self._remember_shared_outcome(
                            shared_key,
                            request,
                            direct_prefetched_token_resolved,
                        )
                fast_resolved = self._resolve_from_fast_payload(payload)
                if fast_resolved != (None, None):
                    self._note_old_bucket_success(old_bucket)
                    return self._remember_shared_outcome(shared_key, request, fast_resolved)
                if self._should_skip_old_bucket(old_bucket):
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, self._missing_bucket_resolver(old_bucket)),
                    )
                public_resolved = self._resolve_from_public_token(
                    payload,
                    old_bucket=old_bucket,
                    expired_candidate=self._should_share_missing_outcome(request),
                    request=request,
                    trace_callback=trace_callback,
                )
                if public_resolved not in {None, (None, None)}:
                    resolved_path, resolver = public_resolved
                    if resolved_path is not None:
                        self._note_old_bucket_success(old_bucket)
                    elif resolver == "qq_expired_after_napcat":
                        self._note_old_bucket_expired_like(old_bucket)
                    return self._remember_shared_outcome(shared_key, request, public_resolved)
                classified_missing = self._classify_missing_from_payload(
                    payload,
                    old_bucket=old_bucket,
                    expired_candidate=self._should_share_missing_outcome(request),
                    request=request,
                )
                if classified_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="prefetched_context_missing_classification",
                        classification=classified_missing,
                    )
                    self._note_old_bucket_expired_like(old_bucket)
                    return self._remember_shared_outcome(shared_key, request, (None, classified_missing))
                fresh_retry = self._resolve_via_fresh_public_retry(
                    request,
                    trace_callback=trace_callback,
                )
                if fresh_retry not in {None, (None, None)}:
                    resolved_path, resolver = fresh_retry
                    if resolved_path is not None:
                        self._note_old_bucket_success(old_bucket)
                    elif resolver == "qq_expired_after_napcat":
                        self._note_old_bucket_expired_like(old_bucket)
                    return self._remember_shared_outcome(shared_key, request, fresh_retry)
                self._note_old_bucket_failure(old_bucket)
            if asset_type == "sticker":
                sticker_remote = self._resolve_from_sticker_remote_url(
                    hint,
                    asset_role=asset_role,
                    file_name=file_name,
                )
                if sticker_remote != (None, None):
                    return self._remember_shared_outcome(shared_key, request, sticker_remote)
            return self._remember_shared_outcome(shared_key, request, prefetched)
        if self._has_forward_parent_marker(hint):
            request_forward_url = self._resolve_from_forward_remote_url(
                {
                    "asset_type": asset_type,
                    "file_name": file_name,
                    "remote_url": hint.get("remote_url"),
                    "url": hint.get("url"),
                },
                request=request,
                trace_callback=trace_callback,
            )
            if request_forward_url != (None, None):
                return self._remember_shared_outcome(shared_key, request, request_forward_url)
        if self._has_forward_parent_hint(hint):
            forward_payload = self._prefetched_forward_media_payloads.get(key)
            if isinstance(forward_payload, dict):
                resolved_from_forward_payload = self._resolve_from_forward_payload_candidate(
                    request,
                    payload=forward_payload,
                    trace_callback=trace_callback,
                )
                if resolved_from_forward_payload not in {None, (None, None)}:
                    return self._remember_shared_outcome(shared_key, request, resolved_from_forward_payload)
            if asset_type == "image":
                classified_forward_missing = self._classify_forward_missing(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                )
                if classified_forward_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="forward_missing_classification",
                        classification=classified_forward_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, classified_forward_missing),
                    )
            classified_old_forward_missing = self._classify_old_forward_expensive_missing(
                request,
                payload=forward_payload,
                require_timeout_signal=True,
            )
            if classified_old_forward_missing is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep="forward_missing_classification",
                    classification=classified_old_forward_missing,
                )
                return self._remember_shared_outcome(
                    shared_key,
                    request,
                    (None, classified_old_forward_missing),
                )
            if asset_type == "image":
                has_forward_payload = isinstance(forward_payload, dict)
                has_hint_file_id = bool(str(hint.get("file_id") or "").strip())
                if not has_forward_payload and not has_hint_file_id:
                    classified_forward_missing = self._classify_forward_missing(
                        request,
                        payload=forward_payload if isinstance(forward_payload, dict) else None,
                    )
                    if classified_forward_missing is not None:
                        self._emit_missing_classification_trace(
                            trace_callback,
                            request,
                            substep="forward_missing_classification",
                            classification=classified_forward_missing,
                        )
                        return self._remember_shared_outcome(shared_key, request, (None, classified_forward_missing))
            passive_forward_resolved = self._download_via_forward_context(
                request,
                materialize=False,
                trace_callback=trace_callback,
            )
            if passive_forward_resolved not in {None, (None, None)}:
                return self._remember_shared_outcome(shared_key, request, passive_forward_resolved)
            forward_payload = self._prefetched_forward_media_payloads.get(key)
            if isinstance(forward_payload, dict):
                resolved_from_forward_payload = self._resolve_from_forward_payload_candidate(
                    request,
                    payload=forward_payload,
                    trace_callback=trace_callback,
                )
                if resolved_from_forward_payload not in {None, (None, None)}:
                    return self._remember_shared_outcome(shared_key, request, resolved_from_forward_payload)
            classified_old_forward_missing = self._classify_old_forward_expensive_missing(
                request,
                payload=forward_payload if isinstance(forward_payload, dict) else None,
                require_timeout_signal=True,
            )
            if classified_old_forward_missing is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep="forward_missing_classification",
                    classification=classified_old_forward_missing,
                )
                return self._remember_shared_outcome(
                    shared_key,
                    request,
                    (None, classified_old_forward_missing),
                )
            if asset_type in {"video", "file", "speech"} and not self._has_direct_forward_file_identifier(
                request,
                payload=forward_payload if isinstance(forward_payload, dict) else None,
            ):
                classified_old_forward_missing = self._classify_old_forward_expensive_missing(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                    failure_signal_mode="terminal",
                )
                if classified_old_forward_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="forward_missing_classification",
                        classification=classified_old_forward_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, classified_old_forward_missing),
                    )
            direct_public_token_resolved = self._resolve_via_direct_public_token_hint(
                request,
                trace_callback=trace_callback,
            )
            if direct_public_token_resolved not in {None, (None, None)}:
                return self._remember_shared_outcome(
                    shared_key,
                    request,
                    direct_public_token_resolved,
                )
            if asset_type in {"video", "file", "speech"}:
                classified_old_forward_missing = self._classify_old_forward_expensive_missing(
                    request,
                    payload=forward_payload
                    if isinstance(forward_payload, dict)
                    else self._direct_public_token_payload_for_request(request),
                    failure_signal_mode="strict",
                )
                if classified_old_forward_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="forward_missing_classification",
                        classification=classified_old_forward_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, classified_old_forward_missing),
                    )
            attempted_direct_forward_file_id = False
            if asset_type in {"video", "file"} and self._should_prefer_direct_file_id_before_targeted_materialize(
                request,
                payload=forward_payload if isinstance(forward_payload, dict) else None,
            ):
                attempted_direct_forward_file_id = True
                direct_forward_file_id = self._resolve_via_direct_file_id(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                    trace_callback=trace_callback,
                )
                if direct_forward_file_id not in {None, (None, None)}:
                    return self._remember_shared_outcome(shared_key, request, direct_forward_file_id)
                classified_old_forward_missing = self._classify_old_forward_expensive_missing(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                    failure_signal_mode="terminal",
                )
                if classified_old_forward_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="forward_missing_classification",
                        classification=classified_old_forward_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, classified_old_forward_missing),
                    )
            if asset_type in {"video", "file", "speech"}:
                targeted_forward_download = self._download_via_forward_context(
                    request,
                    materialize=True,
                    trace_callback=trace_callback,
                )
                if targeted_forward_download not in {None, (None, None)}:
                    return self._remember_shared_outcome(shared_key, request, targeted_forward_download)
                forward_payload = self._prefetched_forward_media_payloads.get(key)
                classified_old_forward_missing = self._classify_old_forward_expensive_missing(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                    failure_signal_mode="terminal",
                )
                if classified_old_forward_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="forward_missing_classification",
                        classification=classified_old_forward_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, classified_old_forward_missing),
                    )
            if not attempted_direct_forward_file_id:
                direct_forward_file_id = self._resolve_via_direct_file_id(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                    trace_callback=trace_callback,
                )
                if direct_forward_file_id not in {None, (None, None)}:
                    return self._remember_shared_outcome(shared_key, request, direct_forward_file_id)
            if asset_type == "image":
                classified_forward_missing = self._classify_forward_missing(
                    request,
                    payload=forward_payload if isinstance(forward_payload, dict) else None,
                )
                if classified_forward_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="forward_missing_classification",
                        classification=classified_forward_missing,
                    )
                    return self._remember_shared_outcome(shared_key, request, (None, classified_forward_missing))
        context_resolved = None
        direct_public_token_resolved = self._resolve_via_direct_public_token_hint(
            request,
            trace_callback=trace_callback,
        )
        if direct_public_token_resolved not in {None, (None, None)}:
            return self._remember_shared_outcome(shared_key, request, direct_public_token_resolved)
        if not self._has_forward_parent_hint(hint):
            attempted_top_level_direct_file_id = False
            if asset_type == "image":
                top_level_image_missing = self._classify_terminal_top_level_image_missing_from_request_state(
                    request,
                )
                if top_level_image_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="top_level_missing_classification",
                        classification=top_level_image_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, top_level_image_missing),
                    )
                context_resolved = self._resolve_via_context_only(
                    request,
                    trace_callback=trace_callback,
                )
            elif asset_type in {"file", "video"}:
                attempted_top_level_direct_file_id = True
                direct_file_id_resolved = self._resolve_via_direct_file_id(
                    request,
                    trace_callback=trace_callback,
                )
                if direct_file_id_resolved not in {None, (None, None)}:
                    return self._remember_shared_outcome(shared_key, request, direct_file_id_resolved)
                top_level_file_like_missing = self._classify_terminal_file_like_missing_from_request_state(
                    request,
                )
                if top_level_file_like_missing is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="top_level_missing_classification",
                        classification=top_level_file_like_missing,
                    )
                    return self._remember_shared_outcome(
                        shared_key,
                        request,
                        (None, top_level_file_like_missing),
                    )
                context_resolved = self._resolve_via_context_only(
                    request,
                    trace_callback=trace_callback,
                )
            else:
                context_resolved = self._resolve_via_context_only(
                    request,
                    trace_callback=trace_callback,
                )
        if context_resolved not in {None, (None, None)}:
            return self._remember_shared_outcome(shared_key, request, context_resolved)
        direct_file_id_resolved = None
        if not self._has_forward_parent_hint(hint) and not (
            str(request.get("asset_type") or "").strip() in {"file", "video"}
        ):
            direct_file_id_resolved = self._resolve_via_direct_file_id(
                request,
                trace_callback=trace_callback,
            )
        if direct_file_id_resolved not in {None, (None, None)}:
            return self._remember_shared_outcome(shared_key, request, direct_file_id_resolved)
        top_level_file_like_missing = self._classify_terminal_file_like_missing_from_request_state(
            request,
        )
        if top_level_file_like_missing is not None:
            self._emit_missing_classification_trace(
                trace_callback,
                request,
                substep="top_level_missing_classification",
                classification=top_level_file_like_missing,
            )
            return self._remember_shared_outcome(
                shared_key,
                request,
                (None, top_level_file_like_missing),
            )
        if asset_type == "image" and not self._has_forward_parent_hint(hint):
            fresh_public_retry = self._resolve_via_fresh_public_retry(
                request,
                trace_callback=trace_callback,
            )
            if fresh_public_retry not in {None, (None, None)}:
                resolved_path, resolver = fresh_public_retry
                if resolved_path is not None:
                    self._note_old_bucket_success(old_bucket)
                elif resolver == "qq_expired_after_napcat":
                    self._note_old_bucket_expired_like(old_bucket)
                return self._remember_shared_outcome(shared_key, request, fresh_public_retry)
        if asset_type == "sticker":
            sticker_remote = self._resolve_from_sticker_remote_url(
                hint,
                asset_role=asset_role,
                file_name=file_name,
            )
            if sticker_remote != (None, None):
                return self._remember_shared_outcome(shared_key, request, sticker_remote)
        return self._remember_shared_outcome(
            shared_key,
            request,
            context_resolved if context_resolved is not None else (None, None),
        )

    def download_for_export(self, request: dict[str, Any]) -> Path | None:
        asset_type = str(request.get("asset_type") or "").strip()
        asset_role = str(request.get("asset_role") or "").strip() or None
        file_name = str(request.get("file_name") or "").strip()
        hint = request.get("download_hint")
        if not isinstance(hint, dict):
            hint = {}
        file_id = str(hint.get("file_id") or "").strip()

        if asset_type not in {"image", "file", "speech", "video", "sticker"}:
            return None
        if not file_id and not file_name:
            if not self._has_context_hint(hint):
                return None

        if asset_type == "sticker":
            result = self._download_remote_sticker(hint, asset_role=asset_role, file_name=file_name or None)
            if result is None:
                return None
            path = Path(result)
            if not path.exists() or not path.is_file():
                return None
            return path.resolve()

        attempted_context_hydration = False
        old_bucket = self._old_context_bucket(asset_type, request)
        skip_old_context = (
            old_bucket is not None
            and self._old_context_failure_buckets.get(old_bucket, 0)
            >= self.OLD_CONTEXT_BUCKET_FAILURE_LIMIT
        )
        if skip_old_context:
            attempted_context_hydration = True
            result = None
            if old_bucket not in self._old_context_skip_logged:
                self._old_context_skip_logged.add(old_bucket)
                self._logger.info(
                    "skip_context_hydration_for_old_assets bucket=%s failures=%s",
                    "/".join(old_bucket),
                    self._old_context_failure_buckets.get(old_bucket, 0),
                )
        else:
            context_payload = self._download_via_context(hint, asset_type=asset_type, asset_role=asset_role)
            if context_payload is not None:
                public_resolved = self._resolve_from_public_token(context_payload)
                context_resolved, _context_resolver = public_resolved or self._resolve_from_fast_payload(context_payload)
                result = str(context_resolved) if context_resolved is not None else None
                attempted_context_hydration = True
                if old_bucket is not None:
                    self._old_context_failure_buckets.pop(old_bucket, None)
                    self._old_context_skip_logged.discard(old_bucket)
            else:
                result = None
                attempted_context_hydration = self._has_context_hint(hint)
                if attempted_context_hydration and old_bucket is not None:
                    self._old_context_failure_buckets[old_bucket] = self._old_context_failure_buckets.get(old_bucket, 0) + 1

        if result is None and not attempted_context_hydration:
            result = self._download(asset_type, file_id=file_id or None, file_name=file_name or None)
        if result is None:
            result = self._download_remote_media(
                asset_type=asset_type,
                file_name=file_name or None,
                hint=hint,
            )
        if result is None:
            return None
        path = Path(result)
        if not path.exists() or not path.is_file():
            return None
        return path.resolve()

    def resolve_via_context_route(
        self,
        request: dict[str, Any],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None]:
        payload = self._context_payload_for_request(request)
        if payload is None:
            return None, None
        return self._resolve_from_fast_payload(payload)

    def resolve_via_public_token_route(
        self,
        request: dict[str, Any],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None]:
        payload = self._context_payload_for_request(request)
        if payload is None:
            return None, None
        resolved = self._resolve_from_public_token(
            payload,
            old_bucket=self._old_context_bucket(str(request.get("asset_type") or "").strip(), request),
            request=request,
            trace_callback=trace_callback,
        )
        if resolved in {None, (None, None)}:
            return None, None
        return resolved

    def should_attempt_second_pass_public_retry(
        self,
        request: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(request, dict):
            return True
        terminal_missing = self._classify_terminal_missing_from_request_state(request)
        if terminal_missing is not None:
            return False
        direct_payload = self._direct_public_token_payload_for_request(request)
        if direct_payload is None:
            return True
        old_bucket = self._old_context_bucket(
            str(request.get("asset_type") or "").strip(),
            request,
        )
        terminal_public_missing = self._classify_missing_from_public_payload(
            direct_payload,
            old_bucket=old_bucket,
            request=request,
        )
        if terminal_public_missing is not None:
            return False
        action = str(direct_payload.get("public_action") or "").strip().lower()
        token = str(direct_payload.get("public_file_token") or "").strip()
        if not action or not token:
            return True
        cache_key = self._public_token_prefetch_key(
            request_data=request,
            action=action,
            token=token,
        )
        cached_result, future = self._public_token_prefetch_state(cache_key)
        if cached_result is not ...:
            return False
        return future is not None

    def _emit_asset_substep_trace(
        self,
        trace_callback: Callable[[dict[str, Any]], None] | None,
        request: dict[str, Any],
        *,
        stage: str,
        substep: str,
        timeout_s: float | None = None,
        status: str | None = None,
        elapsed_s: float | None = None,
        detail: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        if trace_callback is None:
            return
        hint = self._request_hint(request)
        forward_parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
        payload: dict[str, Any] = {
            "phase": "materialize_asset_substep",
            "stage": stage,
            "substep": substep,
            "asset_type": str(request.get("asset_type") or "").strip(),
            "asset_role": str(request.get("asset_role") or "").strip() or None,
            "file_name": str(request.get("file_name") or "").strip() or None,
            "message_id_raw": hint.get("message_id_raw"),
            "element_id": hint.get("element_id"),
            "hint_file_id": hint.get("file_id"),
            "hint_url": hint.get("url"),
            "forward_parent_message_id_raw": forward_parent.get("message_id_raw"),
            "forward_parent_element_id": forward_parent.get("element_id"),
        }
        payload.update(self._asset_trace_context(request))
        if timeout_s is not None:
            payload["timeout_s"] = timeout_s
            payload["timeout_ms"] = int(round(timeout_s * 1000))
        if status:
            payload["status"] = status
        if elapsed_s is not None:
            payload["elapsed_s"] = round(elapsed_s, 4)
            payload["elapsed_ms"] = int(round(elapsed_s * 1000))
        if detail:
            payload["detail"] = detail
        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if value is None or value == "" or value == [] or value == {}:
                    continue
                payload[key] = value
        trace_callback(payload)

    def _emit_missing_classification_trace(
        self,
        trace_callback: Callable[[dict[str, Any]], None] | None,
        request: dict[str, Any],
        *,
        substep: str,
        classification: str,
        detail: str | None = None,
    ) -> None:
        self._emit_asset_substep_trace(
            trace_callback,
            request,
            stage="done",
            substep=substep,
            status="classified_missing",
            detail=classification if detail is None else f"{classification}: {detail}",
        )

    def _log_remote_substep_outcome(
        self,
        *,
        request: dict[str, Any],
        substep: str,
        status: str,
        timeout_s: float | None = None,
        elapsed_s: float | None = None,
        detail: str | None = None,
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        self._note_remote_substep_progress(substep=substep, status=status)
        asset_type = str(request.get("asset_type") or "").strip()
        file_name = str(request.get("file_name") or "").strip() or "-"
        hint = self._request_hint(request)
        forward_parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
        fields = [
            f"substep={substep}",
            f"status={status}",
            f"asset_type={asset_type or '-'}",
            f"file_name={file_name}",
        ]
        if timeout_s is not None:
            fields.append(f"timeout_s={timeout_s:.1f}")
        if elapsed_s is not None:
            fields.append(f"elapsed_s={elapsed_s:.3f}")
        if hint.get("message_id_raw"):
            fields.append(f"message_id_raw={hint.get('message_id_raw')}")
        if hint.get("element_id"):
            fields.append(f"element_id={hint.get('element_id')}")
        if forward_parent.get("message_id_raw"):
            fields.append(f"forward_parent_message_id_raw={forward_parent.get('message_id_raw')}")
        for key, value in self._asset_trace_context(request).items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            fields.append(f"{key}={value}")
        if detail:
            fields.append(f"detail={detail}")
        if isinstance(extra_fields, dict):
            for key, value in extra_fields.items():
                if value is None or value == "" or value == [] or value == {}:
                    continue
                fields.append(f"{key}={value}")
        message = "media_resolution_substep " + " ".join(fields)
        if status == "timeout":
            self._logger.warning(message)
            return
        if elapsed_s is not None and elapsed_s >= self.SLOW_REMOTE_SUBSTEP_WARN_S:
            self._logger.info(message)

    def _asset_trace_context(self, request: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(request, dict):
            return {}
        hint = self._request_hint(request)
        payload: dict[str, Any] = {}
        raw_timestamp = request.get("timestamp_ms")
        if isinstance(raw_timestamp, (int, float)):
            payload["timestamp_ms"] = int(raw_timestamp)
            try:
                payload["timestamp_iso"] = datetime.fromtimestamp(
                    float(raw_timestamp) / 1000.0,
                    tz=timezone.utc,
                ).astimezone(timezone(timedelta(hours=8))).isoformat()
            except (OverflowError, OSError, ValueError):
                pass
        md5 = str(request.get("md5") or "").strip().lower()
        if md5:
            payload["md5"] = md5
        source_path = str(request.get("source_path") or "").strip()
        if source_path:
            payload["source_path"] = source_path
            payload["source_path_kind"] = self._asset_location_kind(source_path)
        hint_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
        if hint_url:
            payload["hint_url_kind"] = self._asset_location_kind(hint_url)
            payload["hint_url_host"] = self._asset_location_host(hint_url)
        return payload

    def _asset_location_kind(self, value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        resolved_url = self._resolve_remote_url(text)
        if resolved_url:
            parsed = urlparse(resolved_url)
            host = parsed.netloc.lower()
            if host in {"127.0.0.1:3000", "localhost:3000", "127.0.0.1:6099", "localhost:6099"}:
                return "napcat_local_download"
            if host.endswith("multimedia.nt.qq.com.cn"):
                return "qq_multimedia"
            return f"{parsed.scheme.lower()}_url"
        if self._looks_like_zero_byte_local_media_path(text):
            return "zero_byte_local"
        if self._looks_like_stale_local_media_path(text):
            return "stale_local"
        path = Path(text)
        if path.exists() and path.is_file():
            try:
                if path.stat().st_size > 0:
                    return "local_file"
            except OSError:
                return "local_path"
            return "zero_byte_local"
        return "missing_local"

    @staticmethod
    def _asset_location_host(value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc:
            return parsed.netloc.lower()
        return None

    def _note_remote_substep_progress(
        self,
        *,
        substep: str,
        status: str,
    ) -> None:
        normalized_substep = str(substep or "").strip().lower()
        normalized_status = str(status or "").strip().lower()
        if not normalized_substep or not normalized_status:
            return
        with self._download_progress_lock:
            if normalized_status == "timeout":
                self._download_progress["timeout_count"] = (
                    int(self._download_progress.get("timeout_count") or 0) + 1
                )
                if normalized_substep == "forward_context_metadata":
                    self._download_progress["forward_context_timeout_count"] = (
                        int(self._download_progress.get("forward_context_timeout_count") or 0) + 1
                    )
            elif normalized_status == "empty" and normalized_substep == "forward_context_metadata":
                self._download_progress["forward_context_empty_count"] = (
                    int(self._download_progress.get("forward_context_empty_count") or 0) + 1
                )
            elif normalized_status == "error" and normalized_substep == "forward_context_metadata":
                self._download_progress["forward_context_error_count"] = (
                    int(self._download_progress.get("forward_context_error_count") or 0) + 1
                )
            elif normalized_status == "unavailable" and normalized_substep == "forward_context_metadata":
                self._download_progress["forward_context_unavailable_count"] = (
                    int(self._download_progress.get("forward_context_unavailable_count") or 0) + 1
                )
            elif normalized_status == "storm_skip":
                self._download_progress["forward_timeout_storm_skip_count"] = (
                    int(self._download_progress.get("forward_timeout_storm_skip_count") or 0) + 1
                )

    def _resolve_via_context_only(
        self,
        request: dict[str, Any],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        hint = self._request_hint(request)
        if not self._has_context_hint(hint):
            return None
        if self._should_skip_context_hydration_for_request(request):
            return None
        asset_type = str(request.get("asset_type") or "").strip()
        asset_role = str(request.get("asset_role") or "").strip() or None
        old_bucket = self._old_context_bucket(asset_type, request)
        if self._should_skip_old_bucket(old_bucket):
            return None, self._missing_bucket_resolver(old_bucket)
        payload = self._download_via_context(
            hint,
            asset_type=asset_type,
            asset_role=asset_role,
            request=request,
            trace_callback=trace_callback,
        )
        if payload is None:
            self._note_old_bucket_failure(old_bucket)
            return None, self._missing_bucket_resolver(old_bucket)
        public_resolved = self._resolve_from_public_token(
            payload,
            old_bucket=old_bucket,
            expired_candidate=self._should_share_missing_outcome(request),
            request=request,
            trace_callback=trace_callback,
        )
        if public_resolved not in {None, (None, None)}:
            resolved_path, resolver = public_resolved
            if resolved_path is not None:
                self._note_old_bucket_success(old_bucket)
            elif resolver == "qq_expired_after_napcat":
                self._note_old_bucket_expired_like(old_bucket)
            return public_resolved
        fast_resolved = self._resolve_from_fast_payload(payload)
        if fast_resolved != (None, None):
            self._note_old_bucket_success(old_bucket)
            return fast_resolved
        classified_missing = self._classify_missing_from_payload(
            payload,
            old_bucket=old_bucket,
            expired_candidate=self._should_share_missing_outcome(request),
            request=request,
        )
        if classified_missing is not None:
            self._emit_missing_classification_trace(
                trace_callback,
                request,
                substep="context_missing_classification",
                classification=classified_missing,
            )
            self._note_old_bucket_expired_like(old_bucket)
            return None, classified_missing
        self._note_old_bucket_failure(old_bucket)
        return None, self._missing_bucket_resolver(old_bucket)

    def _context_payload_for_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            key = self._request_key(request)
            prefetched_payload = self._prefetched_forward_media_payloads.get(key)
            if isinstance(prefetched_payload, dict):
                return prefetched_payload
            _ = self._download_via_forward_context(request)
            forward_payload = self._prefetched_forward_media_payloads.get(key)
            return forward_payload if isinstance(forward_payload, dict) else None
        cached_payload = request.get("_context_payload")
        if isinstance(cached_payload, dict):
            return cached_payload
        asset_type = str(request.get("asset_type") or "").strip()
        asset_role = str(request.get("asset_role") or "").strip() or None
        return self._download_via_context(hint, asset_type=asset_type, asset_role=asset_role)

    def _resolve_via_fresh_public_retry(
        self,
        request: dict[str, Any],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type != "image":
            return None
        old_bucket = self._old_context_bucket(asset_type, request)
        if old_bucket is not None:
            return None
        hint = self._request_hint(request)
        if not self._has_context_hint(hint) or self._has_forward_parent_hint(hint):
            return None
        asset_role = str(request.get("asset_role") or "").strip() or None
        payload = self._download_via_context(
            hint,
            asset_type=asset_type,
            asset_role=asset_role,
            request=request,
            trace_callback=trace_callback,
        )
        if payload is None:
            return None
        public_resolved = self._resolve_from_public_token(
            payload,
            old_bucket=old_bucket,
            request=request,
            trace_callback=trace_callback,
        )
        if public_resolved not in {None, (None, None)}:
            return public_resolved
        fast_resolved = self._resolve_from_fast_payload(payload)
        if fast_resolved != (None, None):
            return fast_resolved
        return None

    def _resolve_via_direct_public_token_hint(
        self,
        request: dict[str, Any],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        payload = self._direct_public_token_payload_for_request(request)
        if payload is None:
            return None
        asset_type = str(request.get("asset_type") or "").strip()
        old_bucket = self._old_context_bucket(asset_type, request)
        return self._resolve_from_public_token(
            payload,
            old_bucket=old_bucket,
            expired_candidate=self._should_share_missing_outcome(request),
            request=request,
            trace_callback=trace_callback,
        )

    def _resolve_via_direct_file_id(
        self,
        request: dict[str, Any],
        *,
        payload: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type not in {"file", "video"}:
            return None
        old_bucket = self._old_context_bucket(asset_type, request)
        file_id = self._direct_forward_file_identifier(request, payload=payload)
        if not file_id or not file_id.startswith("/"):
            return None
        timeout_s = self._direct_file_id_timeout_s(request)
        if self._should_skip_forward_timeout_storm(
            request,
            route="direct_file_id_get_file",
        ):
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep="direct_file_id_get_file",
                timeout_s=timeout_s,
                status="storm_skip",
                elapsed_s=0.0,
                detail="skipped old forward route after repeated timeouts",
            )
            self._note_remote_substep_progress(
                substep="direct_file_id_get_file",
                status="storm_skip",
            )
            return None
        self._emit_asset_substep_trace(
            trace_callback,
            request,
            stage="start",
            substep="direct_file_id_get_file",
            timeout_s=timeout_s,
        )
        started = monotonic()
        try:
            # NapCat GetFileBase treats `file` as authoritative and only falls back
            # to `file_id` when `file` is empty. Passing both can accidentally turn
            # an exact file-id lookup into a loose name-based search.
            payload = self._client.get_file(file_id=file_id, timeout=timeout_s)
        except NapCatApiTimeoutError as exc:
            self._direct_file_id_timeout_cache.add(self._request_key(request))
            elapsed_s = monotonic() - started
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep="direct_file_id_get_file",
                timeout_s=timeout_s,
                status="timeout",
                elapsed_s=elapsed_s,
                detail=str(exc),
            )
            self._log_remote_substep_outcome(
                request=request,
                substep="direct_file_id_get_file",
                status="timeout",
                timeout_s=timeout_s,
                elapsed_s=elapsed_s,
                detail=str(exc),
            )
            self._note_forward_timeout_storm(
                request,
                route="direct_file_id_get_file",
            )
            return None
        except NapCatApiError as exc:
            elapsed_s = monotonic() - started
            if isinstance(request, dict) and "file not found" in str(exc).strip().lower():
                request["_direct_file_id_not_found"] = True
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep="direct_file_id_get_file",
                timeout_s=timeout_s,
                status="error",
                elapsed_s=elapsed_s,
                detail=str(exc),
            )
            if (
                asset_type in {"file", "video"}
                and self._should_classify_direct_file_id_not_found_as_terminal(
                    request,
                    payload=payload if isinstance(payload, dict) else None,
                )
                and "file not found" in str(exc).strip().lower()
            ):
                if request is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep="direct_file_id_get_file_classification",
                        classification="qq_expired_after_napcat",
                    )
                self._direct_file_id_timeout_cache.discard(self._request_key(request))
                return None, "qq_expired_after_napcat"
            self._note_forward_timeout_storm_success(
                request,
                route="direct_file_id_get_file",
            )
            return None
        self._direct_file_id_timeout_cache.discard(self._request_key(request))
        elapsed_s = monotonic() - started
        self._emit_asset_substep_trace(
            trace_callback,
            request,
            stage="done",
            substep="direct_file_id_get_file",
            timeout_s=timeout_s,
            status="ok",
            elapsed_s=elapsed_s,
        )
        self._log_remote_substep_outcome(
            request=request,
            substep="direct_file_id_get_file",
            status="ok",
            timeout_s=timeout_s,
            elapsed_s=elapsed_s,
        )
        self._note_forward_timeout_storm_success(
            request,
            route="direct_file_id_get_file",
        )
        resolved = self._resolved_path_from_payload(payload if isinstance(payload, dict) else None)
        if resolved is not None:
            return resolved, "napcat_segment_file_id_get_file"
        remote_downloaded = self._resolve_remote_from_public_payload(
            {
                "asset_type": asset_type,
                "file_name": str(request.get("file_name") or "").strip() or None,
            },
            payload if isinstance(payload, dict) else None,
            action="get_file",
        )
        if remote_downloaded is not None:
            return remote_downloaded, "napcat_segment_file_id_get_file_remote_url"
        classified_missing = self._classify_blank_direct_file_id_missing(
            request,
            payload if isinstance(payload, dict) else None,
        )
        if classified_missing is not None:
            self._emit_missing_classification_trace(
                trace_callback,
                request,
                substep="direct_file_id_get_file_classification",
                classification=classified_missing,
            )
            return None, classified_missing
        return None

    def _download_via_forward_context(
        self,
        request: dict[str, Any],
        *,
        materialize: bool = False,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        if self._fast_client is None or self._fast_forward_context_route_disabled:
            return None
        hint = self._request_hint(request)
        parent = hint.get("_forward_parent")
        if not isinstance(parent, dict) or not self._has_context_hint(parent):
            return None
        parent_element_id = str(parent.get("element_id") or "").strip()
        if not parent_element_id:
            return None
        key = self._request_key(request)
        timeout_cache_key = self._forward_context_timeout_key(
            request,
            materialize=materialize,
        )
        cached_forward_payload = (
            self._forward_context_payload_cache.get(timeout_cache_key)
            if not materialize and timeout_cache_key is not None
            else None
        )
        prefetched = self._prefetched_forward_media.get(key)
        prefetched_payload = self._prefetched_forward_media_payloads.get(key)
        if prefetched is not None and not materialize:
            return prefetched
        if (
            materialize
            and prefetched is not None
            and isinstance(prefetched_payload, dict)
            and str(prefetched_payload.get("_forward_targeted_mode") or "").strip().lower()
            in {"single_target_download", "hydrated"}
        ):
            return prefetched
        if (
            materialize
            and timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_timeout_cache
        ):
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        if (
            not materialize
            and timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_timeout_cache
        ):
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        if (
            not materialize
            and timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_empty_cache
        ):
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        if (
            not materialize
            and timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_error_cache
        ):
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        if (
            timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_unavailable_cache
        ):
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        if isinstance(cached_forward_payload, dict):
            assets = cached_forward_payload.get("assets")
            assets_list = assets if isinstance(assets, list) else []
            if assets_list:
                matched, matched_payload = self._pick_forward_asset_match(
                    request,
                    assets_list,
                    trace_callback=trace_callback,
                )
                if isinstance(matched_payload, dict):
                    enriched_payload = dict(matched_payload)
                    enriched_payload["_forward_targeted_mode"] = str(
                        cached_forward_payload.get("targeted_mode") or ""
                    ).strip()
                    matched_payload = enriched_payload
                    self._schedule_remote_media_prefetch(
                        request=request,
                        request_data=request,
                        payload=matched_payload,
                    )
                    self._schedule_public_token_prefetch(
                        request=request,
                        request_data=request,
                        payload=matched_payload,
                    )
                self._prefetched_forward_media[key] = matched
                self._prefetched_forward_media_payloads[key] = matched_payload
                return matched
        timeout_s = self._forward_context_timeout_s(
            request,
            materialize=materialize,
        )
        parent_scoped_metadata = (
            not materialize
            and self._should_use_parent_scoped_forward_metadata(request)
            and self._fast_plugin_supports_forward_parent_metadata()
        )
        substep = "forward_context_materialize" if materialize else "forward_context_metadata"
        if materialize and self._should_skip_forward_parent_expensive_timeout(
            request,
            route=substep,
        ):
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=timeout_s,
                status="cached_skip",
                elapsed_s=0.0,
                detail="skipped repeated forward expensive materialize after prior same-parent timeout",
            )
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        if self._should_skip_forward_timeout_storm(
            request,
            route=substep,
        ):
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=timeout_s,
                status="storm_skip",
                elapsed_s=0.0,
                detail="skipped old forward route after repeated timeouts",
            )
            self._note_remote_substep_progress(substep=substep, status="storm_skip")
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        self._emit_asset_substep_trace(
            trace_callback,
            request,
            stage="start",
            substep=substep,
            timeout_s=timeout_s,
        )
        started = monotonic()
        try:
            payload = self._fast_client.hydrate_forward_media(
                message_id_raw=str(parent["message_id_raw"]),
                element_id=parent_element_id,
                peer_uid=str(parent["peer_uid"]),
                chat_type_raw=int(parent["chat_type_raw"]),
                asset_type=str(request.get("asset_type") or "").strip() or None,
                asset_role=str(request.get("asset_role") or "").strip() or None,
                file_name=(
                    None if parent_scoped_metadata else str(request.get("file_name") or "").strip() or None
                ),
                md5=(
                    None if parent_scoped_metadata else str(request.get("md5") or "").strip() or None
                ),
                file_id=(
                    None if parent_scoped_metadata else str(hint.get("file_id") or "").strip() or None
                ),
                url=(
                    None
                    if parent_scoped_metadata
                    else str(hint.get("remote_url") or hint.get("url") or "").strip() or None
                ),
                materialize=materialize,
                download_timeout_ms=(
                    self.FORWARD_TARGET_DOWNLOAD_TIMEOUT_MS if materialize else None
                ),
                timeout=timeout_s,
            )
        except NapCatFastHistoryUnavailable as exc:
            elapsed_s = monotonic() - started
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=timeout_s,
                status="unavailable",
                elapsed_s=elapsed_s,
                detail=str(exc),
            )
            self._note_remote_substep_progress(substep=substep, status="unavailable")
            self._fast_forward_context_route_disabled = True
            self._logger.info(
                "fast_forward_hydration_unavailable; disabling only /hydrate-forward-media for this process. "
                "Ordinary /hydrate-media remains enabled. detail=%s",
                exc,
            )
            if timeout_cache_key is not None:
                self._forward_context_unavailable_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        except NapCatFastHistoryTimeoutError as exc:
            elapsed_s = monotonic() - started
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=timeout_s,
                status="timeout",
                elapsed_s=elapsed_s,
                detail=str(exc),
            )
            self._log_remote_substep_outcome(
                request=request,
                substep=substep,
                status="timeout",
                timeout_s=timeout_s,
                elapsed_s=elapsed_s,
                detail=str(exc),
            )
            if timeout_cache_key is not None:
                self._forward_context_timeout_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            if materialize:
                self._note_forward_parent_expensive_timeout(
                    request,
                    route=substep,
                )
            self._note_forward_timeout_storm(
                request,
                route=substep,
            )
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        except Exception:
            elapsed_s = monotonic() - started
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=timeout_s,
                status="error",
                elapsed_s=elapsed_s,
            )
            if timeout_cache_key is not None:
                self._forward_context_error_cache.add(timeout_cache_key)
                self._forward_context_payload_cache.pop(timeout_cache_key, None)
            if materialize:
                self._note_forward_parent_expensive_timeout_success(
                    request,
                    route=substep,
                )
            self._note_remote_substep_progress(substep=substep, status="error")
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        elapsed_s = monotonic() - started
        self._emit_asset_substep_trace(
            trace_callback,
            request,
            stage="done",
            substep=substep,
            timeout_s=timeout_s,
            status="ok",
            elapsed_s=elapsed_s,
        )
        self._log_remote_substep_outcome(
            request=request,
            substep=substep,
            status="ok",
            timeout_s=timeout_s,
            elapsed_s=elapsed_s,
        )
        if timeout_cache_key is not None:
            self._forward_context_timeout_cache.discard(timeout_cache_key)
            self._forward_context_empty_cache.discard(timeout_cache_key)
            self._forward_context_error_cache.discard(timeout_cache_key)
            self._forward_context_unavailable_cache.discard(timeout_cache_key)
            if not materialize and isinstance(payload, dict):
                if self._is_forward_context_payload_cacheable(payload):
                    self._forward_context_payload_cache[timeout_cache_key] = payload
                else:
                    self._forward_context_payload_cache.pop(timeout_cache_key, None)
        if materialize:
            self._note_forward_parent_expensive_timeout_success(
                request,
                route=substep,
            )
        assets = payload.get("assets") if isinstance(payload, dict) else None
        assets_list = assets if isinstance(assets, list) else []
        if not assets_list:
            if materialize and elapsed_s >= self.FORWARD_TIMEOUT_STORM_SLOW_NOOP_ELAPSED_S:
                self._note_forward_timeout_storm(
                    request,
                    route=substep,
                )
            elif not materialize:
                self._note_forward_timeout_storm_success(
                    request,
                    route=substep,
                )
            if timeout_cache_key is not None:
                self._forward_context_empty_cache.add(timeout_cache_key)
                if not materialize:
                    self._forward_context_payload_cache.pop(timeout_cache_key, None)
            self._note_remote_substep_progress(substep=substep, status="empty")
            self._prefetched_forward_media[key] = (None, None)
            self._prefetched_forward_media_payloads[key] = None
            return None
        matched, matched_payload = self._pick_forward_asset_match(
            request,
            assets_list,
            trace_callback=trace_callback,
        )
        if isinstance(matched_payload, dict) and isinstance(payload, dict):
            enriched_payload = dict(matched_payload)
            enriched_payload["_forward_targeted_mode"] = str(payload.get("targeted_mode") or "").strip()
            matched_payload = enriched_payload
        self._prefetched_forward_media[key] = matched
        self._prefetched_forward_media_payloads[key] = matched_payload
        if isinstance(matched_payload, dict):
            self._schedule_remote_media_prefetch(
                request=request,
                request_data=request,
                payload=matched_payload,
            )
            self._schedule_public_token_prefetch(
                request=request,
                request_data=request,
                payload=matched_payload,
            )
        if materialize:
            if matched not in {None, (None, None)}:
                self._note_forward_timeout_storm_success(
                    request,
                    route=substep,
                )
            elif elapsed_s >= self.FORWARD_TIMEOUT_STORM_SLOW_NOOP_ELAPSED_S:
                self._note_forward_timeout_storm(
                    request,
                    route=substep,
                )
        else:
            self._note_forward_timeout_storm_success(
                request,
                route=substep,
            )
        return matched

    @staticmethod
    def _should_use_parent_scoped_forward_metadata(request: dict[str, Any]) -> bool:
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type != "image":
            return False
        hint = NapCatMediaDownloader._request_hint(request)
        return NapCatMediaDownloader._has_forward_parent_hint(hint)

    def _fast_plugin_supports_forward_parent_metadata(self) -> bool:
        if self._fast_client is None:
            return False
        if self._fast_capabilities_loaded:
            features = (
                self._fast_capabilities.get("features")
                if isinstance(self._fast_capabilities, dict)
                else None
            )
            if isinstance(features, dict):
                return bool(features.get("forward_parent_metadata_scope"))
            return False
        self._fast_capabilities_loaded = True
        try:
            capabilities = self._fast_client.capabilities()
        except Exception:
            self._fast_capabilities = {}
            return False
        self._fast_capabilities = capabilities if isinstance(capabilities, dict) else {}
        features = self._fast_capabilities.get("features")
        if isinstance(features, dict):
            return bool(features.get("forward_parent_metadata_scope"))
        return False

    def _download_via_context(
        self,
        hint: dict[str, Any],
        *,
        asset_type: str,
        asset_role: str | None,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
        ) -> dict[str, Any] | None:
        if (
            self._fast_client is None
            or self._fast_context_route_disabled
            or not self._has_context_hint(hint)
        ):
            if isinstance(request, dict):
                request["_context_payload"] = None
                request["_context_hydration_yielded_no_local_path"] = False
            return None
        timeout_s = self.CONTEXT_ROUTE_TIMEOUT_S
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="start",
                substep="context_hydration",
                timeout_s=timeout_s,
            )
        started = monotonic()
        try:
            data = self._fast_client.hydrate_media(
                message_id_raw=str(hint["message_id_raw"]),
                element_id=str(hint["element_id"]),
                peer_uid=str(hint["peer_uid"]),
                chat_type_raw=int(hint["chat_type_raw"]),
                asset_type=asset_type,
                asset_role=asset_role,
                timeout=timeout_s,
            )
        except NapCatFastHistoryUnavailable as exc:
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep="context_hydration",
                    timeout_s=timeout_s,
                    status="unavailable",
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
            self._fast_context_route_disabled = True
            self._logger.info(
                "fast_context_hydration_unavailable; disabling /hydrate-media for this process and "
                "falling back to local/public recovery. /hydrate-forward-media remains independently available. "
                "A NapCat restart may be required. detail=%s",
                exc,
            )
            if isinstance(request, dict):
                request["_context_payload"] = None
                request["_context_hydration_yielded_no_local_path"] = False
            return None
        except NapCatFastHistoryTimeoutError as exc:
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep="context_hydration",
                    timeout_s=timeout_s,
                    status="timeout",
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
                self._log_remote_substep_outcome(
                    request=request,
                    substep="context_hydration",
                    status="timeout",
                    timeout_s=timeout_s,
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
            if isinstance(request, dict):
                request["_context_payload"] = None
                request["_context_hydration_yielded_no_local_path"] = False
            return None
        except (NapCatFastHistoryError, ValueError, TypeError):
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep="context_hydration",
                    timeout_s=timeout_s,
                    status="error",
                    elapsed_s=elapsed_s,
                )
            if isinstance(request, dict):
                request["_context_payload"] = None
                request["_context_hydration_yielded_no_local_path"] = False
            return None
        except Exception:
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep="context_hydration",
                    timeout_s=timeout_s,
                    status="error",
                    elapsed_s=elapsed_s,
                )
            if isinstance(request, dict):
                request["_context_payload"] = None
                request["_context_hydration_yielded_no_local_path"] = False
            return None
        elapsed_s = monotonic() - started
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep="context_hydration",
                timeout_s=timeout_s,
                status="ok",
                elapsed_s=elapsed_s,
            )
            self._log_remote_substep_outcome(
                request=request,
                substep="context_hydration",
                status="ok",
                timeout_s=timeout_s,
                elapsed_s=elapsed_s,
            )
        if isinstance(request, dict):
            request["_context_payload"] = data if isinstance(data, dict) else None
            request["_context_hydration_yielded_no_local_path"] = bool(
                isinstance(data, dict) and self._resolved_path_from_payload(data) is None
            )
        return data if isinstance(data, dict) else None

    def _pick_forward_asset_match(
        self,
        request: dict[str, Any],
        assets: list[dict[str, Any]],
        *,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[tuple[Path | None, str | None], dict[str, Any] | None]:
        asset_type = str(request.get("asset_type") or "").strip()
        asset_role = str(request.get("asset_role") or "").strip() or None
        hint = self._request_hint(request)
        file_name = str(request.get("file_name") or "").strip().lower()
        md5 = str(request.get("md5") or "").strip().lower()
        file_id = str(hint.get("file_id") or "").strip()
        file_biz_id = str(hint.get("file_biz_id") or request.get("file_biz_id") or "").strip()
        request_url = self._normalized_match_url(
            hint.get("remote_url") or hint.get("url")
        )
        request_stem = self._normalized_file_stem(file_name)
        best_match: dict[str, Any] | None = None
        best_score = -1
        best_rank = (-1, -1, -1, -1)
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            if str(asset.get("asset_type") or "").strip() != asset_type:
                continue
            asset_asset_role = str(asset.get("asset_role") or "").strip()
            if asset_role and asset_asset_role and asset_asset_role != asset_role:
                continue
            asset_file_name = str(asset.get("file_name") or "").strip().lower()
            asset_md5 = str(asset.get("md5") or "").strip().lower()
            asset_file_id = str(asset.get("file_id") or "").strip()
            asset_file_biz_id = str(asset.get("file_biz_id") or "").strip()
            asset_url = self._normalized_match_url(
                asset.get("remote_url") or asset.get("url")
            )
            asset_stem = self._normalized_file_stem(asset_file_name)
            score = 0
            if md5 and asset_md5 and asset_md5 == md5:
                score += 100
            if file_id and asset_file_id and asset_file_id == file_id:
                score += 90
            if file_biz_id and asset_file_biz_id and asset_file_biz_id == file_biz_id:
                score += 85
            if request_url and asset_url and asset_url == request_url:
                score += 70
            exact_file_name_match = bool(file_name and asset_file_name and asset_file_name == file_name)
            if exact_file_name_match:
                score += 50
            elif request_stem and asset_stem and asset_stem == request_stem:
                score += 20
            if score <= 0:
                continue
            has_local_recovery = 1 if self._resolved_path_from_payload(asset) is not None else 0
            has_remote_recovery = 1 if self._resolve_remote_url(
                asset.get("remote_url") or asset.get("url")
            ) else 0
            has_public_recovery = 1 if str(asset.get("public_file_token") or "").strip() else 0
            rank = (
                has_local_recovery,
                has_remote_recovery,
                has_public_recovery,
                len(asset_file_name or ""),
            )
            if score > best_score or (score == best_score and rank > best_rank):
                best_score = score
                best_rank = rank
                best_match = asset
        if best_match is None:
            return (None, None), None
        resolved = self._resolve_from_fast_payload(
            best_match,
            default_resolver="napcat_forward_hydrated",
        )
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            resolved = (None, None)
        if resolved == (None, None):
            resolved = self._resolve_from_forward_remote_url(
                best_match,
                request=request,
                trace_callback=trace_callback,
            )
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            resolved = (None, None)
        if resolved == (None, None) and asset_type == "image":
            classified_forward_missing = self._classify_forward_missing(
                request,
                payload=best_match,
            )
            if classified_forward_missing is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep="forward_missing_classification",
                    classification=classified_forward_missing,
                )
                resolved = (None, classified_forward_missing)
        if resolved == (None, None):
            resolved = self._resolve_from_public_token(
                best_match,
                request=request,
                trace_callback=trace_callback,
            )
        if not isinstance(resolved, tuple) or len(resolved) != 2:
            resolved = (None, None)
        return resolved, best_match

    @staticmethod
    def _is_forward_context_payload_cacheable(payload: dict[str, Any] | None) -> bool:
        if not isinstance(payload, dict):
            return False
        if bool(payload.get("targeted")):
            return False
        targeted_mode = str(payload.get("targeted_mode") or "").strip().lower()
        if targeted_mode in {"single_target_download", "hydrated"}:
            return False
        return True

    def _download(self, asset_type: str, *, file_id: str | None, file_name: str | None) -> str | None:
        try:
            if asset_type == "image":
                data = self._client.get_image(
                    file_id=file_id,
                    file=file_name,
                    timeout=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
                )
            elif asset_type == "speech":
                data = self._client.get_record(
                    file_id=file_id,
                    file=file_name,
                    timeout=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
                )
            elif asset_type == "sticker":
                return None
            else:
                data = self._client.get_file(
                    file_id=file_id,
                    file=file_name,
                    timeout=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
                )
        except NapCatApiError:
            return None
        path = data.get("file") or data.get("url")
        return str(path) if path else None

    def _call_public_action_with_token(
        self,
        action: str,
        token: str,
        *,
        file_name: str | None = None,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        normalized_action = str(action or "").strip().lower()
        if not normalized_action or not token:
            return None
        request_timeout_scope_key = self._request_scoped_public_action_timeout_key(
            request,
            action=normalized_action,
            token=token,
        )
        parent_timeout_scope_key = self._forward_parent_scoped_public_action_timeout_key(
            request,
            action=normalized_action,
        )
        parent_expensive_timeout_key = self._forward_parent_expensive_timeout_key(
            request,
            route=f"public_token_{normalized_action}",
        )
        if (
            request_timeout_scope_key is not None
            and request_timeout_scope_key in self._request_scoped_public_action_timeout_cache
        ):
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=f"public_token_{normalized_action}",
                    timeout_s=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
                    status="cached_skip",
                    elapsed_s=0.0,
                    detail="skipped repeated forward public token retry after prior timeout",
                )
            return None
        if (
            parent_timeout_scope_key is not None
            and parent_timeout_scope_key in self._forward_parent_public_action_timeout_cache
        ):
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=f"public_token_{normalized_action}",
                    timeout_s=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
                    status="cached_skip",
                    elapsed_s=0.0,
                    detail="skipped repeated forward public token retry after prior same-parent timeout",
                )
            return None
        if (
            parent_expensive_timeout_key is not None
            and parent_expensive_timeout_key in self._forward_parent_public_action_timeout_cache
        ):
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=f"public_token_{normalized_action}",
                    timeout_s=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
                    status="cached_skip",
                    elapsed_s=0.0,
                    detail="skipped repeated forward expensive retry after prior same-parent timeout",
                )
            return None
        known_bad_token = self._known_bad_public_tokens.get((normalized_action, token))
        if known_bad_token:
            return self._annotate_public_token_payload(
                {
                "_known_missing_classification": known_bad_token,
                "_known_missing_detail": "cached_known_bad_token",
                },
                action=normalized_action,
                token=token,
                file_name=file_name,
                request=request,
            )
        timeout_s = self._public_action_timeout_s(
            normalized_action,
            request=request,
        )
        if self._should_skip_forward_timeout_storm(
            request,
            route=f"public_token_{normalized_action}",
        ):
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=f"public_token_{normalized_action}",
                    timeout_s=timeout_s,
                    status="storm_skip",
                    elapsed_s=0.0,
                    detail="skipped old forward route after repeated timeouts",
                )
                self._note_remote_substep_progress(
                    substep=f"public_token_{normalized_action}",
                    status="storm_skip",
                )
            return None
        cache_key = (normalized_action, token)

        primary_substep = f"public_token_{normalized_action}"
        if cache_key in self._public_token_action_outcomes:
            cached_payload = self._public_token_action_outcomes[cache_key]
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=primary_substep,
                    timeout_s=timeout_s,
                    status="cached" if cached_payload is not None else "cached_skip",
                    elapsed_s=0.0,
                    detail=(
                        "reused cached public token result"
                        if cached_payload is not None
                        else "skipped repeated public token retry after cached failure"
                    ),
                )
            return cached_payload
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="start",
                substep=primary_substep,
                timeout_s=timeout_s,
            )
        started = monotonic()
        try:
            if normalized_action == "get_image":
                payload = self._client.get_image(file=token, timeout=timeout_s)
            elif normalized_action == "get_file":
                payload = self._client.get_file(file=token, timeout=timeout_s)
            elif normalized_action == "get_record":
                payload = self._client.get_record(file=token, out_format="mp3", timeout=timeout_s)
            else:
                return None
        except NapCatApiTimeoutError as exc:
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=primary_substep,
                    timeout_s=timeout_s,
                    status="timeout",
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
                self._log_remote_substep_outcome(
                    request=request,
                    substep=primary_substep,
                    status="timeout",
                    timeout_s=timeout_s,
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
            if request_timeout_scope_key is not None:
                self._request_scoped_public_action_timeout_cache.add(request_timeout_scope_key)
            if parent_timeout_scope_key is not None:
                self._forward_parent_public_action_timeout_cache.add(parent_timeout_scope_key)
            if parent_expensive_timeout_key is not None:
                self._note_forward_parent_expensive_timeout(
                    request,
                    route=primary_substep,
                )
            self._public_token_action_outcomes[cache_key] = None
            self._note_forward_timeout_storm(
                request,
                route=primary_substep,
            )
            return None
        except NapCatApiError as exc:
            elapsed_s = monotonic() - started
            known_missing = self._classify_known_public_action_error(normalized_action, exc)
            if known_missing is None:
                known_missing = self._classify_public_action_missing_from_error(
                    normalized_action,
                    exc,
                    request=request,
                )
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=primary_substep,
                    timeout_s=timeout_s,
                    status="error",
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
            if known_missing is not None:
                self._known_bad_public_tokens[(normalized_action, token)] = known_missing
                payload = self._annotate_public_token_payload(
                    {
                    "_known_missing_classification": known_missing,
                    "_known_missing_detail": str(exc),
                    },
                    action=normalized_action,
                    token=token,
                    file_name=file_name,
                    request=request,
                )
                self._public_token_action_outcomes[cache_key] = payload
                if parent_timeout_scope_key is not None:
                    self._forward_parent_public_action_timeout_cache.discard(parent_timeout_scope_key)
                if parent_expensive_timeout_key is not None:
                    self._note_forward_parent_expensive_timeout_success(
                        request,
                        route=primary_substep,
                    )
                return payload
            payload = None
        else:
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=primary_substep,
                    timeout_s=timeout_s,
                    status="ok",
                    elapsed_s=elapsed_s,
                )
                self._log_remote_substep_outcome(
                    request=request,
                    substep=primary_substep,
                    status="ok",
                    timeout_s=timeout_s,
                    elapsed_s=elapsed_s,
                )
            payload = self._annotate_public_token_payload(
                payload,
                action=normalized_action,
                token=token,
                file_name=file_name,
                request=request,
            )
            self._public_token_action_outcomes[cache_key] = payload
            if parent_timeout_scope_key is not None:
                self._forward_parent_public_action_timeout_cache.discard(parent_timeout_scope_key)
            if parent_expensive_timeout_key is not None:
                self._note_forward_parent_expensive_timeout_success(
                    request,
                    route=primary_substep,
                )
            return payload

        # Compatibility fallback for runtimes that still expect `file_id`.
        fallback_substep = f"{primary_substep}_fallback"
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="start",
                substep=fallback_substep,
                timeout_s=timeout_s,
            )
        started = monotonic()
        try:
            if normalized_action == "get_image":
                payload = self._client.get_image(file_id=token, timeout=timeout_s)
            elif normalized_action == "get_file":
                payload = self._client.get_file(file_id=token, timeout=timeout_s)
            elif normalized_action == "get_record":
                payload = self._client.get_record(file_id=token, out_format="mp3", timeout=timeout_s)
            else:
                return None
        except NapCatApiTimeoutError as exc:
            elapsed_s = monotonic() - started
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=fallback_substep,
                    timeout_s=timeout_s,
                    status="timeout",
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
                self._log_remote_substep_outcome(
                    request=request,
                    substep=fallback_substep,
                    status="timeout",
                    timeout_s=timeout_s,
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
            if request_timeout_scope_key is not None:
                self._request_scoped_public_action_timeout_cache.add(request_timeout_scope_key)
            if parent_timeout_scope_key is not None:
                self._forward_parent_public_action_timeout_cache.add(parent_timeout_scope_key)
            if parent_expensive_timeout_key is not None:
                self._note_forward_parent_expensive_timeout(
                    request,
                    route=primary_substep,
                )
            self._public_token_action_outcomes[cache_key] = None
            return None
        except NapCatApiError as exc:
            elapsed_s = monotonic() - started
            known_missing = self._classify_known_public_action_error(normalized_action, exc)
            if known_missing is None:
                known_missing = self._classify_public_action_missing_from_error(
                    normalized_action,
                    exc,
                    request=request,
                )
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=fallback_substep,
                    timeout_s=timeout_s,
                    status="error",
                    elapsed_s=elapsed_s,
                    detail=str(exc),
                )
            if known_missing is not None:
                self._known_bad_public_tokens[(normalized_action, token)] = known_missing
                payload = self._annotate_public_token_payload(
                    {
                    "_known_missing_classification": known_missing,
                    "_known_missing_detail": str(exc),
                    },
                    action=normalized_action,
                    token=token,
                    file_name=file_name,
                    request=request,
                )
                self._public_token_action_outcomes[cache_key] = payload
                if parent_timeout_scope_key is not None:
                    self._forward_parent_public_action_timeout_cache.discard(parent_timeout_scope_key)
                if parent_expensive_timeout_key is not None:
                    self._note_forward_parent_expensive_timeout_success(
                        request,
                        route=primary_substep,
                    )
                self._note_forward_timeout_storm_success(
                    request,
                    route=primary_substep,
                )
                return payload
            self._public_token_action_outcomes[cache_key] = None
            if parent_timeout_scope_key is not None:
                self._forward_parent_public_action_timeout_cache.discard(parent_timeout_scope_key)
            if parent_expensive_timeout_key is not None:
                self._note_forward_parent_expensive_timeout_success(
                    request,
                    route=primary_substep,
                )
            self._note_forward_timeout_storm_success(
                request,
                route=primary_substep,
            )
            return None
        elapsed_s = monotonic() - started
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=fallback_substep,
                timeout_s=timeout_s,
                status="ok",
                elapsed_s=elapsed_s,
            )
            self._log_remote_substep_outcome(
                request=request,
                substep=fallback_substep,
                status="ok",
                timeout_s=timeout_s,
                elapsed_s=elapsed_s,
            )
        payload = self._annotate_public_token_payload(
            payload,
            action=normalized_action,
            token=token,
            file_name=file_name,
            request=request,
        )
        self._public_token_action_outcomes[cache_key] = payload
        if parent_timeout_scope_key is not None:
            self._forward_parent_public_action_timeout_cache.discard(parent_timeout_scope_key)
        if parent_expensive_timeout_key is not None:
            self._note_forward_parent_expensive_timeout_success(
                request,
                route=primary_substep,
            )
        self._note_forward_timeout_storm_success(
            request,
            route=primary_substep,
        )
        return payload

    @staticmethod
    def _annotate_public_token_payload(
        payload: dict[str, Any] | None,
        *,
        action: str,
        token: str,
        file_name: str | None,
        request: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return payload
        annotated = dict(payload)
        annotated.setdefault("public_action", action)
        annotated.setdefault("public_file_token", token)
        if file_name:
            annotated.setdefault("file_name", file_name)
        if isinstance(request, dict):
            asset_type = str(request.get("asset_type") or "").strip()
            if asset_type:
                annotated.setdefault("asset_type", asset_type)
            hint = request.get("download_hint")
            if isinstance(hint, dict):
                file_id = str(hint.get("file_id") or "").strip()
                if file_id:
                    annotated.setdefault("file_id", file_id)
        return annotated

    @staticmethod
    def _classify_known_public_action_error(action: str, exc: Exception) -> str | None:
        if not isinstance(exc, NapCatApiError):
            return None
        message = str(exc).strip().lower()
        if not message:
            return None
        if "illegal tag" in message:
            return "napcat_media_decode_failed"
        if "获取视频url失败" in message or ("videourl" in message and action == "get_file"):
            return "napcat_video_url_unavailable"
        if "获取音频url失败" in message or ("recordurl" in message and action == "get_record"):
            return "napcat_record_url_unavailable"
        if "获取文件url失败" in message and action == "get_file":
            return "napcat_file_url_unavailable"
        return None

    def _classify_public_action_missing_from_error(
        self,
        action: str,
        exc: Exception,
        *,
        request: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(exc, NapCatApiError):
            return None
        message = str(exc).strip().lower()
        if "file not found" not in message:
            return None
        if (
            isinstance(request, dict)
            and self._is_forward_expensive_asset(request)
            and not self._has_unexhausted_live_http_media_url(request)
            and action in {"get_file", "get_record"}
        ):
            return "qq_expired_after_napcat"
        old_bucket = self._old_context_bucket(
            str(request.get("asset_type") or "").strip() if isinstance(request, dict) else "",
            request or {},
        )
        if old_bucket is None:
            return None
        if action in {"get_file", "get_record"}:
            return "qq_expired_after_napcat"
        return None

    def _resolve_from_fast_payload(
        self,
        data: dict[str, Any] | None,
        *,
        default_resolver: str = "napcat_context_hydrated",
    ) -> tuple[Path | None, str | None]:
        resolved = self._resolved_path_from_payload(data)
        if resolved is None:
            return None, None
        return resolved, default_resolver

    def _resolve_from_public_token(
        self,
        data: dict[str, Any] | None,
        *,
        old_bucket: tuple[str, str] | None = None,
        expired_candidate: bool = False,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        if not isinstance(data, dict):
            return None
        if str(data.get("asset_type") or "").strip() == "sticker":
            return None
        action = str(data.get("public_action") or "").strip().lower()
        token = str(data.get("public_file_token") or "").strip()
        if not action or not token:
            return None
        prefetched_remote = self._resolve_prefetched_remote_from_payload(
            data,
            action=action,
            request=request,
            trace_callback=trace_callback,
        )
        if prefetched_remote is not None:
            return prefetched_remote, f"napcat_public_token_{action}_remote_url_prefetched"
        if self._should_prefer_direct_http_remote_before_public_action(
            request=request,
            data=data,
            action=action,
        ):
            remote_downloaded = self._resolve_remote_from_public_payload(
                data,
                data,
                action=action,
                request=request,
                trace_callback=trace_callback,
            )
            if remote_downloaded is not None:
                return remote_downloaded, f"napcat_public_token_{action}_remote_url"
            classified_after_direct_remote = self._classify_missing_from_public_payload(
                data,
                old_bucket=old_bucket,
                expired_candidate=expired_candidate,
                request=request,
            )
            if classified_after_direct_remote is not None:
                if request is not None:
                    self._emit_missing_classification_trace(
                        trace_callback,
                        request,
                        substep=f"public_token_{action}_remote_classification",
                        classification=classified_after_direct_remote,
                    )
                return None, classified_after_direct_remote
            return None
        payload: dict[str, Any] | None = None
        prefetch_request_data = request if isinstance(request, dict) else data
        prefetched_public = self._peek_public_token_prefetch(
            request_data=prefetch_request_data,
            action=action,
            token=token,
            request=request,
            trace_callback=trace_callback,
        )
        if prefetched_public is None:
            prefetched_public = self._consume_public_token_prefetch(
                request_data=prefetch_request_data,
                action=action,
                token=token,
                request=request,
                trace_callback=trace_callback,
            )
        if isinstance(prefetched_public, dict):
            prefetched_path = str(prefetched_public.get("resolved_path") or "").strip()
            if prefetched_path:
                path = Path(prefetched_path)
                if path.exists() and path.is_file():
                    resolver = str(prefetched_public.get("resolver") or "").strip()
                    return path.resolve(), resolver or f"napcat_public_token_{action}_prefetched"
            prefetched_payload = prefetched_public.get("payload")
            if isinstance(prefetched_payload, dict):
                payload = prefetched_payload
        if payload is None:
            payload = self._call_public_action_with_token(
                action,
                token,
                file_name=str(data.get("file_name") or "").strip() or None,
                request=request,
                trace_callback=trace_callback,
            )
        if payload is None:
            return None
        known_missing_classification = str(payload.get("_known_missing_classification") or "").strip()
        if known_missing_classification:
            if request is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep=f"public_token_{action}_classification",
                    classification=known_missing_classification,
                )
            return None, known_missing_classification
        resolved = self._resolved_path_from_payload(payload if isinstance(payload, dict) else None)
        if resolved is not None:
            return resolved, f"napcat_public_token_{action}"
        classified_missing = self._classify_missing_from_public_payload(
            payload if isinstance(payload, dict) else None,
            old_bucket=old_bucket,
            expired_candidate=expired_candidate,
            request=request,
        )
        if classified_missing is not None:
            if request is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep=f"public_token_{action}_classification",
                    classification=classified_missing,
                )
            return None, classified_missing
        remote_downloaded = None
        remote_attempt_already_failed = bool(
            isinstance(prefetched_public, dict)
            and prefetched_public.get("remote_attempted")
            and not str(prefetched_public.get("resolved_path") or "").strip()
        )
        if not remote_attempt_already_failed:
            remote_downloaded = self._resolve_remote_from_public_payload(
                data,
                payload if isinstance(payload, dict) else None,
                action=action,
                request=request,
                trace_callback=trace_callback,
        )
        if remote_downloaded is not None:
            return remote_downloaded, f"napcat_public_token_{action}_remote_url"
        classified_after_remote_failure = self._classify_missing_from_public_payload(
            payload if isinstance(payload, dict) else None,
            old_bucket=old_bucket,
            expired_candidate=expired_candidate,
            request=request,
        )
        if classified_after_remote_failure is not None:
            if request is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep=f"public_token_{action}_remote_classification",
                    classification=classified_after_remote_failure,
                )
            return None, classified_after_remote_failure
        return None

    def _should_prefer_direct_http_remote_before_public_action(
        self,
        *,
        request: dict[str, Any] | None,
        data: dict[str, Any] | None,
        action: str,
    ) -> bool:
        if not isinstance(request, dict) or not isinstance(data, dict):
            return False
        if str(action or "").strip().lower() != "get_image":
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        raw_remote_url = self._public_payload_remote_url(data)
        if not raw_remote_url:
            return False
        parsed = urlparse(raw_remote_url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return False
        return self._asset_location_kind(raw_remote_url) == "https_url"

    def _remember_shared_outcome(
        self,
        shared_key: tuple[Any, ...] | None,
        request: dict[str, Any],
        result: tuple[Path | None, str | None],
    ) -> tuple[Path | None, str | None]:
        if shared_key is None:
            return result
        resolved_path, resolver = result
        if resolved_path is not None or self._should_share_missing_outcome(
            request,
            resolver=resolver,
        ):
            self._shared_media_outcomes[shared_key] = result
        return result

    def _should_share_missing_outcome(
        self,
        request: dict[str, Any],
        *,
        resolver: str | None = None,
    ) -> bool:
        terminal_missing_resolvers = {
            "qq_expired_after_napcat",
            "qq_not_downloaded_local_placeholder",
            "napcat_video_url_unavailable",
            "napcat_file_url_unavailable",
            "napcat_record_url_unavailable",
        }
        normalized_resolver = str(resolver or "").strip()
        if normalized_resolver not in terminal_missing_resolvers:
            return False
        hint = self._request_hint(request)
        asset_type = str(request.get("asset_type") or "").strip()
        if self._has_forward_parent_hint(hint) and asset_type in {"file", "video", "speech"}:
            return normalized_resolver == "qq_expired_after_napcat"
        return True

    def _shared_request_key(self, request: dict[str, Any]) -> tuple[Any, ...] | None:
        hint = self._request_hint(request)
        asset_type = str(request.get("asset_type") or "").strip()
        file_name = str(request.get("file_name") or "").strip().lower()
        md5 = str(request.get("md5") or "").strip().lower()
        source_leaf = ""
        source_path = str(request.get("source_path") or "").strip()
        if source_path:
            source_leaf = PureWindowsPath(source_path).name.strip().lower()
        file_id = str(hint.get("file_id") or "").strip()
        remote_url = self._normalized_match_url(hint.get("remote_url") or hint.get("url"))
        if self._has_forward_parent_hint(hint) and asset_type in {"file", "video", "speech"}:
            strong_forward_identity = any([md5, source_leaf, file_id, remote_url])
            if not strong_forward_identity:
                return None
        if not any([file_name, md5, source_leaf, file_id, remote_url]):
            return None
        return (
            asset_type,
            str(request.get("asset_role") or "").strip(),
            file_name,
            md5,
            source_leaf,
            file_id,
            remote_url,
        )

    def _resolve_from_stale_local_neighbors(
        self,
        request: dict[str, Any],
    ) -> tuple[Path | None, str | None]:
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type != "image":
            return None, None
        source_path = str(request.get("source_path") or "").strip()
        if not source_path:
            return None, None
        fallback = self._find_stale_image_neighbor(source_path)
        if fallback is None:
            return None, None
        return fallback, "stale_source_neighbor"

    def _resolve_from_source_local_path(
        self,
        request: dict[str, Any] | None,
    ) -> tuple[Path | None, str | None]:
        if not isinstance(request, dict):
            return None, None
        source_path = str(request.get("source_path") or "").strip()
        if not source_path:
            return None, None
        cache_key = source_path.casefold()
        if cache_key in self._source_local_resolution_cache:
            resolved = self._source_local_resolution_cache[cache_key]
        else:
            resolved = self._resolved_path_from_payload({"file": source_path})
            self._source_local_resolution_cache[cache_key] = resolved
        if resolved is None:
            return None, None
        return resolved, "source_local_path"

    def _resolve_from_hint_local_path(
        self,
        hint: dict[str, Any] | None,
    ) -> tuple[Path | None, str | None]:
        if not isinstance(hint, dict):
            return None, None
        cache_key = (
            str(hint.get("file") or "").strip().casefold(),
            str(hint.get("path") or "").strip().casefold(),
            str(hint.get("url") or "").strip().casefold(),
        )
        if cache_key in self._hint_local_resolution_cache:
            cached = self._hint_local_resolution_cache[cache_key]
            if cached is None:
                return None, None
            return cached, "hint_local_path"
        for key in ("file", "path", "url"):
            resolved = self._resolved_path_from_payload({key: hint.get(key)})
            if resolved is not None:
                self._hint_local_resolution_cache[cache_key] = resolved
                return resolved, "hint_local_path"
        self._hint_local_resolution_cache[cache_key] = None
        return None, None

    def _find_stale_image_neighbor(self, source_path: str) -> Path | None:
        cache_key = source_path.casefold()
        if cache_key in self._stale_image_neighbor_cache:
            return self._stale_image_neighbor_cache[cache_key]
        source = Path(source_path)
        if source.exists() and source.is_file():
            try:
                if source.stat().st_size > 0:
                    resolved = source.resolve()
                    self._stale_image_neighbor_cache[cache_key] = resolved
                    return resolved
            except OSError:
                self._stale_image_neighbor_cache[cache_key] = None
                return None
        parts = list(PureWindowsPath(source_path).parts)
        lowered = [part.casefold() for part in parts]
        if "nt_qq" not in lowered or "nt_data" not in lowered:
            self._stale_image_neighbor_cache[cache_key] = None
            return None
        parent = source.parent
        parent_name = parent.name.casefold()
        if parent_name not in {"ori", "oritemp", "thumb"}:
            self._stale_image_neighbor_cache[cache_key] = None
            return None
        stem = self._strip_thumb_suffix(source.stem)
        if not stem:
            self._stale_image_neighbor_cache[cache_key] = None
            return None
        base_dir = parent.parent
        index = self._ntqq_image_candidate_index_for_base_dir(base_dir)
        best_match, has_any_match = index.get(stem.casefold(), (None, False))
        if not has_any_match or best_match is None:
            self._stale_image_neighbor_cache[cache_key] = None
            return None
        self._stale_image_neighbor_cache[cache_key] = best_match
        return best_match

    @staticmethod
    def _strip_thumb_suffix(value: str) -> str:
        lowered = value.casefold()
        if lowered.endswith("_720"):
            return value[:-4]
        if lowered.endswith("_0"):
            return value[:-2]
        return value

    @staticmethod
    def _iter_image_candidates_in_directory(directory: Path, *, stem: str):
        direct = directory / stem
        if direct.exists() and direct.is_file():
            yield direct.resolve()
        for pattern in (f"{stem}.*", f"{stem}_*.*", f"{stem}_*"):
            for candidate in directory.glob(pattern):
                if not candidate.is_file():
                    continue
                if not NapCatMediaDownloader._image_extension_allowed(candidate):
                    continue
                yield candidate.resolve()

    @staticmethod
    def _find_image_candidate_in_directory(directory: Path, *, stem: str) -> Path | None:
        candidates: list[Path] = []
        for candidate in NapCatMediaDownloader._iter_image_candidates_in_directory(
            directory,
            stem=stem,
        ):
            if candidate.stat().st_size <= 0:
                continue
            candidates.append(candidate.resolve())
        if not candidates:
            return None
        unique_candidates = {candidate.resolve(): None for candidate in candidates}
        return sorted(unique_candidates, key=NapCatMediaDownloader._neighbor_candidate_priority)[0]

    @staticmethod
    def _image_extension_allowed(path: Path) -> bool:
        suffix = path.suffix.casefold()
        return suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} or suffix == ""

    @staticmethod
    def _neighbor_candidate_priority(path: Path) -> tuple[int, int, int, int, str]:
        parent_rank = {"ori": 0, "oritemp": 1, "thumb": 2}.get(path.parent.name.casefold(), 99)
        suffix_rank = {
            ".gif": 0,
            ".webp": 1,
            ".png": 2,
            ".jpg": 3,
            ".jpeg": 4,
            ".bmp": 5,
            "": 6,
        }.get(path.suffix.casefold(), 99)
        stem = path.stem if path.suffix else path.name
        match = re.search(r"_(\d+)$", stem)
        has_variant = 1 if match else 0
        variant_rank = -int(match.group(1)) if match else 0
        return (parent_rank, suffix_rank, has_variant, variant_rank, str(path))

    @staticmethod
    def _normalized_file_stem(value: object) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        return Path(text).stem.strip().lower()

    @staticmethod
    def _normalized_match_url(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        parsed = urlparse(text)
        if parsed.scheme and parsed.netloc:
            return parsed._replace(
                scheme=parsed.scheme.lower(),
                netloc=parsed.netloc.lower(),
                fragment="",
            ).geturl()
        return text.lower()

    @staticmethod
    def _resolved_path_from_payload(data: dict[str, Any] | None) -> Path | None:
        if not isinstance(data, dict):
            return None
        value = data.get("file") or data.get("path") or data.get("url")
        if not value:
            return None
        path = Path(str(value))
        if not path.exists() or not path.is_file():
            return None
        try:
            if path.stat().st_size <= 0:
                return None
        except OSError:
            return None
        return path.resolve()

    def _iter_forward_remote_url_candidates(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(request, dict):
            return ()
        hint = self._request_hint(request)
        original_url = self._resolve_remote_url(
            str(
                (payload or {}).get("remote_url")
                or (payload or {}).get("url")
                or hint.get("remote_url")
                or hint.get("url")
                or ""
            ).strip()
        )
        values: list[tuple[str, str]] = []
        seen: set[str] = set()
        if original_url:
            normalized = self._normalized_match_url(original_url)
            values.append(("original", original_url))
            seen.add(normalized)
            projected_url = self._derive_projected_localhost_download_url(original_url)
            if projected_url:
                projected_normalized = self._normalized_match_url(projected_url)
                if projected_normalized not in seen:
                    values.append(("projected", projected_url))
                    seen.add(projected_normalized)
        return tuple(values)

    def _project_download_path_onto_remote_base(self, candidate: str) -> str | None:
        parsed = urlparse(candidate)
        if not parsed.path.startswith("/download"):
            return None
        if not self._remote_base_url:
            return None
        relative_path = parsed.path
        if parsed.query:
            relative_path += f"?{parsed.query}"
        if parsed.fragment:
            relative_path += f"#{parsed.fragment}"
        return urljoin(self._remote_base_url.rstrip("/") + "/", relative_path.lstrip("/"))

    def _derive_projected_localhost_download_url(self, remote_url: str | None) -> str | None:
        candidate = str(remote_url or "").strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            host = parsed.netloc.lower()
            if host.startswith("127.0.0.1") or host.startswith("localhost"):
                return candidate
            return self._project_download_path_onto_remote_base(candidate)
        if candidate.startswith("/download") or candidate.startswith("download?"):
            if not self._remote_base_url:
                return None
            return urljoin(self._remote_base_url.rstrip("/") + "/", candidate.lstrip("/"))
        return None

    def _remember_remote_media_failure_reason(self, remote_url: str | None, reason: str | None) -> None:
        normalized = self._normalized_match_url(remote_url or "")
        if not normalized:
            return
        if reason:
            self._remote_media_failure_reasons[normalized] = reason
        else:
            self._remote_media_failure_reasons.pop(normalized, None)

    @staticmethod
    def _classify_remote_media_response_failure(response: httpx.Response) -> str | None:
        payload: dict[str, Any] | None = None
        content = response.content
        content_type = str(response.headers.get("content-type") or "").strip().lower()
        should_probe_json = bool(content) and (
            "application/json" in content_type
            or content.lstrip().startswith((b"{", b"["))
        )
        if should_probe_json:
            with suppress(ValueError):
                parsed_payload = response.json()
                if isinstance(parsed_payload, dict):
                    payload = parsed_payload
        if isinstance(payload, dict):
            detail = " ".join(
                str(payload.get(key) or "").strip().lower()
                for key in ("message", "msg", "wording", "retmsg")
            )
            retcode = payload.get("retcode")
            code = payload.get("code")
            status = str(payload.get("status") or "").strip().lower()
            if retcode in {-5503042, "-5503042"} or "file has expired" in detail:
                return "expired_remote"
            if "unsupported api download" in detail or "不支持的api download" in detail:
                return "unsupported_local_download"
            if status and status != "ok":
                return "json_error"
            if code not in {None, 0, "0"} or retcode not in {None, 0, "0"}:
                return "json_error"
        if response.status_code >= 400:
            return "http_error"
        return None

    def _resolve_from_forward_remote_url(
        self,
        data: dict[str, Any] | None,
        *,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None]:
        if not isinstance(data, dict):
            return None, None
        asset_type = str(data.get("asset_type") or "").strip()
        if asset_type not in {"image", "file", "speech", "video"}:
            return None, None
        prefetched, had_prefetched_result = self._resolve_prefetched_remote_url(
            data,
            substep="forward_remote_url_prefetch",
            request=request,
            trace_callback=trace_callback,
        )
        if prefetched is not None:
            return prefetched, "napcat_forward_remote_url_prefetched"
        if had_prefetched_result:
            return None, None
        file_name = str(data.get("file_name") or "").strip() or None
        for label, candidate_url in self._iter_forward_remote_url_candidates(
            request,
            payload=data if isinstance(data, dict) else None,
        ):
            if not candidate_url:
                continue
            cache_key = (asset_type, self._normalized_match_url(candidate_url))
            substep = "forward_remote_url_projected" if label == "projected" else "forward_remote_url"
            cached_resolution = self._consume_remote_media_prefetch(cache_key)
            if cached_resolution is not ...:
                extra_fields = {
                    "attempt_url": candidate_url,
                    "attempt_url_kind": self._asset_location_kind(candidate_url),
                    "attempt_url_host": self._asset_location_host(candidate_url),
                    "attempt_label": label,
                    "failure_reason": self._remote_media_failure_reasons.get(
                        self._normalized_match_url(candidate_url)
                    ),
                }
                if request is not None:
                    self._emit_asset_substep_trace(
                        trace_callback,
                        request,
                        stage="done",
                        substep=substep,
                        timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                        status="cached_ok" if cached_resolution else "cached_error",
                        detail=candidate_url,
                        extra_fields=extra_fields,
                    )
                if cached_resolution:
                    path = Path(cached_resolution)
                    if path.exists() and path.is_file():
                        return path.resolve(), "napcat_forward_remote_url"
                    self._drop_remote_prefetch_result(cache_key)
                continue
            if request is not None:
                extra_fields = {
                    "attempt_url": candidate_url,
                    "attempt_url_kind": self._asset_location_kind(candidate_url),
                    "attempt_url_host": self._asset_location_host(candidate_url),
                    "attempt_label": label,
                }
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="start",
                    substep=substep,
                    timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                    detail=candidate_url,
                    extra_fields=extra_fields,
                )
            started = monotonic()
            resolved = self._download_remote_media(
                asset_type=asset_type,
                file_name=file_name,
                hint={"url": candidate_url},
            )
            elapsed_s = monotonic() - started
            self._store_remote_prefetch_result(cache_key, resolved)
            self._record_prefetch_feedback("remote_ok" if resolved is not None else "remote_error")
            resolved_size_bytes: int | None = None
            if resolved is not None:
                try:
                    resolved_size_bytes = Path(resolved).stat().st_size
                except OSError:
                    resolved_size_bytes = None
            if request is not None:
                extra_fields = {
                    "attempt_url": candidate_url,
                    "attempt_url_kind": self._asset_location_kind(candidate_url),
                    "attempt_url_host": self._asset_location_host(candidate_url),
                    "resolved_size_bytes": resolved_size_bytes,
                    "attempt_label": label,
                    "failure_reason": self._remote_media_failure_reasons.get(
                        self._normalized_match_url(candidate_url)
                    ),
                }
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=substep,
                    timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                    status="ok" if resolved is not None else "error",
                    elapsed_s=elapsed_s,
                    detail=candidate_url,
                    extra_fields=extra_fields,
                )
                self._log_remote_substep_outcome(
                    request=request,
                    substep=substep,
                    status="ok" if resolved is not None else "error",
                    timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                    elapsed_s=elapsed_s,
                    detail=candidate_url,
                    extra_fields=extra_fields,
                )
            if resolved is None:
                continue
            path = Path(resolved)
            if not path.exists() or not path.is_file():
                continue
            return path.resolve(), "napcat_forward_remote_url"
        return None, None

    def _resolve_from_sticker_remote_url(
        self,
        hint: dict[str, Any] | None,
        *,
        asset_role: str | None,
        file_name: str | None,
    ) -> tuple[Path | None, str | None]:
        if not isinstance(hint, dict):
            return None, None
        resolved = self._download_remote_sticker(
            hint,
            asset_role=asset_role,
            file_name=file_name,
        )
        if resolved is None:
            return None, None
        path = Path(resolved)
        if not path.exists() or not path.is_file():
            return None, None
        return path.resolve(), "sticker_remote_download"

    def _resolve_remote_from_public_payload(
        self,
        request_data: dict[str, Any],
        payload: dict[str, Any] | None,
        *,
        action: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path | None:
        if not isinstance(payload, dict):
            return None
        asset_type = str(request_data.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return None
        remote_url = self._public_payload_remote_url(payload)
        if not remote_url:
            return None
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        file_name = (
            str(payload.get("file_name") or "").strip()
            or str(request_data.get("file_name") or "").strip()
            or None
        )
        substep = f"public_token_{action}_remote_url"
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        cached_resolution = self._consume_remote_media_prefetch(cache_key)
        if cached_resolution is not ...:
            if request is not None:
                self._emit_asset_substep_trace(
                    trace_callback,
                    request,
                    stage="done",
                    substep=substep,
                    timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                    status="cached_ok" if cached_resolution else "cached_error",
                    detail=resolved_remote_url,
                )
            if cached_resolution:
                cached_path = Path(cached_resolution)
                if cached_path.exists() and cached_path.is_file():
                    return cached_path.resolve()
                self._drop_remote_prefetch_result(cache_key)
            return None
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="start",
                substep=substep,
                timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                detail=resolved_remote_url,
            )
        started = monotonic()
        resolved = self._download_remote_media(
            asset_type=asset_type,
            file_name=file_name,
            hint={"url": resolved_remote_url},
        )
        elapsed_s = monotonic() - started
        self._store_remote_prefetch_result(cache_key, resolved)
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                status="ok" if resolved is not None else "error",
                elapsed_s=elapsed_s,
                detail=resolved_remote_url,
            )
            self._log_remote_substep_outcome(
                request=request,
                substep=substep,
                status="ok" if resolved is not None else "error",
                timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                elapsed_s=elapsed_s,
                detail=resolved_remote_url,
            )
        if resolved is None:
            return None
        path = Path(resolved)
        if not path.exists() or not path.is_file():
            return None
        return path.resolve()

    def _resolve_prefetched_remote_from_payload(
        self,
        request_data: dict[str, Any],
        *,
        action: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path | None:
        asset_type = str(request_data.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return None
        remote_url = str(request_data.get("remote_url") or request_data.get("url") or "").strip()
        if not remote_url:
            return None
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        substep = f"public_token_{action}_remote_url_prefetch"
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        prefetched_resolution = self._peek_remote_media_prefetch(cache_key)
        if prefetched_resolution is ...:
            return None
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                status="cached_ok" if prefetched_resolution else "cached_error",
                detail=resolved_remote_url,
            )
        if not prefetched_resolution:
            return None
        cached_path = Path(prefetched_resolution)
        if not cached_path.exists() or not cached_path.is_file():
            self._drop_remote_prefetch_result(cache_key)
            return None
        return cached_path.resolve()

    def _resolve_prefetched_remote_url(
        self,
        request_data: dict[str, Any],
        *,
        substep: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, bool]:
        asset_type = str(request_data.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return None, False
        remote_url = str(request_data.get("remote_url") or request_data.get("url") or "").strip()
        if not remote_url:
            return None, False
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None, False
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        prefetched_resolution = self._peek_remote_media_prefetch(cache_key)
        if prefetched_resolution is ...:
            return None, False
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                status="cached_ok" if prefetched_resolution else "cached_error",
                detail=resolved_remote_url,
            )
        if not prefetched_resolution:
            return None, True
        cached_path = Path(prefetched_resolution)
        if not cached_path.exists() or not cached_path.is_file():
            self._drop_remote_prefetch_result(cache_key)
            return None, True
        return cached_path.resolve(), True

    def _schedule_remote_media_prefetch(
        self,
        *,
        request: dict[str, Any],
        request_data: dict[str, Any],
        payload: dict[str, Any] | None,
    ) -> None:
        if not isinstance(payload, dict):
            return
        asset_type = str(request_data.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return
        remote_url = str(payload.get("remote_url") or payload.get("url") or "").strip()
        if not remote_url:
            return
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return
        file_name = (
            str(payload.get("file_name") or "").strip()
            or str(request_data.get("file_name") or "").strip()
            or None
        )
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        future, created = self._ensure_remote_media_future(
            asset_type=asset_type,
            file_name=file_name,
            resolved_remote_url=resolved_remote_url,
        )
        if future is None or not created:
            return

    def _schedule_request_remote_prefetch(
        self,
        request: dict[str, Any],
    ) -> None:
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return
        hint = self._request_hint(request)
        if not isinstance(hint, dict):
            return
        if self._resolve_from_source_local_path(request) != (None, None):
            return
        if self._resolve_from_hint_local_path(hint) != (None, None):
            return
        remote_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
        if not remote_url:
            return
        self._schedule_remote_media_prefetch(
            request=request,
            request_data=request,
            payload={
                "asset_type": asset_type,
                "file_name": str(request.get("file_name") or "").strip() or None,
                "remote_url": remote_url,
                "url": remote_url,
            },
        )

    def _schedule_request_direct_public_token_prefetch(
        self,
        request: dict[str, Any],
    ) -> None:
        payload = self._direct_public_token_payload_for_request(request)
        if payload is None:
            return
        self._schedule_public_token_prefetch(
            request=request,
            request_data=request,
            payload=payload,
        )

    def _schedule_public_token_prefetch(
        self,
        *,
        request: dict[str, Any],
        request_data: dict[str, Any],
        payload: dict[str, Any] | None,
    ) -> None:
        if not isinstance(payload, dict):
            return
        asset_type = str(request_data.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return
        action = str(payload.get("public_action") or "").strip().lower()
        token = str(payload.get("public_file_token") or "").strip()
        if not action or not token:
            return
        # If we already have a usable remote URL on hand, the regular remote prefetch
        # path is cheaper and avoids doing an extra local NapCat round-trip.
        remote_url = str(payload.get("remote_url") or payload.get("url") or "").strip()
        if self._should_skip_public_token_prefetch_for_existing_remote_url(remote_url):
            return
        cache_key = self._public_token_prefetch_key(
            request_data=request_data,
            action=action,
            token=token,
        )
        cached_result, future = self._public_token_prefetch_state(cache_key)
        if cached_result is not ...:
            return
        if future is not None and not future.done():
            return
        generation = self._transient_state_generation
        file_name = (
            str(payload.get("file_name") or "").strip()
            or str(request_data.get("file_name") or "").strip()
            or None
        )
        with self._executor_lock:
            public_token_executor = self._public_token_executor
            if public_token_executor is None:
                return
            future = public_token_executor.submit(
                self._prefetch_public_token_task,
                asset_type,
                action,
                token,
                file_name,
                request,
                generation,
            )
        with self._prefetch_state_lock:
            if generation != self._transient_state_generation:
                future.cancel()
                return
            existing_future = self._public_token_prefetch_futures.get(cache_key)
            existing_result = self._public_token_prefetch_cache.get(cache_key, ...)
            if existing_result is not ... or (
                existing_future is not None and not existing_future.done()
            ):
                future.cancel()
                return
            self._public_token_prefetch_futures[cache_key] = future
        future.add_done_callback(
            lambda done_future, cache_key=cache_key, generation=generation: self._finalize_public_token_prefetch_future(
                cache_key=cache_key,
                future=done_future,
                generation=generation,
            )
        )

    def _direct_public_token_payload_for_request(
        self,
        request: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(request, dict):
            return None
        hint = self._request_hint(request)
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type == "image":
            action = "get_image"
        elif asset_type == "speech":
            action = "get_record"
        elif asset_type in {"file", "video"}:
            action = "get_file"
        else:
            return None
        token = str(hint.get("file_id") or "").strip()
        if not token or token.startswith("/"):
            return None
        payload: dict[str, Any] = {
            "asset_type": asset_type,
            "public_action": action,
            "public_file_token": token,
        }
        file_name = str(request.get("file_name") or "").strip()
        if file_name:
            payload["file_name"] = file_name
        remote_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
        if remote_url:
            payload["remote_url"] = remote_url
            payload["url"] = remote_url
        return payload

    def _cached_public_prefetch_result_for_request(
        self,
        request: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        direct_payload = self._direct_public_token_payload_for_request(request)
        if not isinstance(request, dict) or not isinstance(direct_payload, dict):
            return None
        action = str(direct_payload.get("public_action") or "").strip().lower()
        token = str(direct_payload.get("public_file_token") or "").strip()
        if not action or not token:
            return None
        cache_key = self._public_token_prefetch_key(
            request_data=request,
            action=action,
            token=token,
        )
        cached_result, _future = self._public_token_prefetch_state(cache_key)
        if cached_result is not ...:
            return cached_result if isinstance(cached_result, dict) else None
        return None

    def _peek_public_token_prefetch(
        self,
        *,
        request_data: dict[str, Any],
        action: str,
        token: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        cache_key = self._public_token_prefetch_key(
            request_data=request_data,
            action=action,
            token=token,
        )
        cached_result, future = self._public_token_prefetch_state(cache_key)
        if cached_result is not ...:
            self._emit_public_token_prefetch_trace(
                request=request,
                action=action,
                result=cached_result,
                trace_callback=trace_callback,
            )
            return cached_result
        if future is None or not future.done():
            return None
        try:
            cached_result = future.result()
        except Exception:
            cached_result = None
        self._store_public_token_prefetch_result(cache_key, cached_result)
        self._emit_public_token_prefetch_trace(
            request=request,
            action=action,
            result=cached_result,
            trace_callback=trace_callback,
        )
        return cached_result

    def _emit_public_token_prefetch_trace(
        self,
        *,
        request: dict[str, Any] | None,
        action: str,
        result: dict[str, Any] | None,
        trace_callback: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        if request is None or trace_callback is None:
            return
        status = "cached_error"
        if isinstance(result, dict):
            if str(result.get("resolved_path") or "").strip():
                status = "cached_ok"
            elif result.get("remote_attempted"):
                status = "cached_remote_error"
            elif isinstance(result.get("payload"), dict):
                status = "cached_payload"
        self._emit_asset_substep_trace(
            trace_callback,
            request,
            stage="done",
            substep=f"public_token_{action}_prefetch",
            timeout_s=self.PUBLIC_TOKEN_ACTION_TIMEOUT_S,
            status=status,
        )

    def _consume_public_token_prefetch(
        self,
        *,
        request_data: dict[str, Any],
        action: str,
        token: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any] | None:
        cache_key = self._public_token_prefetch_key(
            request_data=request_data,
            action=action,
            token=token,
        )
        cached_result, future = self._public_token_prefetch_state(cache_key)
        if cached_result is not ...:
            self._emit_public_token_prefetch_trace(
                request=request,
                action=action,
                result=cached_result,
                trace_callback=trace_callback,
            )
            return cached_result
        if future is None:
            return None
        try:
            cached_result = future.result(timeout=self.PUBLIC_TOKEN_PREFETCH_WAIT_S)
        except FutureTimeoutError:
            return None
        except Exception:
            cached_result = None
        self._store_public_token_prefetch_result(cache_key, cached_result)
        self._emit_public_token_prefetch_trace(
            request=request,
            action=action,
            result=cached_result,
            trace_callback=trace_callback,
        )
        return cached_result

    def _prefetch_public_token_task(
        self,
        asset_type: str,
        action: str,
        token: str,
        file_name: str | None,
        request: dict[str, Any] | None,
        generation: int,
    ) -> dict[str, Any] | None:
        payload = self._call_public_action_with_token(
            action,
            token,
            file_name=file_name,
            request=request,
        )
        result: dict[str, Any] = {
            "payload": payload if isinstance(payload, dict) else None,
            "resolved_path": None,
            "resolver": None,
            "remote_attempted": False,
        }
        if not isinstance(payload, dict):
            return result
        known_missing_classification = str(payload.get("_known_missing_classification") or "").strip()
        if known_missing_classification:
            result["resolver"] = known_missing_classification
            return result
        self._record_prefetch_feedback("token_payload")
        resolved = self._resolved_path_from_payload(payload)
        if resolved is not None:
            result["resolved_path"] = str(resolved)
            result["resolver"] = f"napcat_public_token_{action}_prefetched"
            self._record_prefetch_feedback("token_resolved")
            return result
        remote_url = self._public_payload_remote_url(payload)
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return result
        result["remote_attempted"] = True
        remote_name = str(payload.get("file_name") or "").strip() or file_name
        downloaded = self._download_remote_media(
            asset_type=asset_type,
            file_name=remote_name or None,
            hint={"url": resolved_remote_url},
        )
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        self._store_remote_prefetch_result(cache_key, downloaded, generation=generation)
        if downloaded is None:
            self._record_prefetch_feedback("token_remote_error")
            return result
        result["resolved_path"] = downloaded
        result["resolver"] = f"napcat_public_token_{action}_remote_url_prefetched"
        self._record_prefetch_feedback("token_remote_ok")
        return result

    @staticmethod
    def _public_token_prefetch_key(
        *,
        request_data: dict[str, Any],
        action: str,
        token: str,
    ) -> tuple[str, ...]:
        hint = request_data.get("download_hint")
        hint_dict = hint if isinstance(hint, dict) else {}
        parent = hint_dict.get("_forward_parent")
        parent_dict = parent if isinstance(parent, dict) else {}

        def _pick(*keys: str) -> str:
            for key in keys:
                value = request_data.get(key)
                if value not in {None, ""}:
                    return str(value).strip()
                value = hint_dict.get(key)
                if value not in {None, ""}:
                    return str(value).strip()
            return ""

        def _pick_parent(*keys: str) -> str:
            for key in keys:
                value = parent_dict.get(key)
                if value not in {None, ""}:
                    return str(value).strip()
            return ""

        return (
            str(request_data.get("asset_type") or "").strip(),
            str(request_data.get("asset_role") or "").strip(),
            str(action or "").strip().lower(),
            str(token or "").strip(),
            str(request_data.get("file_name") or "").strip().lower(),
            str(request_data.get("md5") or "").strip().lower(),
            _pick("message_id_raw"),
            _pick("element_id"),
            _pick("peer_uid"),
            _pick("chat_type_raw"),
            _pick_parent("message_id_raw"),
            _pick_parent("element_id"),
            _pick_parent("peer_uid"),
            _pick_parent("chat_type_raw"),
        )

    def _consume_remote_media_prefetch(
        self,
        cache_key: tuple[str, str],
        *,
        wait_s: float | None = None,
    ) -> str | None | object:
        cached_resolution, future = self._remote_prefetch_state(cache_key)
        if cached_resolution is not ...:
            return cached_resolution
        if future is None:
            return ...
        wait_budget = self.REMOTE_PREFETCH_PEEK_WAIT_S if wait_s is None else max(0.0, float(wait_s))
        if wait_budget <= 0.0 and not future.done():
            return ...
        try:
            cached_resolution = future.result(timeout=wait_budget)
        except FutureTimeoutError:
            return ...
        except Exception:
            cached_resolution = None
        self._store_remote_prefetch_result(cache_key, cached_resolution)
        return cached_resolution

    def _peek_remote_media_prefetch(
        self,
        cache_key: tuple[str, str],
    ) -> str | None | object:
        cached_resolution, future = self._remote_prefetch_state(cache_key)
        if cached_resolution is not ...:
            return cached_resolution
        if future is None or not future.done():
            return ...
        try:
            cached_resolution = future.result()
        except Exception:
            cached_resolution = None
        self._store_remote_prefetch_result(cache_key, cached_resolution)
        return cached_resolution

    def _submit_remote_media_download(
        self,
        *,
        asset_type: str,
        file_name: str | None,
        resolved_remote_url: str,
    ) -> Future[str | None] | None:
        with self._executor_lock:
            loop = self._remote_loop
            if loop is None or loop.is_closed():
                return None
            try:
                return asyncio.run_coroutine_threadsafe(
                    self._download_remote_media_prefetch_task(
                        asset_type=asset_type,
                        file_name=file_name,
                        resolved_remote_url=resolved_remote_url,
                    ),
                    loop,
                )
            except RuntimeError:
                return None

    def _ensure_remote_media_future(
        self,
        *,
        asset_type: str,
        file_name: str | None,
        resolved_remote_url: str,
    ) -> tuple[Future[str | None] | None, bool]:
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        cached_resolution, future = self._remote_prefetch_state(cache_key)
        if cached_resolution is not ...:
            return None, False
        if future is not None and not future.done():
            return future, False
        generation = self._transient_state_generation
        future = self._submit_remote_media_download(
            asset_type=asset_type,
            file_name=file_name,
            resolved_remote_url=resolved_remote_url,
        )
        if future is None:
            return None, False
        with self._prefetch_state_lock:
            if generation != self._transient_state_generation:
                future.cancel()
                return None, False
            existing_future = self._remote_media_resolution_futures.get(cache_key)
            existing_result = self._remote_media_resolution_cache.get(cache_key, ...)
            if existing_result is not ...:
                future.cancel()
                return None, False
            if existing_future is not None and not existing_future.done():
                future.cancel()
                return existing_future, False
            self._update_download_progress(
                cache_key,
                asset_type=asset_type,
                file_name=file_name,
                next_state="queued",
            )
            self._remote_media_resolution_futures[cache_key] = future
        return future, True

    async def _download_remote_media_prefetch_task(
        self,
        asset_type: str,
        file_name: str | None,
        resolved_remote_url: str,
    ) -> str | None:
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        self._update_download_progress(
            cache_key,
            asset_type=asset_type,
            file_name=file_name,
            next_state="active",
        )
        resolved, used_cached_file = await self._download_remote_media_async(
            asset_type=asset_type,
            file_name=file_name,
            hint={"url": resolved_remote_url},
        )
        self._update_download_progress(
            cache_key,
            asset_type=asset_type,
            file_name=file_name,
            next_state=(
                "failed"
                if resolved is None
                else "cached"
                if used_cached_file
                else "completed"
            ),
        )
        self._record_prefetch_feedback("remote_ok" if resolved is not None else "remote_error")
        return resolved

    def _configure_prefetch_pools_for_requests(
        self,
        requests: list[dict[str, Any]],
        *,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        total_prefetchable = 0
        eager_remote_prefetchable = 0
        for request in requests:
            asset_type = str(request.get("asset_type") or "").strip()
            if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
                continue
            hint = self._request_hint(request)
            if not isinstance(hint, dict):
                continue
            if self._resolve_from_source_local_path(request) != (None, None):
                continue
            if self._resolve_from_hint_local_path(hint) != (None, None):
                continue
            total_prefetchable += 1
            if self._resolve_remote_url(str(hint.get("remote_url") or hint.get("url") or "").strip()):
                eager_remote_prefetchable += 1
        feedback = self._prefetch_feedback_snapshot()
        remote_workers = self._compute_remote_media_fetch_workers(
            total_prefetchable=total_prefetchable,
            eager_remote_prefetchable=eager_remote_prefetchable,
            feedback=feedback,
        )
        public_token_workers = self._compute_public_token_prefetch_workers(
            total_prefetchable=total_prefetchable,
            eager_remote_prefetchable=eager_remote_prefetchable,
            feedback=feedback,
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "prefetch_pool_config",
                    "stage": "done",
                    "total_prefetchable": total_prefetchable,
                    "eager_remote_prefetchable": eager_remote_prefetchable,
                    "remote_workers": remote_workers,
                    "public_token_workers": public_token_workers,
                    "feedback": feedback,
                }
            )
        if (
            remote_workers == self._remote_media_fetch_workers
            and public_token_workers == self._public_token_prefetch_workers
        ):
            return
        if self._prefetch_has_inflight_work():
            return
        self._remote_media_fetch_workers = remote_workers
        self._public_token_prefetch_workers = public_token_workers
        self._rebuild_prefetch_executors(wait=False, recreate=True)

    def _create_prefetch_executors(self) -> None:
        if not self._remote_prefetch_runtime_disabled:
            try:
                self._start_remote_download_runtime()
                self._remote_prefetch_runtime_disabled = False
                self._remote_prefetch_runtime_disable_reason = None
            except Exception as exc:
                self._remote_prefetch_runtime_disabled = True
                self._remote_prefetch_runtime_disable_reason = str(exc)
                self._remote_loop = None
                self._remote_loop_thread = None
                self._remote_async_client = None
                self._remote_async_semaphore = None
                self._logger.warning(
                    "remote_media_prefetch_runtime_disabled detail=%s",
                    exc,
                )
        self._public_token_executor = ThreadPoolExecutor(
            max_workers=self._public_token_prefetch_workers,
            thread_name_prefix="qq-public-token",
        )

    def _rebuild_prefetch_executors(self, *, wait: bool, recreate: bool) -> None:
        with self._executor_lock:
            public_token_executor = self._public_token_executor
            self._public_token_executor = None
            if public_token_executor is not None:
                public_token_executor.shutdown(wait=wait, cancel_futures=True)
            self._stop_remote_download_runtime(wait=wait)
            if recreate:
                self._create_prefetch_executors()

    def _start_remote_download_runtime(self) -> None:
        if self._remote_loop is not None and self._remote_loop_thread is not None:
            return
        ready = Event()

        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._remote_loop = loop
            self._remote_async_client = httpx.AsyncClient(
                timeout=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                transport=self._remote_transport,
                trust_env=self._use_system_proxy,
                follow_redirects=True,
            )
            self._remote_async_semaphore = asyncio.Semaphore(self._remote_media_fetch_workers)
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                client = self._remote_async_client
                if client is not None:
                    loop.run_until_complete(client.aclose())
                self._remote_async_client = None
                self._remote_async_semaphore = None
                self._remote_loop = None
                asyncio.set_event_loop(None)
                loop.close()

        thread = Thread(
            target=_runner,
            name="qq-remote-media-async",
            daemon=True,
        )
        self._remote_loop_thread = thread
        thread.start()
        if not ready.wait(timeout=5.0):
            raise RuntimeError("remote media async runtime failed to start")

    def _stop_remote_download_runtime(self, *, wait: bool) -> None:
        loop = self._remote_loop
        thread = self._remote_loop_thread
        if loop is None or thread is None:
            self._remote_loop = None
            self._remote_loop_thread = None
            return
        with self._prefetch_state_lock:
            futures = list(self._remote_media_resolution_futures.values())
        if wait:
            for future in futures:
                with suppress(Exception):
                    future.result(timeout=self.REMOTE_MEDIA_FETCH_TIMEOUT_S + 2.0)
        else:
            for future in futures:
                future.cancel()
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5.0)
        self._remote_loop_thread = None

    @classmethod
    def _compute_remote_media_fetch_workers(
        cls,
        *,
        total_prefetchable: int = 0,
        eager_remote_prefetchable: int = 0,
        feedback: dict[str, int] | None = None,
    ) -> int:
        override = str(os.environ.get("QQ_REMOTE_MEDIA_FETCH_WORKERS") or "").strip()
        if override:
            with suppress(ValueError):
                return max(1, min(16, int(override)))
        cpu_count = os.cpu_count() or 4
        hardware_target = max(4, min(8, cpu_count // 2 or 4))
        queue_pressure = max(total_prefetchable, eager_remote_prefetchable * 2)
        if queue_pressure <= 32:
            workers = min(hardware_target, 4)
        elif queue_pressure <= 160:
            workers = min(hardware_target, max(4, hardware_target - 1))
        else:
            workers = hardware_target
        feedback = feedback or {}
        remote_ok = int(feedback.get("remote_ok", 0))
        remote_error = int(feedback.get("remote_error", 0))
        total_remote = remote_ok + remote_error
        if total_remote >= 12:
            error_ratio = remote_error / float(total_remote)
            if error_ratio >= 0.6:
                workers = max(3, workers - 2)
            elif error_ratio >= 0.35:
                workers = max(4, workers - 1)
            elif remote_ok >= 24 and error_ratio <= 0.1:
                workers = min(hardware_target, workers + 1)
        return max(1, min(16, workers))

    @classmethod
    def _compute_public_token_prefetch_workers(
        cls,
        *,
        total_prefetchable: int = 0,
        eager_remote_prefetchable: int = 0,
        feedback: dict[str, int] | None = None,
    ) -> int:
        override = str(os.environ.get("QQ_PUBLIC_TOKEN_PREFETCH_WORKERS") or "").strip()
        if override:
            with suppress(ValueError):
                return max(1, min(8, int(override)))
        cpu_count = os.cpu_count() or 4
        base = max(2, min(4, cpu_count // 4 or 2))
        token_pressure = max(0, total_prefetchable - eager_remote_prefetchable)
        if token_pressure <= 24:
            workers = min(base, 2)
        elif token_pressure <= 96:
            workers = min(base, 3)
        else:
            workers = min(base, 4)
        feedback = feedback or {}
        token_payload = int(feedback.get("token_payload", 0))
        token_resolved = int(feedback.get("token_resolved", 0))
        token_remote_ok = int(feedback.get("token_remote_ok", 0))
        token_remote_error = int(feedback.get("token_remote_error", 0))
        if token_payload >= 8:
            resolve_ratio = token_resolved / float(max(token_payload, 1))
            remote_error_ratio = token_remote_error / float(max(token_remote_ok + token_remote_error, 1))
            if resolve_ratio < 0.35 or remote_error_ratio >= 0.5:
                workers = max(1, workers - 1)
            elif resolve_ratio >= 0.75 and remote_error_ratio <= 0.15 and token_pressure > 24:
                workers = min(base, workers + 1)
        return max(1, min(8, workers))

    def _prefetch_feedback_snapshot(self) -> dict[str, int]:
        with self._prefetch_feedback_lock:
            return dict(self._prefetch_feedback)

    def _record_prefetch_feedback(self, key: str) -> None:
        with self._prefetch_feedback_lock:
            self._prefetch_feedback[key] = self._prefetch_feedback.get(key, 0) + 1

    def _remote_prefetch_state(
        self,
        cache_key: tuple[str, str],
    ) -> tuple[str | None | object, Future[str | None] | None]:
        with self._prefetch_state_lock:
            return (
                self._remote_media_resolution_cache.get(cache_key, ...),
                self._remote_media_resolution_futures.get(cache_key),
            )

    def _store_remote_prefetch_result(
        self,
        cache_key: tuple[str, str],
        value: str | None,
        *,
        generation: int | None = None,
    ) -> None:
        with self._prefetch_state_lock:
            if generation is not None and generation != self._transient_state_generation:
                return
            self._remote_media_resolution_cache[cache_key] = value
            self._remote_media_resolution_futures.pop(cache_key, None)

    def _drop_remote_prefetch_result(self, cache_key: tuple[str, str]) -> None:
        with self._prefetch_state_lock:
            self._remote_media_resolution_cache.pop(cache_key, None)
            self._remote_media_resolution_futures.pop(cache_key, None)

    def _public_token_prefetch_state(
        self,
        cache_key: tuple[str, str, str, str],
    ) -> tuple[dict[str, Any] | None | object, Future[dict[str, Any] | None] | None]:
        with self._prefetch_state_lock:
            return (
                self._public_token_prefetch_cache.get(cache_key, ...),
                self._public_token_prefetch_futures.get(cache_key),
            )

    def _store_public_token_prefetch_result(
        self,
        cache_key: tuple[str, str, str, str],
        value: dict[str, Any] | None,
        *,
        generation: int | None = None,
        future: Future[dict[str, Any] | None] | None = None,
    ) -> None:
        with self._prefetch_state_lock:
            if generation is not None and generation != self._transient_state_generation:
                return
            self._public_token_prefetch_cache[cache_key] = value
            if future is None:
                self._public_token_prefetch_futures.pop(cache_key, None)
            else:
                current_future = self._public_token_prefetch_futures.get(cache_key)
                if current_future is future:
                    self._public_token_prefetch_futures.pop(cache_key, None)

    def _finalize_public_token_prefetch_future(
        self,
        *,
        cache_key: tuple[str, str, str, str],
        future: Future[dict[str, Any] | None],
        generation: int,
    ) -> None:
        with suppress(Exception):
            result = future.result()
            self._store_public_token_prefetch_result(
                cache_key,
                result,
                generation=generation,
                future=future,
            )
            return
        self._store_public_token_prefetch_result(
            cache_key,
            None,
            generation=generation,
            future=future,
        )

    def _prefetch_has_inflight_work(self) -> bool:
        with self._prefetch_state_lock:
            return any(not future.done() for future in self._remote_media_resolution_futures.values()) or any(
                not future.done() for future in self._public_token_prefetch_futures.values()
            )

    def _download_remote_sticker(
        self,
        hint: dict[str, Any],
        *,
        asset_role: str | None,
        file_name: str | None,
    ) -> str | None:
        remote_url = str(hint.get("remote_url") or "").strip()
        remote_file_name = str(hint.get("remote_file_name") or "").strip()
        cache_dir = self._remote_cache_dir or self._prepare_remote_cache_dir()
        if not remote_url or cache_dir is None:
            return None

        cache_root = cache_dir / "remote_stickers"
        native_name = remote_file_name or file_name or "sticker.gif"
        role_folder = "dynamic" if asset_role == "dynamic" else "static"
        native_path = cache_root / role_folder / self._remote_cache_file_name(
            remote_url,
            file_name=native_name,
        )
        try:
            native_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        if not native_path.exists() or not native_path.is_file() or native_path.stat().st_size <= 0:
            payload = self._download_remote_payload(remote_url)
            if payload is None:
                return None
            if not self._write_bytes(native_path, payload):
                return None
        return str(native_path)

    async def _download_remote_payload_async(self, remote_url: str) -> bytes | None:
        resolved_url = self._resolve_remote_url(remote_url)
        if not resolved_url:
            return None
        client = self._remote_async_client
        semaphore = self._remote_async_semaphore
        if client is None or semaphore is None:
            return None
        try:
            async with semaphore:
                response = await client.get(resolved_url)
        except httpx.HTTPError:
            self._remember_remote_media_failure_reason(resolved_url, "http_error")
            return None
        failure_reason = self._classify_remote_media_response_failure(response)
        if failure_reason is not None:
            self._remember_remote_media_failure_reason(resolved_url, failure_reason)
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPError:
            self._remember_remote_media_failure_reason(resolved_url, "http_error")
            return None
        payload = response.content
        self._remember_remote_media_failure_reason(resolved_url, None)
        return payload if payload else None

    def _download_remote_payload_sync(self, remote_url: str) -> bytes | None:
        resolved_url = self._resolve_remote_url(remote_url)
        if not resolved_url:
            return None
        try:
            with httpx.Client(
                timeout=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                transport=self._remote_transport,
                trust_env=self._use_system_proxy,
                follow_redirects=True,
            ) as client:
                response = client.get(resolved_url)
        except httpx.HTTPError:
            self._remember_remote_media_failure_reason(resolved_url, "http_error")
            return None
        failure_reason = self._classify_remote_media_response_failure(response)
        if failure_reason is not None:
            self._remember_remote_media_failure_reason(resolved_url, failure_reason)
            return None
        try:
            response.raise_for_status()
        except httpx.HTTPError:
            self._remember_remote_media_failure_reason(resolved_url, "http_error")
            return None
        payload = response.content
        self._remember_remote_media_failure_reason(resolved_url, None)
        return payload if payload else None

    @classmethod
    def _is_failed_remote_media_response(cls, response: httpx.Response) -> bool:
        return cls._classify_remote_media_response_failure(response) is not None

    def _download_remote_payload(self, remote_url: str) -> bytes | None:
        loop = self._remote_loop
        if loop is None:
            return self._download_remote_payload_sync(remote_url)
        future = asyncio.run_coroutine_threadsafe(
            self._download_remote_payload_async(remote_url),
            loop,
        )
        try:
            return future.result(timeout=self.REMOTE_MEDIA_FETCH_TIMEOUT_S + 2.0)
        except Exception:
            future.cancel()
            return None

    async def _download_remote_media_async(
        self,
        *,
        asset_type: str,
        file_name: str | None,
        hint: dict[str, Any],
    ) -> tuple[str | None, bool]:
        remote_url = str(hint.get("url") or "").strip()
        cache_dir = self._remote_cache_dir or self._prepare_remote_cache_dir()
        if not remote_url or cache_dir is None:
            return None, False

        remote_name = self._remote_file_name(remote_url, file_name=file_name)
        if not remote_name:
            return None, False
        cache_root = cache_dir / "remote_media" / asset_type
        target_path = cache_root / self._remote_cache_file_name(
            remote_url,
            file_name=remote_name,
        )
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None, False

        if target_path.exists() and target_path.is_file() and target_path.stat().st_size > 0:
            return str(target_path), True

        payload = await self._download_remote_payload_async(remote_url)
        if payload is None:
            return None, False
        if not self._write_bytes(target_path, payload):
            return None, False
        return str(target_path), False

    def _download_remote_media_sync(
        self,
        *,
        asset_type: str,
        file_name: str | None,
        hint: dict[str, Any],
    ) -> str | None:
        remote_url = str(hint.get("url") or "").strip()
        cache_dir = self._remote_cache_dir or self._prepare_remote_cache_dir()
        if not remote_url or cache_dir is None:
            return None

        remote_name = self._remote_file_name(remote_url, file_name=file_name)
        if not remote_name:
            return None
        cache_root = cache_dir / "remote_media" / asset_type
        target_path = cache_root / self._remote_cache_file_name(
            remote_url,
            file_name=remote_name,
        )
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        if target_path.exists() and target_path.is_file() and target_path.stat().st_size > 0:
            return str(target_path)

        payload = self._download_remote_payload_sync(remote_url)
        if payload is None:
            return None
        if not self._write_bytes(target_path, payload):
            return None
        return str(target_path)

    def _download_remote_media(
        self,
        *,
        asset_type: str,
        file_name: str | None,
        hint: dict[str, Any],
    ) -> str | None:
        remote_url = str(hint.get("url") or "").strip()
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        cached_resolution = self._consume_remote_media_prefetch(cache_key, wait_s=0.0)
        if cached_resolution is not ...:
            return cached_resolution
        future, _created = self._ensure_remote_media_future(
            asset_type=asset_type,
            file_name=file_name,
            resolved_remote_url=resolved_remote_url,
        )
        if future is None:
            resolved = self._download_remote_media_sync(
                asset_type=asset_type,
                file_name=file_name,
                hint={"url": resolved_remote_url},
            )
            self._store_remote_prefetch_result(cache_key, resolved)
            return resolved
        try:
            return future.result(timeout=self.REMOTE_MEDIA_FETCH_TIMEOUT_S + 2.0)
        except Exception:
            future.cancel()
            return None

    def _should_skip_eager_remote_prefetch(
        self,
        request: dict[str, Any],
        *,
        old_bucket: tuple[str, str] | None,
    ) -> bool:
        if self._should_prefer_direct_public_token_prefetch_for_placeholder_image(request):
            return True
        if old_bucket is None and not self._should_share_missing_outcome(request):
            return False
        return self._classify_image_placeholder_missing_from_evidence(request) is not None

    def _should_skip_public_token_prefetch_for_existing_remote_url(
        self,
        remote_url: str | None,
    ) -> bool:
        candidate = str(remote_url or "").strip()
        if not self._resolve_remote_url(candidate):
            return False
        return self._asset_location_kind(candidate) != "napcat_local_download"

    def _should_prefer_direct_public_token_prefetch_for_placeholder_image(
        self,
        request: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        if self._classify_image_local_placeholder_missing(request) is None:
            return False
        if not str(hint.get("file_id") or "").strip():
            return False
        remote_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
        remote_kind = self._asset_location_kind(remote_url)
        if remote_kind == "napcat_local_download":
            return True
        return self._is_weak_relative_gchatpic_remote_hint(remote_url)

    def _should_skip_old_bucket(self, old_bucket: tuple[str, str] | None) -> bool:
        if old_bucket is None:
            return False
        if old_bucket in self._old_context_expired_buckets:
            if old_bucket not in self._old_context_skip_logged:
                self._old_context_skip_logged.add(old_bucket)
                self._logger.info(
                    "skip_context_hydration_for_expired_assets bucket=%s",
                    "/".join(old_bucket),
                )
            return True
        failures = self._old_context_failure_buckets.get(old_bucket, 0)
        if failures < self.OLD_CONTEXT_BUCKET_FAILURE_LIMIT:
            return False
        if old_bucket not in self._old_context_skip_logged:
            self._old_context_skip_logged.add(old_bucket)
            self._logger.info(
                "skip_context_hydration_for_old_assets bucket=%s failures=%s",
                "/".join(old_bucket),
                failures,
            )
        return True

    def _note_old_bucket_failure(self, old_bucket: tuple[str, str] | None) -> None:
        if old_bucket is None:
            return
        self._old_context_failure_buckets[old_bucket] = (
            self._old_context_failure_buckets.get(old_bucket, 0) + 1
        )

    def _note_old_bucket_success(self, old_bucket: tuple[str, str] | None) -> None:
        if old_bucket is None:
            return
        self._old_context_expired_buckets.discard(old_bucket)
        self._old_context_failure_buckets.pop(old_bucket, None)
        self._old_context_skip_logged.discard(old_bucket)

    def _note_old_bucket_expired_like(self, old_bucket: tuple[str, str] | None) -> None:
        if old_bucket is None:
            return
        self._old_context_expired_buckets.add(old_bucket)
        self._old_context_failure_buckets[old_bucket] = self.OLD_CONTEXT_BUCKET_FAILURE_LIMIT
        self._old_context_skip_logged.discard(old_bucket)

    def _missing_bucket_resolver(self, old_bucket: tuple[str, str] | None) -> str | None:
        if old_bucket is not None and old_bucket in self._old_context_expired_buckets:
            return "qq_expired_after_napcat"
        return None

    def _classify_missing_from_payload(
        self,
        data: dict[str, Any] | None,
        *,
        old_bucket: tuple[str, str] | None,
        expired_candidate: bool = False,
        request: dict[str, Any] | None = None,
    ) -> str | None:
        if isinstance(request, dict) and str(request.get("asset_type") or "").strip().lower() == "image":
            if self._has_terminal_top_level_image_local_download_dead_signal(
                request,
                payload=data if isinstance(data, dict) else None,
            ):
                return "qq_expired_after_napcat"
            local_placeholder_missing = self._classify_image_placeholder_missing_from_evidence(
                request,
                payload=data if isinstance(data, dict) else None,
            )
            if local_placeholder_missing is not None:
                return local_placeholder_missing
            if self._has_terminal_top_level_image_missing_signal(
                request,
                payload=data if isinstance(data, dict) else None,
            ):
                return "qq_expired_after_napcat"
        if isinstance(request, dict):
            terminal_file_like_missing = self._classify_terminal_top_level_file_like_missing(
                request,
                payload=data if isinstance(data, dict) else None,
            )
            if terminal_file_like_missing is not None:
                return terminal_file_like_missing
        if old_bucket is None and not expired_candidate:
            return None
        if not isinstance(data, dict):
            return None
        if self._resolved_path_from_payload(data) is not None:
            return None
        blank_public_file_missing = self._classify_blank_public_get_file_missing(
            data,
            old_bucket=old_bucket,
            request=request,
        )
        if blank_public_file_missing is not None:
            return blank_public_file_missing
        action = str(data.get("public_action") or "").strip().lower()
        token = str(data.get("public_file_token") or "").strip()
        if not action or not token:
            return None
        remote_url = str(data.get("remote_url") or data.get("url") or "").strip()
        if not remote_url:
            return None
        resolved_url = self._resolve_remote_url(remote_url)
        if not resolved_url:
            return None
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        host = parsed.netloc.lower()
        if "multimedia.nt.qq.com.cn" not in host and "gchat.qpic.cn" not in host:
            return None
        local_placeholder_missing = self._classify_image_placeholder_missing_from_evidence(
            request,
            payload=data,
        )
        if local_placeholder_missing is not None:
            return local_placeholder_missing
        return "qq_expired_after_napcat"

    def _classify_missing_from_public_payload(
        self,
        data: dict[str, Any] | None,
        *,
        old_bucket: tuple[str, str] | None,
        expired_candidate: bool = False,
        request: dict[str, Any] | None = None,
    ) -> str | None:
        if isinstance(request, dict) and str(request.get("asset_type") or "").strip().lower() == "image":
            if self._has_terminal_top_level_image_local_download_dead_signal(
                request,
                payload=data if isinstance(data, dict) else None,
            ):
                return "qq_expired_after_napcat"
            local_placeholder_missing = self._classify_image_placeholder_missing_from_evidence(
                request,
                payload=data if isinstance(data, dict) else None,
            )
            if local_placeholder_missing is not None:
                return local_placeholder_missing
            if self._has_terminal_top_level_image_missing_signal(
                request,
                payload=data if isinstance(data, dict) else None,
            ):
                return "qq_expired_after_napcat"
        if isinstance(request, dict):
            terminal_file_like_missing = self._classify_terminal_top_level_file_like_missing(
                request,
                payload=data if isinstance(data, dict) else None,
            )
            if terminal_file_like_missing is not None:
                return terminal_file_like_missing
        if old_bucket is None and not expired_candidate:
            return None
        if not isinstance(data, dict):
            return None
        if self._resolved_path_from_payload(data) is not None:
            return None
        blank_public_file_missing = self._classify_blank_public_get_file_missing(
            data,
            old_bucket=old_bucket,
            request=request,
        )
        if blank_public_file_missing is not None:
            return blank_public_file_missing
        zero_byte_public_missing = self._classify_zero_byte_public_payload_missing(
            data,
            old_bucket=old_bucket,
            request=request,
        )
        if zero_byte_public_missing is not None:
            return zero_byte_public_missing
        remote_url = self._public_payload_remote_url(data)
        if not remote_url:
            return None
        resolved_url = self._resolve_remote_url(remote_url)
        if not resolved_url:
            return None
        parsed = urlparse(resolved_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        host = parsed.netloc.lower()
        if "multimedia.nt.qq.com.cn" not in host and "gchat.qpic.cn" not in host:
            return None
        local_placeholder_missing = self._classify_image_placeholder_missing_from_evidence(
            request,
            payload=data,
        )
        if local_placeholder_missing is not None:
            return local_placeholder_missing
        return "qq_expired_after_napcat"

    def _classify_blank_public_get_file_missing(
        self,
        data: dict[str, Any] | None,
        *,
        old_bucket: tuple[str, str] | None,
        request: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(data, dict):
            return None
        action = str(data.get("public_action") or "").strip().lower()
        request_asset_type = str(
            (request or {}).get("asset_type") if isinstance(request, dict) else ""
        ).strip().lower()
        payload_asset_type = str(data.get("asset_type") or "").strip().lower()
        effective_asset_type = request_asset_type or payload_asset_type
        if effective_asset_type == "speech":
            if action != "get_record":
                return None
        elif effective_asset_type in {"file", "video"}:
            if action != "get_file":
                return None
        else:
            return None
        payload_file = str(data.get("file") or "").strip()
        remote_url = str(data.get("url") or data.get("remote_url") or "").strip()
        if payload_file:
            return None
        if remote_url:
            parsed = urlparse(remote_url)
            if parsed.scheme.lower() in {"http", "https"}:
                return None
            if Path(remote_url).exists():
                return None
        file_name = str(
            data.get("file_name")
            or ((request or {}).get("file_name") if isinstance(request, dict) else "")
            or ""
        ).strip()
        file_size = str(data.get("file_size") or "").strip()
        file_id = str(data.get("file_id") or "").strip()
        token = str(data.get("public_file_token") or "").strip()
        if not any([file_name, file_size, file_id, token]):
            return None
        if isinstance(request, dict):
            hint = self._request_hint(request)
            if self._has_forward_parent_hint(hint):
                if old_bucket is None and not self._is_forward_expensive_asset(request):
                    return None
        elif old_bucket is None:
            return None
        return "qq_expired_after_napcat"

    def _classify_zero_byte_public_payload_missing(
        self,
        data: dict[str, Any] | None,
        *,
        old_bucket: tuple[str, str] | None,
        request: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(data, dict):
            return None
        action = str(data.get("public_action") or "").strip().lower()
        request_asset_type = str(
            (request or {}).get("asset_type") if isinstance(request, dict) else ""
        ).strip().lower()
        payload_asset_type = str(data.get("asset_type") or "").strip().lower()
        effective_asset_type = request_asset_type or payload_asset_type
        if effective_asset_type == "speech":
            if action != "get_record":
                return None
        elif effective_asset_type in {"file", "video"}:
            if action != "get_file":
                return None
        else:
            return None
        if self._has_live_http_media_url(request, payload=data):
            return None
        if self._zero_byte_local_payload_path(data) is None:
            return None
        file_name = str(
            data.get("file_name")
            or ((request or {}).get("file_name") if isinstance(request, dict) else "")
            or ""
        ).strip()
        file_size = str(data.get("file_size") or "").strip()
        file_id = str(data.get("file_id") or "").strip()
        token = str(data.get("public_file_token") or "").strip()
        if not any([file_name, file_size, file_id, token]):
            return None
        if isinstance(request, dict):
            hint = self._request_hint(request)
            if self._has_forward_parent_hint(hint):
                if old_bucket is None and not self._is_forward_expensive_asset(request):
                    return None
        elif old_bucket is None:
            return None
        return "qq_expired_after_napcat"

    def _should_skip_forward_timeout_storm(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> bool:
        storm_key = self._forward_timeout_storm_key(request, route=route)
        if storm_key is None:
            return False
        return storm_key in self._forward_timeout_storm_open

    def _note_forward_timeout_storm(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> None:
        storm_key = self._forward_timeout_storm_key(request, route=route)
        if storm_key is None:
            return
        failures = self._forward_timeout_storm_counts.get(storm_key, 0) + 1
        self._forward_timeout_storm_counts[storm_key] = failures
        if failures >= self.FORWARD_TIMEOUT_STORM_LIMIT:
            self._forward_timeout_storm_open.add(storm_key)

    def _note_forward_timeout_storm_success(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> None:
        storm_key = self._forward_timeout_storm_key(request, route=route)
        if storm_key is None:
            return
        self._forward_timeout_storm_counts.pop(storm_key, None)
        self._forward_timeout_storm_open.discard(storm_key)

    def _should_skip_forward_parent_expensive_timeout(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> bool:
        parent_key = self._forward_parent_expensive_timeout_key(request, route=route)
        if parent_key is None:
            return False
        return parent_key in self._forward_parent_public_action_timeout_cache

    def _note_forward_parent_expensive_timeout(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> None:
        parent_key = self._forward_parent_expensive_timeout_key(request, route=route)
        if parent_key is None:
            return
        self._forward_parent_public_action_timeout_cache.add(parent_key)

    def _note_forward_parent_expensive_timeout_success(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> None:
        parent_key = self._forward_parent_expensive_timeout_key(request, route=route)
        if parent_key is None:
            return
        self._forward_parent_public_action_timeout_cache.discard(parent_key)

    def _forward_timeout_storm_key(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> tuple[str, ...] | None:
        if not isinstance(request, dict):
            return None
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return None
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"image", "file", "video", "speech"}:
            return None
        raw_timestamp = request.get("timestamp_ms")
        if not isinstance(raw_timestamp, (int, float)):
            return None
        try:
            asset_dt = datetime.fromtimestamp(float(raw_timestamp) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        asset_age = datetime.now(timezone.utc) - asset_dt
        if asset_age < timedelta(days=self.FORWARD_TIMEOUT_STORM_MIN_AGE_DAYS):
            return None
        normalized_route = self._forward_timeout_storm_route_group(
            route=route,
            asset_age=asset_age,
        )
        if asset_age >= timedelta(days=self.FORWARD_TIMEOUT_STORM_GLOBAL_MIN_AGE_DAYS):
            age_bucket = f"{self.FORWARD_TIMEOUT_STORM_GLOBAL_MIN_AGE_DAYS}d_plus"
        else:
            age_bucket = asset_dt.strftime("%Y-%m")
        return (
            "forward_timeout_storm",
            normalized_route,
            asset_type,
            str(request.get("asset_role") or "").strip().lower(),
            age_bucket,
        )

    @staticmethod
    def _forward_timeout_storm_route_group(
        *,
        route: str,
        asset_age: timedelta,
    ) -> str:
        normalized = str(route or "").strip().lower()
        if asset_age >= timedelta(days=NapCatMediaDownloader.FORWARD_TIMEOUT_STORM_GLOBAL_MIN_AGE_DAYS):
            if normalized in {
                "public_token_get_image",
                "public_token_get_file",
                "public_token_get_record",
                "forward_context_materialize",
                "direct_file_id_get_file",
            }:
                return "forward_expensive"
            if normalized == "forward_context_metadata":
                return "forward_meta"
        return normalized

    def _forward_parent_expensive_timeout_key(
        self,
        request: dict[str, Any] | None,
        *,
        route: str,
    ) -> tuple[str, ...] | None:
        if not isinstance(request, dict):
            return None
        normalized_route = str(route or "").strip().lower()
        if normalized_route not in {"public_token_get_file", "forward_context_materialize"}:
            return None
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return None
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"video", "file"}:
            return None
        raw_timestamp = request.get("timestamp_ms")
        if not isinstance(raw_timestamp, (int, float)):
            return None
        try:
            asset_dt = datetime.fromtimestamp(float(raw_timestamp) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        if datetime.now(timezone.utc) - asset_dt < timedelta(
            days=self.FORWARD_TIMEOUT_STORM_MIN_AGE_DAYS
        ):
            return None
        parent = hint.get("_forward_parent")
        assert isinstance(parent, dict)
        return (
            "forward_parent_expensive_timeout",
            str(parent.get("message_id_raw") or "").strip(),
            str(parent.get("element_id") or "").strip(),
            str(parent.get("peer_uid") or "").strip(),
            str(parent.get("chat_type_raw") or "").strip(),
            asset_type,
            str(request.get("asset_role") or "").strip().lower(),
        )

    def _classify_forward_missing(
        self,
        request: dict[str, Any],
        *,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        if str(request.get("asset_type") or "").strip() != "image":
            return None
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return None
        source_path = str(request.get("source_path") or "").strip()
        if source_path and not (
            self._looks_like_stale_local_media_path(source_path)
            or self._looks_like_zero_byte_local_media_path(source_path)
        ):
            return None
        local_placeholder_missing = self._classify_image_placeholder_missing_from_evidence(
            request,
            payload=payload,
        )
        if local_placeholder_missing is not None:
            return local_placeholder_missing
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return None
        if self._has_terminal_forward_image_missing_signal(
            request,
            payload=payload,
        ):
            return "qq_expired_after_napcat"
        return None

    def _has_terminal_forward_image_missing_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return False
        remote_terminal = self._has_terminal_forward_image_remote_evidence(
            request,
            payload=payload,
        )
        public_failed = self._public_action_failed(
            request,
            action="get_image",
            payload=payload,
        )
        has_public_recovery_handle = self._has_forward_image_public_recovery_handle(
            request,
            payload=payload,
        )
        has_local_media_hint = self._has_forward_local_media_hint(
            request,
            payload=payload,
        )
        local_broken = self._has_stale_forward_local_media_hint(
            request,
            payload=payload,
        ) or self._has_zero_byte_forward_local_media_hint(
            request,
            payload=payload,
        )
        metadata_terminal = self._has_terminal_forward_image_route_signal(
            request,
            materialize=False,
        )
        metadata_local_missing = self._has_terminal_forward_image_metadata_local_missing_signal(
            request,
            payload=payload,
        )
        if remote_terminal and metadata_terminal:
            return True
        if metadata_local_missing:
            return True
        if remote_terminal and public_failed:
            return True
        if public_failed and local_broken:
            return True
        if public_failed and metadata_terminal:
            return True
        if remote_terminal and local_broken and not has_public_recovery_handle:
            return True
        if remote_terminal and not has_public_recovery_handle and not has_local_media_hint:
            return True
        return False

    def _has_terminal_forward_image_metadata_local_missing_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict) or not isinstance(payload, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        local_candidate = str(payload.get("file") or payload.get("url") or "").strip()
        if not local_candidate:
            return False
        if self._resolve_remote_url(local_candidate):
            return False
        if Path(local_candidate).exists():
            return False
        if not (
            self._has_stale_forward_local_media_hint(request, payload=payload)
            or self._has_zero_byte_forward_local_media_hint(request, payload=payload)
        ):
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        return True

    def _has_terminal_forward_image_remote_evidence(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return False
        candidate_reasons = self._forward_remote_failure_reasons(
            request,
            payload=payload,
        )
        original_reason = candidate_reasons.get("original") or ""
        projected_reason = candidate_reasons.get("projected") or ""
        return (
            original_reason == "expired_remote"
            and projected_reason == "unsupported_local_download"
        )

    def _has_forward_image_public_recovery_handle(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return False
        return self._has_image_public_recovery_handle(request, payload=payload)

    def _has_image_public_recovery_handle(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        token = str(
            (payload or {}).get("public_file_token") if isinstance(payload, dict) else ""
        ).strip()
        if token:
            return True
        return bool(
            str(
                request.get("public_file_token")
                or hint.get("public_file_token")
                or hint.get("file_id")
                or ""
            ).strip()
        )

    def _has_terminal_top_level_image_missing_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if self._has_terminal_top_level_image_local_download_dead_signal(
            request,
            payload=payload,
        ):
            return True
        if not isinstance(request, dict) or not isinstance(payload, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        has_public_recovery_handle = self._has_image_public_recovery_handle(
            request,
            payload=payload,
        )
        has_local_broken_hint = self._has_stale_forward_local_media_hint(
            request,
            payload=payload,
        ) or self._has_zero_byte_forward_local_media_hint(
            request,
            payload=payload,
        )
        if not (has_public_recovery_handle and has_local_broken_hint):
            return False
        if self._has_terminal_image_remote_evidence(request, payload=payload):
            return True
        candidate_reasons = self._request_remote_failure_reasons(
            request,
            payload=payload,
        )
        return "expired_remote" in set(candidate_reasons.values())

    def _classify_image_placeholder_missing_from_evidence(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        local_placeholder_missing = self._classify_image_local_placeholder_missing(request)
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return None
        if self._has_failed_forward_remote_url(request, payload=payload):
            if local_placeholder_missing is not None:
                return local_placeholder_missing
        if self._public_action_failed(
            request,
            action="get_image",
            payload=payload,
        ):
            if local_placeholder_missing is not None:
                return local_placeholder_missing
        if self._public_action_remote_failed(
            request,
            action="get_image",
            payload=payload,
        ):
            if local_placeholder_missing is not None:
                return local_placeholder_missing
        if self._has_terminal_top_level_image_context_placeholder_signal(
            request,
            payload=payload,
        ):
            return local_placeholder_missing or "qq_not_downloaded_local_placeholder"
        if local_placeholder_missing is None:
            return None
        return None

    def _has_terminal_image_remote_evidence(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        candidate_reasons = self._request_remote_failure_reasons(
            request,
            payload=payload,
        )
        reason_values = set(candidate_reasons.values())
        return (
            "expired_remote" in reason_values
            and "unsupported_local_download" in reason_values
        )

    def _has_terminal_top_level_image_local_download_dead_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        remote_hint = str(
            (payload or {}).get("remote_url")
            or hint.get("remote_url")
            or hint.get("url")
            or ""
        ).strip()
        if not (
            self._is_relative_local_download_hint(remote_hint)
            or self._asset_location_kind(remote_hint) == "napcat_local_download"
        ):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        if not (
            bool(request.get("_context_hydration_yielded_no_local_path"))
            or self._has_stale_forward_local_media_hint(request, payload=payload)
            or self._has_zero_byte_forward_local_media_hint(request, payload=payload)
        ):
            return False
        direct_payload = self._direct_public_token_payload_for_request(request)
        if not self._public_action_failed(
            request,
            action="get_image",
            payload=direct_payload if isinstance(direct_payload, dict) else payload,
        ):
            return False
        return True

    def _has_terminal_forward_image_route_signal(
        self,
        request: dict[str, Any] | None,
        *,
        materialize: bool,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return False
        return (
            self._forward_context_timed_out(request, materialize=materialize)
            or
            self._forward_context_empty(request, materialize=materialize)
            or self._forward_context_error(request, materialize=materialize)
            or self._forward_context_unavailable(request, materialize=materialize)
        )

    def _should_attempt_targeted_forward_image_materialize(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        return False

    def _should_prefer_targeted_forward_image_materialize_before_metadata(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        return False

    def _resolve_from_forward_payload_candidate(
        self,
        request: dict[str, Any],
        *,
        payload: dict[str, Any] | None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        if not isinstance(payload, dict):
            return None
        asset_type = str(request.get("asset_type") or "").strip().lower()
        fast_resolved = self._resolve_from_fast_payload(payload)
        if fast_resolved != (None, None):
            return fast_resolved
        if asset_type == "image":
            classified_forward_missing = self._classify_forward_missing(
                request,
                payload=payload,
            )
            if classified_forward_missing is not None:
                self._emit_missing_classification_trace(
                    trace_callback,
                    request,
                    substep="forward_missing_classification",
                    classification=classified_forward_missing,
                )
                return None, classified_forward_missing
        forward_remote_url = self._resolve_from_forward_remote_url(
            payload,
            request=request,
            trace_callback=trace_callback,
        )
        if forward_remote_url != (None, None):
            return forward_remote_url
        public_resolved = self._resolve_from_public_token(
            payload,
            request=request,
            trace_callback=trace_callback,
        )
        if public_resolved not in {None, (None, None)}:
            return public_resolved
        return None

    def _has_failed_forward_remote_url(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return False
        if asset_type == "image" and self._has_forward_parent_hint(self._request_hint(request)):
            candidates = self._iter_forward_remote_url_candidates(
                request,
                payload=payload,
            )
            if not candidates:
                return False
            for _, candidate_url in candidates:
                if not candidate_url:
                    continue
                normalized_url = self._normalized_match_url(candidate_url)
                cache_key = (asset_type, normalized_url)
                prefetched_resolution = self._peek_remote_media_prefetch(cache_key)
                has_failure_reason = normalized_url in self._remote_media_failure_reasons
                if prefetched_resolution is ... and not has_failure_reason:
                    return False
                if prefetched_resolution not in {None, ...}:
                    return False
            return True
        hint = self._request_hint(request)
        remote_url = str(
            (payload or {}).get("remote_url")
            or (payload or {}).get("url")
            or hint.get("remote_url")
            or hint.get("url")
            or ""
        ).strip()
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return False
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        prefetched_resolution = self._peek_remote_media_prefetch(cache_key)
        if prefetched_resolution is ...:
            return self._normalized_match_url(resolved_remote_url) in self._remote_media_failure_reasons
        return prefetched_resolution is None

    def _forward_remote_failure_reasons(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for label, candidate_url in self._iter_forward_remote_url_candidates(
            request,
            payload=payload,
        ):
            if not candidate_url:
                continue
            normalized_url = self._normalized_match_url(candidate_url)
            reason = self._remote_media_failure_reasons.get(normalized_url)
            if reason:
                reasons[label] = reason
        return reasons

    def _request_remote_failure_reasons(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for label, candidate_url in self._iter_request_remote_url_candidates(
            request,
            payload=payload,
        ):
            normalized_url = self._normalized_match_url(candidate_url)
            reason = self._remote_media_failure_reasons.get(normalized_url)
            if reason:
                reasons[label] = reason
        return reasons

    def _classify_old_forward_expensive_missing(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
        require_timeout_signal: bool = False,
        failure_signal_mode: str = "strict",
    ) -> str | None:
        if not self._is_forward_expensive_asset(request):
            return None
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return None
        normalized_mode = str(failure_signal_mode or "strict").strip().lower()
        if require_timeout_signal:
            normalized_mode = "strict"
        if normalized_mode == "strict":
            if not self._is_forward_expensive_terminal_candidate(request, payload=payload):
                return None
            if not self._has_old_forward_timeout_signal(request, payload=payload):
                return None
        elif normalized_mode == "terminal":
            if not self._has_forward_expensive_terminal_signal(
                request,
                payload=payload,
                allow_timeout=self._is_forward_expensive_terminal_candidate(request, payload=payload),
            ):
                return None
        elif normalized_mode not in {"", "none"}:
            raise ValueError(f"unsupported failure_signal_mode: {failure_signal_mode}")
        direct_file_identifier = self._direct_forward_file_identifier(
            request,
            payload=payload,
        )
        if direct_file_identifier.startswith("/") and not self._has_failed_direct_forward_file_identifier(
            request,
        ):
            return None
        has_broken_local_hint = self._has_stale_forward_local_media_hint(
            request,
            payload=payload,
        ) or self._has_zero_byte_forward_local_media_hint(
            request,
            payload=payload,
        )
        if not has_broken_local_hint:
            if not self._allow_old_forward_missing_without_stale_local_hint(
                request,
                payload=payload,
                failure_signal_mode=normalized_mode,
            ):
                return None
        return "qq_expired_after_napcat"

    def _has_old_forward_timeout_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if self._fast_forward_context_route_disabled:
            return True
        if self._forward_context_timed_out(request, materialize=False):
            return True
        if self._forward_context_timed_out(request, materialize=True):
            return True
        if self._forward_context_unavailable(request, materialize=False):
            return True
        if self._forward_context_unavailable(request, materialize=True):
            return True
        asset_type = str(request.get("asset_type") or "").strip().lower()
        action = "get_record" if asset_type == "speech" else "get_file"
        if self._public_action_timed_out(
            request,
            action=action,
            payload=payload,
        ):
            return True
        if self._has_failed_direct_forward_file_identifier(request):
            return True
        return self._should_skip_forward_timeout_storm(
            request,
            route=f"public_token_{action}",
        ) or self._should_skip_forward_timeout_storm(
            request,
            route="forward_context_materialize",
        ) or self._should_skip_forward_timeout_storm(
            request,
            route="direct_file_id_get_file",
        )

    def _has_forward_expensive_terminal_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
        allow_timeout: bool = False,
    ) -> bool:
        if allow_timeout and self._has_old_forward_timeout_signal(request, payload=payload):
            return True
        if self._forward_context_empty(request, materialize=False):
            return True
        if self._forward_context_error(request, materialize=False):
            return True
        if self._forward_context_unavailable(request, materialize=False):
            return True
        if self._forward_context_empty(request, materialize=True):
            return True
        if self._forward_context_error(request, materialize=True):
            return True
        if self._has_terminal_forward_metadata_local_missing_signal(
            request,
            payload=payload,
        ):
            return True
        if self._has_zero_byte_forward_local_media_hint(request, payload=payload):
            return True
        if self._has_blank_forward_public_payload(request, payload=payload):
            return True
        if self._has_terminal_public_action_failure(
            request,
            payload=payload,
        ):
            return True
        if self._has_terminal_direct_forward_file_identifier_failure(
            request,
            payload=payload,
        ):
            return True
        return False

    def _has_blank_forward_public_payload(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(payload, dict):
            return False
        if not self._is_forward_expensive_asset(request):
            return False
        action = str(payload.get("public_action") or "").strip().lower()
        request_asset_type = str(
            (request or {}).get("asset_type") if isinstance(request, dict) else ""
        ).strip().lower()
        payload_asset_type = str(payload.get("asset_type") or "").strip().lower()
        effective_asset_type = request_asset_type or payload_asset_type
        if effective_asset_type == "speech":
            if action != "get_record":
                return False
        elif effective_asset_type in {"video", "file"}:
            if action != "get_file":
                return False
        else:
            return False
        if str(payload.get("file") or "").strip():
            return False
        remote_url = str(payload.get("url") or payload.get("remote_url") or "").strip()
        if remote_url:
            parsed = urlparse(remote_url)
            if parsed.scheme.lower() in {"http", "https"}:
                return False
            if Path(remote_url).exists():
                return False
        file_name = str(
            payload.get("file_name")
            or ((request or {}).get("file_name") if isinstance(request, dict) else "")
            or ""
        ).strip()
        file_size = str(payload.get("file_size") or "").strip()
        file_id = str(payload.get("file_id") or "").strip()
        token = str(payload.get("public_file_token") or "").strip()
        return bool(file_name or file_size or file_id or token)

    def _has_terminal_public_action_failure(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type == "speech":
            action = "get_record"
        elif asset_type in {"file", "video"}:
            action = "get_file"
        elif asset_type == "image":
            action = "get_image"
        else:
            return False
        effective_token = str(
            (payload or {}).get("public_file_token") if isinstance(payload, dict) else ""
        ).strip()
        cache_key = (action, effective_token)
        cached_payload = (
            self._public_token_action_outcomes.get(cache_key)
            if effective_token
            else None
        )
        if isinstance(cached_payload, dict):
            if str(cached_payload.get("_known_missing_classification") or "").strip():
                return True
            old_bucket = self._old_context_bucket(asset_type, request)
            if asset_type == "image":
                classified_missing = self._classify_missing_from_public_payload(
                    cached_payload,
                    old_bucket=old_bucket,
                    expired_candidate=self._is_forward_expensive_asset(request),
                    request=request,
                )
                if classified_missing is not None:
                    return True
            else:
                if self._classify_blank_public_get_file_missing(
                    cached_payload,
                    old_bucket=old_bucket,
                    request=request,
                ) is not None:
                    return True
                if self._classify_zero_byte_public_payload_missing(
                    cached_payload,
                    old_bucket=old_bucket,
                    request=request,
                ) is not None:
                    return True
            if self._has_blank_forward_public_payload(request, payload=cached_payload):
                return True
        if asset_type == "image":
            return self._public_action_failed(
                request,
                action=action,
                payload=payload,
            )
        return False

    def _has_failed_direct_forward_file_identifier(
        self,
        request: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if self._request_key(request) in self._direct_file_id_timeout_cache:
            return True
        return self._should_skip_forward_timeout_storm(
            request,
            route="direct_file_id_get_file",
        )

    def _classify_blank_direct_file_id_missing(
        self,
        request: dict[str, Any] | None,
        payload: dict[str, Any] | None,
    ) -> str | None:
        if not isinstance(request, dict):
            return None
        if not self._should_classify_blank_direct_file_id_as_terminal(
            request,
            payload=payload,
        ):
            return None
        file_id = self._direct_forward_file_identifier(request, payload=payload)
        if not file_id.startswith("/"):
            return None
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return None
        if payload is None:
            return "qq_expired_after_napcat"
        if not isinstance(payload, dict):
            return None
        if self._resolved_path_from_payload(payload) is not None:
            return None
        remote_url = str(payload.get("url") or payload.get("remote_url") or "").strip()
        if remote_url:
            parsed = urlparse(remote_url)
            if parsed.scheme.lower() in {"http", "https"}:
                return None
            if Path(remote_url).exists():
                return None
        file_name = str(payload.get("file_name") or request.get("file_name") or "").strip()
        file_size = str(payload.get("file_size") or "").strip()
        payload_file_id = str(payload.get("file_id") or "").strip()
        return "qq_expired_after_napcat" if (file_name or file_size or payload_file_id) else None

    def _should_skip_context_hydration_for_request(
        self,
        request: dict[str, Any] | None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        if asset_type == "image":
            if self._classify_image_placeholder_missing_from_evidence(request) is not None:
                return True
            direct_payload = self._direct_public_token_payload_for_request(request)
            if direct_payload is None:
                return False
            if not self._is_weak_relative_gchatpic_remote_hint(
                str(hint.get("remote_url") or hint.get("url") or "").strip()
            ):
                return False
            return self._has_terminal_top_level_image_missing_signal(
                request,
                payload=direct_payload,
            )
        if asset_type in {"file", "video"}:
            return self._classify_terminal_file_like_missing_from_request_state(request) is not None
        return False

    def _classify_terminal_missing_from_request_state(
        self,
        request: dict[str, Any] | None,
    ) -> str | None:
        if not isinstance(request, dict):
            return None
        terminal_top_level_image_missing = self._classify_terminal_top_level_image_missing_from_request_state(
            request,
        )
        if terminal_top_level_image_missing is not None:
            return terminal_top_level_image_missing
        terminal_file_like_missing = self._classify_terminal_file_like_missing_from_request_state(
            request,
        )
        if terminal_file_like_missing is not None:
            return terminal_file_like_missing
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type != "speech":
            return None
        direct_payload = self._direct_public_token_payload_for_request(request)
        if direct_payload is None:
            return None
        return self._classify_missing_from_public_payload(
            direct_payload,
            old_bucket=self._old_context_bucket(asset_type, request),
            request=request,
        )

    @staticmethod
    def _is_weak_relative_gchatpic_remote_hint(remote_url: str | None) -> bool:
        candidate = str(remote_url or "").strip()
        if not candidate:
            return False
        lowered = candidate.casefold()
        return lowered.startswith("/gchatpic_new/") or lowered.startswith("gchatpic_new/")

    def _classify_terminal_file_like_missing_from_request_state(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(request, dict):
            return None
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return self._classify_old_forward_expensive_missing(
                request,
                failure_signal_mode="terminal",
            )
        cached_context_payload = request.get("_context_payload")
        terminal_payload = payload if isinstance(payload, dict) else (
            cached_context_payload if isinstance(cached_context_payload, dict) else None
        )
        return self._classify_terminal_top_level_file_like_missing(
            request,
            payload=terminal_payload or self._direct_public_token_payload_for_request(request),
        )

    def _classify_terminal_top_level_image_missing_from_request_state(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(request, dict):
            return None
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return None
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return None
        cached_context_payload = request.get("_context_payload")
        context_payload = payload if isinstance(payload, dict) else (
            cached_context_payload if isinstance(cached_context_payload, dict) else None
        )
        direct_payload = self._direct_public_token_payload_for_request(request)
        prefetched_public = self._cached_public_prefetch_result_for_request(request)
        prefetched_public_payload = (
            prefetched_public.get("payload") if isinstance(prefetched_public, dict) else None
        )
        candidate_payloads: list[dict[str, Any]] = []
        for candidate in (context_payload, prefetched_public_payload, direct_payload):
            if isinstance(candidate, dict) and candidate not in candidate_payloads:
                candidate_payloads.append(candidate)
        for candidate_payload in candidate_payloads:
            if self._has_terminal_top_level_image_local_download_dead_signal(
                request,
                payload=candidate_payload,
            ):
                return "qq_expired_after_napcat"
        for candidate_payload in candidate_payloads:
            if self._has_terminal_top_level_image_missing_signal(
                request,
                payload=candidate_payload,
            ):
                return "qq_expired_after_napcat"
        for candidate_payload in candidate_payloads:
            local_placeholder_missing = self._classify_image_placeholder_missing_from_evidence(
                request,
                payload=candidate_payload,
            )
            if local_placeholder_missing is not None:
                return local_placeholder_missing
        return None

    def _classify_terminal_top_level_file_like_missing(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> str | None:
        if not self._has_terminal_top_level_file_like_missing_signal(
            request,
            payload=payload,
        ):
            return None
        return "qq_expired_after_napcat"

    def _has_terminal_top_level_file_like_missing_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"file", "video", "speech"}:
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        old_bucket = self._old_context_bucket(asset_type, request)
        if self._classify_blank_public_get_file_missing(
            payload,
            old_bucket=old_bucket,
            request=request,
        ) is not None:
            return True
        if self._classify_zero_byte_public_payload_missing(
            payload,
            old_bucket=old_bucket,
            request=request,
        ) is not None:
            return True
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        if (
            bool(request.get("_direct_file_id_not_found"))
            and isinstance(payload, dict)
            and self._has_terminal_top_level_file_like_context_missing_signal(
                request,
                payload=payload,
            )
        ):
            return True
        if not (
            self._has_stale_forward_local_media_hint(request, payload=payload)
            or self._has_zero_byte_forward_local_media_hint(request, payload=payload)
        ):
            return False
        if isinstance(payload, dict) and self._resolved_path_from_payload(payload) is not None:
            return False
        direct_file_id = str(
            (payload or {}).get("file_id") if isinstance(payload, dict) else ""
        ).strip() or str(hint.get("file_id") or "").strip()
        has_direct_identifier = direct_file_id.startswith("/")
        has_public_token = bool(
            str(
                (payload or {}).get("public_file_token") if isinstance(payload, dict) else ""
            ).strip()
            or str(hint.get("file_id") or "").strip()
        )
        if not (has_direct_identifier or has_public_token):
            return False
        if self._has_terminal_public_action_failure(request, payload=payload):
            return True
        if asset_type == "speech" and self._public_action_failed(
            request,
            action="get_record",
            payload=payload,
        ):
            return True
        return False

    def _has_terminal_top_level_file_like_context_missing_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict) or not isinstance(payload, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"file", "video"}:
            return False
        if self._has_forward_parent_hint(self._request_hint(request)):
            return False
        if not bool(request.get("_direct_file_id_not_found")):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        direct_file_id = self._direct_forward_file_identifier(request, payload=payload)
        if not direct_file_id.startswith("/"):
            return False
        remote_candidate = str(payload.get("url") or payload.get("remote_url") or "").strip()
        if remote_candidate and self._resolve_remote_url(remote_candidate):
            return False
        if remote_candidate and Path(remote_candidate).exists():
            return False
        return bool(
            str(payload.get("file_name") or request.get("file_name") or "").strip()
            or str(payload.get("file_size") or "").strip()
            or str(payload.get("file_id") or "").strip()
            or str(payload.get("public_file_token") or "").strip()
        )

    def _has_terminal_top_level_image_context_placeholder_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict) or not isinstance(payload, dict):
            return False
        if str(request.get("asset_type") or "").strip().lower() != "image":
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        remote_hint = str(hint.get("remote_url") or hint.get("url") or "").strip()
        if not (
            self._is_weak_relative_gchatpic_remote_hint(remote_hint)
            or self._is_relative_local_download_hint(remote_hint)
            or self._asset_location_kind(remote_hint) == "napcat_local_download"
        ):
            return False
        context_local_candidate = str(payload.get("file") or payload.get("url") or "").strip()
        context_failed_to_materialize_local = bool(request.get("_context_hydration_yielded_no_local_path"))
        if not context_failed_to_materialize_local and context_local_candidate:
            context_failed_to_materialize_local = (
                self._asset_location_kind(context_local_candidate)
                in {"missing_local", "stale_local", "zero_byte_local"}
            )
        if not context_failed_to_materialize_local:
            return False
        return self._has_stale_forward_local_media_hint(
            request,
            payload=payload,
        ) or self._has_zero_byte_forward_local_media_hint(
            request,
            payload=payload,
        )

    def _should_classify_direct_file_id_not_found_as_terminal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if self._is_forward_expensive_terminal_candidate(request, payload=payload):
            return True
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"file", "video"}:
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        if not (
            self._has_stale_forward_local_media_hint(request, payload=payload)
            or self._has_zero_byte_forward_local_media_hint(request, payload=payload)
        ):
            return False
        file_id = self._direct_forward_file_identifier(request, payload=payload)
        return file_id.startswith("/")

    def _should_classify_blank_direct_file_id_as_terminal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        if self._is_forward_expensive_asset(request):
            return True
        if self._has_terminal_top_level_file_like_missing_signal(
            request,
            payload=payload,
        ):
            return True
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"file", "video"}:
            return False
        hint = self._request_hint(request)
        if self._has_forward_parent_hint(hint):
            return False
        if not isinstance(payload, dict):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        direct_file_id = self._direct_forward_file_identifier(request, payload=payload)
        if not direct_file_id.startswith("/"):
            return False
        file_name = str(payload.get("file_name") or request.get("file_name") or "").strip()
        file_size = str(payload.get("file_size") or "").strip()
        payload_file_id = str(payload.get("file_id") or "").strip()
        return bool(file_name or file_size or payload_file_id)

    def _has_terminal_forward_metadata_local_missing_signal(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict) or not isinstance(payload, dict):
            return False
        if not self._is_forward_expensive_asset(request):
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        if not (
            self._has_stale_forward_local_media_hint(request, payload=payload)
            or self._has_zero_byte_forward_local_media_hint(request, payload=payload)
        ):
            return False
        local_candidate = str(
            payload.get("file") or payload.get("path") or payload.get("url") or ""
        ).strip()
        if local_candidate:
            if self._resolve_remote_url(local_candidate):
                return False
            if Path(local_candidate).exists():
                return False
        file_name = str(payload.get("file_name") or request.get("file_name") or "").strip()
        file_size = str(payload.get("file_size") or "").strip()
        file_id = str(payload.get("file_id") or "").strip()
        token = str(payload.get("public_file_token") or "").strip()
        return bool(file_name or file_size or file_id or token)

    def _has_terminal_direct_forward_file_identifier_failure(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        return (
            self._classify_blank_direct_file_id_missing(request, payload if isinstance(payload, dict) else None)
            == "qq_expired_after_napcat"
        )

    def _direct_forward_file_identifier(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> str:
        hint = self._request_hint(request) if isinstance(request, dict) else {}
        for value in (
            hint.get("file_id"),
            (payload or {}).get("file_id") if isinstance(payload, dict) else None,
        ):
            candidate = str(value or "").strip()
            if candidate:
                return candidate
        return ""

    def _forward_context_empty(
        self,
        request: dict[str, Any] | None,
        *,
        materialize: bool,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        timeout_cache_key = self._forward_context_timeout_key(
            request,
            materialize=materialize,
        )
        return bool(
            timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_empty_cache
        )

    def _forward_context_error(
        self,
        request: dict[str, Any] | None,
        *,
        materialize: bool,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        timeout_cache_key = self._forward_context_timeout_key(
            request,
            materialize=materialize,
        )
        return bool(
            timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_error_cache
        )

    def _forward_context_unavailable(
        self,
        request: dict[str, Any] | None,
        *,
        materialize: bool,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        timeout_cache_key = self._forward_context_timeout_key(
            request,
            materialize=materialize,
        )
        if (
            timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_unavailable_cache
        ):
            return True
        return bool(self._fast_forward_context_route_disabled and self._has_forward_parent_hint(self._request_hint(request)))

    def _public_action_timeout_s(
        self,
        action: str,
        *,
        request: dict[str, Any] | None = None,
    ) -> float:
        normalized_action = str(action or "").strip().lower()
        if normalized_action in {"get_image", "get_file", "get_record"} and self._is_forward_expensive_terminal_candidate(request):
            return float(self.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S)
        return float(self.PUBLIC_TOKEN_ACTION_TIMEOUT_S)

    def _direct_file_id_timeout_s(self, request: dict[str, Any] | None) -> float:
        if self._is_forward_expensive_terminal_candidate(request):
            return float(self.OLD_FORWARD_EXPENSIVE_DIRECT_FILE_ID_TIMEOUT_S)
        return float(self.DIRECT_FILE_ID_TIMEOUT_S)

    def _forward_context_timeout_s(
        self,
        request: dict[str, Any] | None,
        *,
        materialize: bool,
    ) -> float:
        if self._is_forward_expensive_terminal_candidate(request):
            return float(
                self.OLD_FORWARD_EXPENSIVE_MATERIALIZE_TIMEOUT_S
                if materialize
                else self.OLD_FORWARD_EXPENSIVE_METADATA_TIMEOUT_S
            )
        return float(
            self.FORWARD_TARGET_HTTP_TIMEOUT_S if materialize else self.FORWARD_CONTEXT_TIMEOUT_S
        )

    def _is_very_old_forward_expensive_asset(self, request: dict[str, Any] | None) -> bool:
        if not self._is_forward_expensive_asset(request):
            return False
        asset_age = self._request_asset_age(request)
        if asset_age is None:
            return False
        return asset_age >= timedelta(days=self.FORWARD_TIMEOUT_STORM_GLOBAL_MIN_AGE_DAYS)

    def _is_forward_expensive_terminal_candidate(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not self._is_forward_expensive_asset(request):
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        if self._has_stale_forward_local_media_hint(request, payload=payload):
            return True
        if self._has_zero_byte_forward_local_media_hint(request, payload=payload):
            return True
        if self._has_direct_forward_file_identifier(request, payload=payload):
            return True
        if isinstance(payload, dict):
            action = str(payload.get("public_action") or "").strip().lower()
            if action in {"get_file", "get_record"}:
                return True
            if str(payload.get("file_id") or "").strip().startswith("/"):
                return True
        return (
            self._forward_context_unavailable(request, materialize=False)
            or self._forward_context_unavailable(request, materialize=True)
            or self._forward_context_error(request, materialize=False)
            or self._forward_context_error(request, materialize=True)
            or self._forward_context_empty(request, materialize=False)
            or self._forward_context_empty(request, materialize=True)
            or self._forward_context_timed_out(request, materialize=False)
            or self._forward_context_timed_out(request, materialize=True)
        )

    def _is_forward_expensive_asset(self, request: dict[str, Any] | None) -> bool:
        if not isinstance(request, dict):
            return False
        hint = self._request_hint(request)
        if not self._has_forward_parent_hint(hint):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"image", "file", "video", "speech"}:
            return False
        return True

    def _has_live_http_media_url(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        for value in self._iter_request_media_locations(request, payload=payload):
            resolved_url = self._resolve_remote_url(value)
            if not resolved_url:
                continue
            parsed = urlparse(resolved_url)
            if parsed.scheme.lower() in {"http", "https"}:
                return True
        return False

    def _has_unexhausted_live_http_media_url(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not self._has_live_http_media_url(
            request,
            payload=payload,
        ):
            return False
        if self._has_failed_forward_remote_url(
            request,
            payload=payload,
        ):
            return False
        candidate_reasons = self._request_remote_failure_reasons(
            request,
            payload=payload,
        )
        if any(reason != "unsupported_local_download" for reason in candidate_reasons.values()):
            return False
        return True

    def _has_direct_forward_file_identifier(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        return bool(self._direct_forward_file_identifier(request, payload=payload))

    def _allow_old_forward_missing_without_stale_local_hint(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
        failure_signal_mode: str = "strict",
    ) -> bool:
        if not self._is_forward_expensive_terminal_candidate(request, payload=payload):
            return False
        normalized_mode = str(failure_signal_mode or "strict").strip().lower()
        if normalized_mode not in {"strict", "terminal"}:
            return False
        return not self._has_unexhausted_live_http_media_url(request, payload=payload)

    def _should_prefer_direct_file_id_before_targeted_materialize(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"video", "file"}:
            return False
        if not self._is_forward_expensive_terminal_candidate(request, payload=payload):
            return False
        file_id = self._direct_forward_file_identifier(request, payload=payload)
        if not file_id.startswith("/"):
            return False
        if self._has_unexhausted_live_http_media_url(request, payload=payload):
            return False
        return True

    def _has_stale_forward_local_media_hint(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        for value in self._iter_request_media_locations(request, payload=payload):
            if self._looks_like_stale_local_media_path(value):
                return True
        return False

    def _has_forward_local_media_hint(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        return any(self._iter_request_local_media_locations(request, payload=payload))

    def _has_zero_byte_forward_local_media_hint(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        for value in self._iter_request_media_locations(request, payload=payload):
            if self._looks_like_zero_byte_local_media_path(value):
                return True
        return False

    def _iter_request_media_locations(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, ...]:
        values: list[str] = []
        if isinstance(request, dict):
            values.extend(
                [
                    str(request.get("source_path") or "").strip(),
                ]
            )
            hint = self._request_hint(request)
            values.extend(
                [
                    str(hint.get("url") or "").strip(),
                    str(hint.get("remote_url") or "").strip(),
                    str(hint.get("file") or "").strip(),
                    str(hint.get("path") or "").strip(),
                ]
            )
        if isinstance(payload, dict):
            values.extend(
                [
                    str(payload.get("file") or "").strip(),
                    str(payload.get("url") or "").strip(),
                    str(payload.get("remote_url") or "").strip(),
                    str(payload.get("path") or "").strip(),
                ]
            )
        return tuple(value for value in values if value)

    def _iter_request_local_media_locations(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[str, ...]:
        values: list[str] = []
        for value in self._iter_request_media_locations(request, payload=payload):
            candidate = str(value or "").strip()
            if not candidate:
                continue
            lowered = candidate.lower()
            if lowered.startswith("file://"):
                values.append(candidate)
                continue
            parsed = urlparse(candidate)
            if parsed.scheme.lower() in {"http", "https"}:
                continue
            if re.match(r"^[a-zA-Z]:[\\/]", candidate) or candidate.startswith("\\\\"):
                values.append(candidate)
        return tuple(values)

    def _iter_request_remote_url_candidates(
        self,
        request: dict[str, Any] | None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(label_prefix: str, raw_value: object) -> None:
            candidate = self._resolve_remote_url(str(raw_value or "").strip())
            if not candidate:
                return
            normalized = self._normalized_match_url(candidate)
            if normalized not in seen:
                values.append((label_prefix, candidate))
                seen.add(normalized)
            projected = self._derive_projected_localhost_download_url(candidate)
            if projected:
                projected_normalized = self._normalized_match_url(projected)
                if projected_normalized not in seen:
                    values.append((f"{label_prefix}_projected", projected))
                    seen.add(projected_normalized)

        if isinstance(request, dict):
            hint = self._request_hint(request)
            _add("original", hint.get("remote_url") or hint.get("url"))
        if isinstance(payload, dict):
            _add("payload_original", payload.get("remote_url") or payload.get("url"))
        return tuple(values)

    @staticmethod
    def _public_payload_remote_url(payload: dict[str, Any] | None) -> str:
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("remote_url") or payload.get("url") or "").strip()

    @staticmethod
    def _zero_byte_local_payload_path(data: dict[str, Any] | None) -> Path | None:
        if not isinstance(data, dict):
            return None
        candidate = data.get("file") or data.get("path") or data.get("url")
        if not candidate:
            return None
        path_text = str(candidate).strip()
        if not path_text:
            return None
        if path_text.lower().startswith("file://"):
            path_text = path_text[7:]
        parsed = urlparse(path_text)
        if parsed.scheme.lower() in {"http", "https"}:
            return None
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return None
        try:
            if path.stat().st_size > 0:
                return None
        except OSError:
            return None
        return path.resolve()

    @staticmethod
    def _looks_like_stale_local_media_path(value: object) -> bool:
        candidate = str(value or "").strip()
        if not candidate:
            return False
        if candidate.lower().startswith("file://"):
            candidate = candidate[7:]
        if not (
            re.match(r"^[a-zA-Z]:[\\/]", candidate)
            or candidate.startswith("\\\\")
        ):
            return False
        return not Path(candidate).exists()

    @staticmethod
    def _looks_like_zero_byte_local_media_path(value: object) -> bool:
        candidate = str(value or "").strip()
        if not candidate:
            return False
        if candidate.lower().startswith("file://"):
            candidate = candidate[7:]
        if not (
            re.match(r"^[a-zA-Z]:[\\/]", candidate)
            or candidate.startswith("\\\\")
        ):
            return False
        path = Path(candidate)
        if not path.exists() or not path.is_file():
            return False
        try:
            return path.stat().st_size <= 0
        except OSError:
            return False

    def _public_action_timed_out(
        self,
        request: dict[str, Any] | None,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> bool:
        effective_token = str(token or "").strip()
        if not effective_token and isinstance(payload, dict):
            effective_token = str(payload.get("public_file_token") or "").strip()
        request_timeout_scope_key = self._request_scoped_public_action_timeout_key(
            request,
            action=action,
            token=effective_token or None,
        )
        return (
            request_timeout_scope_key is not None
            and request_timeout_scope_key in self._request_scoped_public_action_timeout_cache
        )

    def _public_action_failed(
        self,
        request: dict[str, Any] | None,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> bool:
        if self._public_action_timed_out(
            request,
            action=action,
            payload=payload,
            token=token,
        ):
            return True
        effective_token = str(token or "").strip()
        if not effective_token and isinstance(payload, dict):
            effective_token = str(payload.get("public_file_token") or "").strip()
        if not effective_token:
            return False
        return (str(action or "").strip().lower(), effective_token) in self._public_token_action_outcomes and (
            self._public_token_action_outcomes[(str(action or "").strip().lower(), effective_token)]
            is None
        )

    def _public_action_remote_failed(
        self,
        request: dict[str, Any] | None,
        *,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        if not isinstance(request, dict) or not isinstance(payload, dict):
            return False
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return False
        payload_action = str(payload.get("public_action") or "").strip().lower()
        normalized_action = str(action or "").strip().lower()
        if payload_action and payload_action != normalized_action:
            return False
        if self._resolved_path_from_payload(payload) is not None:
            return False
        remote_url = self._public_payload_remote_url(payload)
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return False
        normalized_remote_url = self._normalized_match_url(resolved_remote_url)
        cache_key = (asset_type, normalized_remote_url)
        cached_resolution = self._peek_remote_media_prefetch(cache_key)
        if cached_resolution not in {None, ...}:
            return False
        return normalized_remote_url in self._remote_media_failure_reasons

    def _forward_context_timed_out(
        self,
        request: dict[str, Any] | None,
        *,
        materialize: bool,
    ) -> bool:
        if not isinstance(request, dict):
            return False
        timeout_cache_key = self._forward_context_timeout_key(
            request,
            materialize=materialize,
        )
        return (
            timeout_cache_key is not None
            and timeout_cache_key in self._forward_context_timeout_cache
        )

    @staticmethod
    def _request_asset_age(request: dict[str, Any] | None) -> timedelta | None:
        if not isinstance(request, dict):
            return None
        raw_timestamp = request.get("timestamp_ms")
        if not isinstance(raw_timestamp, (int, float)):
            return None
        try:
            asset_dt = datetime.fromtimestamp(float(raw_timestamp) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return datetime.now(timezone.utc) - asset_dt

    def _classify_image_local_placeholder_missing(
        self,
        request: dict[str, Any] | None,
    ) -> str | None:
        if not isinstance(request, dict):
            return None
        if str(request.get("asset_type") or "").strip() != "image":
            return None
        source_path = str(request.get("source_path") or "").strip()
        if not source_path:
            return None
        cache_key = source_path.casefold()
        cached = self._image_placeholder_missing_cache.get(cache_key)
        if cache_key in self._image_placeholder_missing_cache:
            return cached

        source = Path(source_path)
        parent = source.parent
        if parent.name.casefold() not in {"ori", "oritemp", "thumb"}:
            self._image_placeholder_missing_cache[cache_key] = None
            return None
        stem = self._strip_thumb_suffix(source.stem)
        if not stem:
            self._image_placeholder_missing_cache[cache_key] = None
            return None

        base_dir = parent.parent
        index = self._ntqq_image_candidate_index_for_base_dir(base_dir)
        best_match, has_any_match = index.get(stem.casefold(), (None, False))
        if not has_any_match:
            self._image_placeholder_missing_cache[cache_key] = None
            return None
        if best_match is not None:
            self._image_placeholder_missing_cache[cache_key] = None
            return None

        result = "qq_not_downloaded_local_placeholder"
        self._image_placeholder_missing_cache[cache_key] = result
        return result

    def _ntqq_image_candidate_index_for_base_dir(
        self,
        base_dir: Path,
    ) -> dict[str, tuple[Path | None, bool]]:
        cache_key = str(base_dir).casefold()
        cached = self._ntqq_image_candidate_index_cache.get(cache_key)
        if cached is not None:
            return cached
        index: dict[str, tuple[Path | None, bool]] = {}
        for sibling_name in ("Ori", "OriTemp", "Thumb"):
            sibling_dir = base_dir / sibling_name
            if not sibling_dir.exists() or not sibling_dir.is_dir():
                continue
            with suppress(OSError):
                for candidate in sibling_dir.iterdir():
                    if not candidate.is_file():
                        continue
                    if not self._image_extension_allowed(candidate):
                        continue
                    stem = self._strip_thumb_suffix(candidate.stem if candidate.suffix else candidate.name)
                    if not stem:
                        continue
                    stem_key = stem.casefold()
                    best_match, has_any_match = index.get(stem_key, (None, False))
                    has_any_match = True
                    resolved_path: Path | None = None
                    with suppress(OSError):
                        if candidate.stat().st_size > 0:
                            resolved_path = candidate.resolve()
                    if resolved_path is not None:
                        if best_match is None or self._neighbor_candidate_priority(resolved_path) < self._neighbor_candidate_priority(best_match):
                            best_match = resolved_path
                    index[stem_key] = (best_match, has_any_match)
        self._ntqq_image_candidate_index_cache[cache_key] = index
        return index

    @staticmethod
    def _old_context_bucket(asset_type: str, request: dict[str, Any]) -> tuple[str, str] | None:
        if asset_type not in {"image", "file", "video", "speech"}:
            return None
        raw_timestamp = request.get("timestamp_ms")
        if not isinstance(raw_timestamp, (int, float)):
            return None
        try:
            asset_dt = datetime.fromtimestamp(float(raw_timestamp) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        if datetime.now(timezone.utc) - asset_dt < timedelta(
            days=NapCatMediaDownloader.OLD_CONTEXT_BUCKET_MIN_AGE_DAYS
        ):
            return None
        return asset_type, asset_dt.strftime("%Y-%m")

    @staticmethod
    def _write_bytes(target_path: Path, payload: bytes) -> bool:
        temp_path = target_path.with_name(
            f".{target_path.stem}.{build_timestamp_token(include_pid=True)}{target_path.suffix}.tmp"
        )
        try:
            temp_path.write_bytes(payload)
            temp_path.replace(target_path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        return True

    @staticmethod
    def _has_context_hint(hint: dict[str, Any]) -> bool:
        required = ("message_id_raw", "element_id", "peer_uid", "chat_type_raw")
        return all(str(hint.get(key) or "").strip() for key in required)

    @staticmethod
    def _has_forward_parent_hint(hint: dict[str, Any]) -> bool:
        parent = hint.get("_forward_parent")
        if not isinstance(parent, dict):
            return False
        return NapCatMediaDownloader._has_context_hint(parent)

    @staticmethod
    def _has_forward_parent_marker(hint: dict[str, Any]) -> bool:
        parent = hint.get("_forward_parent")
        return isinstance(parent, dict)

    @staticmethod
    def _forward_context_timeout_key(
        request: dict[str, Any],
        *,
        materialize: bool,
    ) -> tuple[str, ...] | None:
        hint = NapCatMediaDownloader._request_hint(request)
        parent = hint.get("_forward_parent")
        if not isinstance(parent, dict) or not NapCatMediaDownloader._has_context_hint(parent):
            return None
        key = (
            str(parent.get("message_id_raw") or "").strip(),
            str(parent.get("element_id") or "").strip(),
            str(parent.get("peer_uid") or "").strip(),
            str(parent.get("chat_type_raw") or "").strip(),
            str(request.get("asset_type") or "").strip(),
            str(request.get("asset_role") or "").strip(),
        )
        if not materialize:
            return ("metadata", *key)
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type in {"file", "video", "speech"}:
            return ("materialize", *key)
        return (
            "materialize",
            *key,
            str(request.get("file_name") or "").strip().lower(),
            str(request.get("md5") or "").strip().lower(),
        )

    @staticmethod
    def _request_scoped_public_action_timeout_key(
        request: dict[str, Any] | None,
        *,
        action: str,
        token: str | None = None,
    ) -> tuple[str, ...] | None:
        if not isinstance(request, dict):
            return None
        asset_type = str(request.get("asset_type") or "").strip()
        if asset_type not in {"image", "file", "video", "speech"}:
            return None
        hint = NapCatMediaDownloader._request_hint(request)
        if not NapCatMediaDownloader._has_forward_parent_hint(hint):
            return None
        if asset_type == "image" and action != "get_image":
            return None
        if asset_type in {"file", "video"} and action != "get_file":
            return None
        if asset_type == "speech" and action != "get_record":
            return None
        parent = hint.get("_forward_parent")
        assert isinstance(parent, dict)
        if asset_type == "image":
            discriminator: tuple[str, ...] = ()
        else:
            discriminator = (
                str(token or "").strip(),
                str(request.get("file_name") or "").strip().lower(),
                str(request.get("md5") or "").strip().lower(),
                str(hint.get("file_id") or "").strip(),
            )
        return (
            "forward_public_timeout",
            action,
            str(parent.get("message_id_raw") or "").strip(),
            str(parent.get("element_id") or "").strip(),
            str(parent.get("peer_uid") or "").strip(),
            str(parent.get("chat_type_raw") or "").strip(),
            asset_type,
            str(request.get("asset_role") or "").strip(),
            *discriminator,
        )

    @staticmethod
    def _forward_parent_scoped_public_action_timeout_key(
        request: dict[str, Any] | None,
        *,
        action: str,
    ) -> tuple[str, ...] | None:
        if not isinstance(request, dict):
            return None
        asset_type = str(request.get("asset_type") or "").strip().lower()
        if asset_type not in {"video", "file"}:
            return None
        if str(action or "").strip().lower() != "get_file":
            return None
        hint = NapCatMediaDownloader._request_hint(request)
        if not NapCatMediaDownloader._has_forward_parent_hint(hint):
            return None
        raw_timestamp = request.get("timestamp_ms")
        if not isinstance(raw_timestamp, (int, float)):
            return None
        try:
            asset_dt = datetime.fromtimestamp(float(raw_timestamp) / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        if datetime.now(timezone.utc) - asset_dt < timedelta(
            days=NapCatMediaDownloader.FORWARD_TIMEOUT_STORM_MIN_AGE_DAYS
        ):
            return None
        parent = hint.get("_forward_parent")
        assert isinstance(parent, dict)
        return (
            "forward_parent_public_timeout",
            str(action or "").strip().lower(),
            str(parent.get("message_id_raw") or "").strip(),
            str(parent.get("element_id") or "").strip(),
            str(parent.get("peer_uid") or "").strip(),
            str(parent.get("chat_type_raw") or "").strip(),
            asset_type,
            str(request.get("asset_role") or "").strip().lower(),
        )

    @staticmethod
    def _request_hint(request: dict[str, Any]) -> dict[str, Any]:
        hint = request.get("download_hint")
        return hint if isinstance(hint, dict) else {}

    @staticmethod
    def _request_key(request: dict[str, Any]) -> tuple[Any, ...]:
        hint = NapCatMediaDownloader._request_hint(request)
        parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
        remote_url = NapCatMediaDownloader._normalized_match_url(hint.get("remote_url") or hint.get("url"))
        return (
            str(request.get("asset_type") or "").strip(),
            str(request.get("asset_role") or "").strip(),
            str(request.get("file_name") or "").strip().lower(),
            str(request.get("md5") or "").strip().lower(),
            str(hint.get("file_id") or "").strip(),
            str(hint.get("file_biz_id") or request.get("file_biz_id") or "").strip(),
            remote_url,
            str(hint.get("message_id_raw") or "").strip(),
            str(hint.get("element_id") or "").strip(),
            str(hint.get("peer_uid") or "").strip(),
            str(hint.get("chat_type_raw") or "").strip(),
            str(parent.get("message_id_raw") or "").strip(),
            str(parent.get("element_id") or "").strip(),
            str(parent.get("peer_uid") or "").strip(),
            str(parent.get("chat_type_raw") or "").strip(),
        )

    @staticmethod
    def _batch_request_key(request: dict[str, Any]) -> tuple[Any, ...]:
        return (
            str(request.get("asset_type") or "").strip(),
            str(request.get("asset_role") or "").strip(),
            "",
            "",
            str(request.get("message_id_raw") or "").strip(),
            str(request.get("element_id") or "").strip(),
            str(request.get("peer_uid") or "").strip(),
            str(request.get("chat_type_raw") or "").strip(),
            "",
            "",
            "",
            "",
        )

    @staticmethod
    def _remote_file_name(remote_url: str, *, file_name: str | None) -> str | None:
        parsed = urlparse(remote_url)
        remote_leaf = Path(parsed.path).name.strip()
        if file_name:
            return file_name
        if remote_leaf:
            return remote_leaf
        return None

    @staticmethod
    def _remote_cache_file_name(remote_url: str, *, file_name: str | None) -> str:
        raw_name = NapCatMediaDownloader._remote_file_name(remote_url, file_name=file_name) or "remote.bin"
        path = Path(raw_name)
        suffix = "".join(path.suffixes)
        stem = path.name[: -len(suffix)] if suffix else path.name
        safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "remote"
        url_hash = hashlib.sha1(remote_url.encode("utf-8")).hexdigest()[:12]
        return f"{safe_stem}_{url_hash}{suffix}"

    def _resolve_remote_url(self, remote_url: str) -> str | None:
        candidate = str(remote_url or "").strip()
        if not candidate:
            return None
        lowered = candidate.lower()
        if lowered.startswith("file://"):
            return None
        if re.match(r"^[a-zA-Z]:[\\/]", candidate):
            return None
        if candidate.startswith("\\\\"):
            return None
        parsed = urlparse(candidate)
        if parsed.scheme and parsed.netloc:
            return candidate
        if not self._remote_base_url:
            return None
        if not (
            candidate.startswith("/download")
            or candidate.startswith("download?")
        ):
            return None
        return urljoin(self._remote_base_url.rstrip("/") + "/", candidate.lstrip("/"))

    @staticmethod
    def _is_relative_local_download_hint(value: object) -> bool:
        candidate = str(value or "").strip().lower()
        return candidate.startswith("/download?") or candidate.startswith("download?")
