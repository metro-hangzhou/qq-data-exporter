from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import uuid
from concurrent.futures import Future
import httpx

from qq_data_integrations.napcat.fast_history_client import NapCatFastHistoryTimeoutError
from qq_data_integrations.napcat.fast_history_client import NapCatFastHistoryUnavailable
from qq_data_integrations.napcat.http_client import NapCatApiError
from qq_data_integrations.napcat.http_client import NapCatApiTimeoutError
from qq_data_integrations.napcat.media_downloader import NapCatMediaDownloader


class _DummyClient:
    pass


class _TimeoutPublicFileClient:
    def __init__(self) -> None:
        self.get_file_calls = 0
        self.timeouts: list[float | None] = []

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        raise NapCatApiTimeoutError("NapCat action timed out: get_file")


class _TimeoutPublicRecordClient:
    def __init__(self) -> None:
        self.get_record_calls = 0
        self.timeouts: list[float | None] = []

    def get_record(self, *args, **kwargs):
        self.get_record_calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        raise NapCatApiTimeoutError("NapCat action timed out: get_record")


class _TimeoutPublicImageClient:
    def __init__(self) -> None:
        self.get_image_calls = 0
        self.timeouts: list[float | None] = []

    def get_image(self, *args, **kwargs):
        self.get_image_calls += 1
        self.timeouts.append(kwargs.get("timeout"))
        raise NapCatApiTimeoutError("NapCat action timed out: get_image")


class _MissingDirectFileClient:
    def __init__(self) -> None:
        self.get_file_calls = 0

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        raise NapCatApiError("file not found")


class _MissingPublicFileClient:
    def __init__(self) -> None:
        self.get_file_calls = 0

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        raise NapCatApiError("file not found")


class _SuccessPublicFileClient:
    def __init__(self) -> None:
        self.get_file_calls = 0

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        return {"file": str(Path(__file__).resolve()), "file_name": "success.bin"}


class _MissingPublicRecordClient:
    def __init__(self) -> None:
        self.get_record_calls = 0

    def get_record(self, *args, **kwargs):
        self.get_record_calls += 1
        raise NapCatApiError("file not found")


class _BlankPublicFileClient:
    def __init__(self) -> None:
        self.get_file_calls = 0

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        return {"file": "", "url": ""}


class _BlankDirectFilePayloadClient:
    def __init__(self) -> None:
        self.get_file_calls = 0

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        return {
            "file": "",
            "url": "",
            "file_id": "/fileid/blank-direct-file",
            "file_name": "blank-direct-file.mp4",
            "file_size": "12345",
        }


class _PublicImageClient:
    def __init__(self) -> None:
        self.get_image_calls = 0

    def get_image(self, *args, **kwargs):
        self.get_image_calls += 1
        return {
            "file": str(Path(__file__).resolve()),
            "url": "",
        }


class _RemoteMediaDownloader(NapCatMediaDownloader):
    def __init__(self, remote_cache_dir: Path) -> None:
        super().__init__(_DummyClient(), remote_cache_dir=remote_cache_dir)

    async def _download_remote_payload_async(self, remote_url: str) -> bytes | None:  # type: ignore[override]
        return b"fake-bytes" if remote_url else None


class _CleanupProbeDownloader(NapCatMediaDownloader):
    def __init__(self) -> None:
        super().__init__(_DummyClient())
        self.rebuild_calls: list[tuple[bool, bool]] = []

    def _rebuild_prefetch_executors(self, *, wait: bool, recreate: bool) -> None:  # type: ignore[override]
        self.rebuild_calls.append((wait, recreate))


class _BrokenRemoteRuntimeDownloader(NapCatMediaDownloader):
    def _start_remote_download_runtime(self) -> None:  # type: ignore[override]
        raise RuntimeError("remote media async runtime failed to start")


class _BrokenRemoteRuntimeWithSyncFallbackDownloader(_BrokenRemoteRuntimeDownloader):
    def __init__(self, remote_cache_dir: Path) -> None:
        super().__init__(_DummyClient(), remote_cache_dir=remote_cache_dir)
        self.sync_remote_calls: list[str] = []

    def _download_remote_payload_sync(self, remote_url: str) -> bytes | None:  # type: ignore[override]
        self.sync_remote_calls.append(str(remote_url))
        return b"sync-fallback-bytes" if remote_url else None


class _ResettingExecutor:
    def __init__(self, downloader: NapCatMediaDownloader) -> None:
        self.downloader = downloader

    def submit(self, fn, *args, **kwargs):
        _ = fn, args, kwargs
        future: Future[dict[str, object] | None] = Future()
        self.downloader.reset_export_state()
        future.set_result(None)
        return future

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        _ = wait, cancel_futures
        return


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        future: Future[dict[str, object] | None] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # pragma: no cover - helper should stay simple
            future.set_exception(exc)
        return future

    def shutdown(self, *, wait: bool = True, cancel_futures: bool = False) -> None:
        _ = wait, cancel_futures
        return


class _ResetDuringTokenPrefetchDownloader(NapCatMediaDownloader):
    def _create_prefetch_executors(self) -> None:  # type: ignore[override]
        self._remote_prefetch_runtime_disabled = True
        self._remote_prefetch_runtime_disable_reason = "simulated"
        self._remote_loop = None
        self._remote_loop_thread = None
        self._remote_async_client = None
        self._remote_async_semaphore = None
        self._public_token_executor = _ResettingExecutor(self)


class _ResetDuringRemoteSubmitDownloader(NapCatMediaDownloader):
    def _create_prefetch_executors(self) -> None:  # type: ignore[override]
        self._remote_prefetch_runtime_disabled = True
        self._remote_prefetch_runtime_disable_reason = "simulated"
        self._remote_loop = None
        self._remote_loop_thread = None
        self._remote_async_client = None
        self._remote_async_semaphore = None
        self._public_token_executor = None

    def _submit_remote_media_download(  # type: ignore[override]
        self,
        *,
        asset_type: str,
        file_name: str | None,
        resolved_remote_url: str,
    ) -> Future[str | None] | None:
        _ = asset_type, file_name, resolved_remote_url
        future: Future[str | None] = Future()
        self.reset_export_state()
        future.set_result("stale")
        return future


def _workspace_temp_dir() -> Path:
    root = Path(".tmp") / f"pytest_remote_cache_{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _second_pass_request(*, file_id: str = "live-token") -> dict[str, object]:
    return {
        "asset_type": "image",
        "file_name": "sample-image.jpg",
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Ori\sample-image",
        "md5": "sample-md5",
        "timestamp_ms": 1754057899000,
        "download_hint": {
            "file_id": file_id,
            "message_id_raw": "7565810516712816523",
            "element_id": "7565810516712816524",
            "peer_uid": "922065597",
            "chat_type_raw": "group",
        },
    }


class _TimeoutForwardClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        raise NapCatFastHistoryTimeoutError("timed out")


class _BatchFastClient:
    def __init__(self, *, raise_timeout: bool = False) -> None:
        self.raise_timeout = raise_timeout
        self.calls: list[list[dict[str, object]]] = []
        self.timeouts: list[object] = []

    def hydrate_media_batch(self, _items, *, timeout=None):
        self.calls.append(list(_items))
        self.timeouts.append(timeout)
        if self.raise_timeout:
            raise NapCatFastHistoryTimeoutError("batch timed out")
        return {"items": []}


class _EmptyForwardClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        return {"assets": []}


class _ErrorForwardClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError("forward route exploded")


class _UnavailableForwardClient:
    def __init__(self) -> None:
        self.forward_calls: list[dict[str, object]] = []
        self.media_calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.forward_calls.append(kwargs)
        raise NapCatFastHistoryUnavailable("forward route missing")

    def hydrate_media(self, **kwargs):
        self.media_calls.append(kwargs)
        return {"file": str(Path(__file__).resolve())}


class _UnavailableContextClient:
    def __init__(self) -> None:
        self.forward_calls: list[dict[str, object]] = []
        self.media_calls: list[dict[str, object]] = []

    def hydrate_media(self, **kwargs):
        self.media_calls.append(kwargs)
        raise NapCatFastHistoryUnavailable("context route missing")

    def hydrate_forward_media(self, **kwargs):
        self.forward_calls.append(kwargs)
        return {
            "assets": [
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": "forward-ok.jpg",
                    "file": str(Path(__file__).resolve()),
                }
            ]
        }


class _StaticContextPayloadClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = dict(payload)
        self.media_calls: list[dict[str, object]] = []

    def hydrate_media(self, **kwargs):
        self.media_calls.append(kwargs)
        return dict(self.payload)


class _NeighborProbeDownloader(NapCatMediaDownloader):
    def __init__(self) -> None:
        super().__init__(_DummyClient())
        self.base_dir_index_builds = 0

    def _ntqq_image_candidate_index_for_base_dir(self, base_dir: Path):  # type: ignore[override]
        self.base_dir_index_builds += 1
        return super()._ntqq_image_candidate_index_for_base_dir(base_dir)


class _SuccessForwardClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "targeted_mode": "metadata_only",
            "assets": [
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": "2C167901425EF469C0B1F0BF859E4B2C.jpg",
                    "file": str(Path(__file__).resolve()),
                },
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": "49D109C31C9FADA0A156408B75DC1620.png",
                    "file": str(Path(__file__).resolve()),
                },
            ],
        }


class _TargetedForwardMetadataClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capabilities(self):
        return {"features": {"forward_parent_metadata_scope": True}}

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        requested_file_name = str(kwargs.get("file_name") or "").strip()
        if not requested_file_name:
            return {
                "assets": [
                    {
                        "asset_type": "image",
                        "asset_role": "forward_media",
                        "file_name": "A1111111111111111111111111111111.jpg",
                        "md5": "a1111111111111111111111111111111",
                        "file": str(Path(__file__).resolve()),
                    },
                    {
                        "asset_type": "image",
                        "asset_role": "forward_media",
                        "file_name": "B2222222222222222222222222222222.jpg",
                        "md5": "b2222222222222222222222222222222",
                        "file": str(Path(__file__).resolve()),
                    },
                ],
            }
        return {
            "targeted": True,
            "targeted_mode": "metadata_only",
            "assets": [
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": requested_file_name,
                    "md5": Path(requested_file_name).stem.lower(),
                    "file": str(Path(__file__).resolve()),
                }
            ],
        }


class _SlowMismatchedForwardClient:
    def __init__(self, delay_s: float = 0.02) -> None:
        self.delay_s = delay_s
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        time.sleep(self.delay_s)
        return {
            "targeted_mode": "single_target_download",
            "assets": [
                {
                    "asset_type": "video",
                    "asset_role": "forward_media",
                    "file_name": "not-the-requested-video.mp4",
                    "file": str(Path(__file__).resolve()),
                }
            ],
        }


class _RecordingForwardClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        return {"assets": []}


class _OldForwardMetadataTimeoutClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise AssertionError("targeted materialize should be skipped for stale old forward video")
        raise NapCatFastHistoryTimeoutError("timed out")


class _OldForwardTokenOnlyClient:
    def __init__(self, stale_url: str) -> None:
        self.stale_url = stale_url
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise AssertionError("targeted materialize should be skipped after old forward token timeout")
        return {
            "assets": [
                {
                    "asset_type": "video",
                    "asset_role": "forward_media",
                    "file_name": "old-forward-timeout.mp4",
                    "url": self.stale_url,
                    "public_action": "get_file",
                    "public_file_token": "old-forward-timeout-token",
                }
            ]
        }


class _ForwardMetadataOnlyVideoClient:
    def __init__(self, stale_path: str) -> None:
        self.stale_path = stale_path
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise AssertionError("targeted materialize should be skipped after direct public token proof")
        return {
            "targeted": True,
            "targeted_mode": "metadata_only",
            "assets": [
                {
                    "asset_type": "video",
                    "file_name": str(kwargs.get("file_name") or "forward-video.mp4"),
                    "file_id": str(kwargs.get("file_id") or ""),
                    "file_size": "8551137",
                    "file": self.stale_path,
                    "url": self.stale_path,
                }
            ],
        }


def test_remote_payload_sync_rejects_json_api_failure_body() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json; charset=utf-8"},
                json={
                    "status": "failed",
                    "retcode": 200,
                    "message": "不支持的Api download",
                    "wording": "不支持的Api download",
                },
                request=request,
            )
        ),
    )

    payload = downloader._download_remote_payload_sync(
        "http://127.0.0.1:3000/download?appid=1407&fileid=fake&spec=0"
    )

    assert payload is None


def test_remote_payload_sync_keeps_binary_media_body() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png"},
                content=b"\x89PNG\r\n\x1a\nbinary",
                request=request,
            )
        ),
    )

    payload = downloader._download_remote_payload_sync(
        "http://127.0.0.1:3000/download?appid=1407&fileid=fake&spec=0"
    )

    assert payload == b"\x89PNG\r\n\x1a\nbinary"


class _ForwardImageTargetedMissClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise AssertionError("forward image targeted materialize should not run")
        return {"targeted": True, "targeted_mode": "targeted_miss", "assets": []}


class _ForwardImageMetadataTimeoutClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise AssertionError("forward image targeted materialize should not run after metadata timeout")
        raise NapCatFastHistoryTimeoutError("timed out")


class _ForwardImageMissingLocalPayloadClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = dict(payload)
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise AssertionError("forward image targeted materialize should not run when metadata already proves missing")
        return {
            "targeted": True,
            "targeted_mode": "metadata_only",
            "assets": [dict(self.payload)],
        }


class _OldForwardMaterializeOnlyTimeoutClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            raise NapCatFastHistoryTimeoutError("timed out")
        return {"assets": []}


class _OldForwardEmptyClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        return {"assets": []}


class _OldForwardZeroLocalClient:
    def __init__(self, zero_path: str) -> None:
        self.zero_path = zero_path
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("materialize"):
            return {
                "asset_type": "video",
                "asset_role": "forward_media",
                "file_name": "old-forward-zero-local.mp4",
                "file": self.zero_path,
            }
        return {"assets": []}


def _build_forward_request(file_name: str) -> dict[str, object]:
    return {
        "asset_type": "image",
        "asset_role": "forward_media",
        "file_name": file_name,
        "md5": "",
        "download_hint": {
            "_forward_parent": {
                "message_id_raw": "7617760641125573795",
                "element_id": "7617760641125573794",
                "peer_uid": "u_example",
                "chat_type_raw": "2",
            }
        },
    }


def _build_forward_video_request(file_name: str) -> dict[str, object]:
    request = _build_forward_request(file_name)
    request["asset_type"] = "video"
    return request


def _build_forward_speech_request(file_name: str) -> dict[str, object]:
    request = _build_forward_request(file_name)
    request["asset_type"] = "speech"
    return request


def _mark_request_old(request: dict[str, object], *, days: int = 90) -> dict[str, object]:
    updated = dict(request)
    updated["timestamp_ms"] = int((time.time() - (days * 24 * 60 * 60)) * 1000)
    return updated


def _set_forward_parent_identity(
    request: dict[str, object],
    *,
    message_id_raw: str,
    element_id: str,
) -> dict[str, object]:
    updated = dict(request)
    hint = dict(updated.get("download_hint") or {})
    parent = dict(hint.get("_forward_parent") or {})
    parent["message_id_raw"] = message_id_raw
    parent["element_id"] = element_id
    hint["_forward_parent"] = parent
    updated["download_hint"] = hint
    return updated


def _set_forward_stale_local_path(
    request: dict[str, object],
    path: str,
) -> dict[str, object]:
    updated = dict(request)
    hint = dict(updated.get("download_hint") or {})
    hint["url"] = path
    updated["download_hint"] = hint
    updated["source_path"] = path
    return updated


def _build_context_hint_request(file_name: str) -> dict[str, object]:
    return {
        "asset_type": "image",
        "asset_role": "",
        "file_name": file_name,
        "download_hint": {
            "message_id_raw": "7610000000000000001",
            "element_id": "7610000000000000000",
            "peer_uid": "u_example",
            "chat_type_raw": "2",
        },
    }


def test_settle_export_download_progress_clears_pending_counts() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    downloader._remote_base_url = "http://127.0.0.1:6099"
    downloader.begin_export_download_tracking([{"asset_type": "image", "download_hint": {}}])
    cache_key = ("image", "queued")
    downloader._download_operation_states[cache_key] = "queued"
    downloader._download_progress["queued"] = 1
    downloader._download_progress["active"] = 1
    downloader._download_operation_states[("image", "active")] = "active"

    settled = downloader.settle_export_download_progress()

    assert settled["queued"] == 0
    assert settled["active"] == 0


def test_remote_prefetch_runtime_startup_failure_degrades_without_breaking_downloader() -> None:
    downloader = _BrokenRemoteRuntimeDownloader(_DummyClient())

    assert downloader._remote_prefetch_runtime_disabled is True
    assert downloader._remote_prefetch_runtime_disable_reason == "remote media async runtime failed to start"
    assert downloader._public_token_executor is not None
    assert downloader._remote_loop is None
    assert downloader._remote_async_client is None


def test_remote_prefetch_runtime_disabled_process_still_rebuilds_safely() -> None:
    downloader = _BrokenRemoteRuntimeDownloader(_DummyClient())

    downloader._rebuild_prefetch_executors(wait=False, recreate=True)

    assert downloader._remote_prefetch_runtime_disabled is True
    assert downloader._public_token_executor is not None


def test_remote_media_download_falls_back_to_sync_when_async_runtime_disabled() -> None:
    temp_root = _workspace_temp_dir()
    downloader = _BrokenRemoteRuntimeWithSyncFallbackDownloader(temp_root / "remote_cache")

    try:
        resolved = downloader._download_remote_media(
            asset_type="image",
            file_name="fallback.jpg",
            hint={"url": "https://example.invalid/fallback.jpg"},
        )

        assert resolved is not None
        resolved_path = Path(resolved)
        assert resolved_path.exists()
        assert resolved_path.read_bytes() == b"sync-fallback-bytes"
        assert downloader.sync_remote_calls == ["https://example.invalid/fallback.jpg"]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_public_token_remote_url_recovers_via_sync_fallback_when_async_runtime_disabled() -> None:
    temp_root = _workspace_temp_dir()
    downloader = _BrokenRemoteRuntimeWithSyncFallbackDownloader(temp_root / "remote_cache")
    downloader._call_public_action_with_token = (  # type: ignore[method-assign]
        lambda *args, **kwargs: {
            "url": "https://example.invalid/public-token-fallback.jpg",
        }
    )

    try:
        resolved = downloader._resolve_from_public_token(
            {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": "token-sync-fallback",
                "file_name": "public-token-fallback.jpg",
            },
            request={
                "asset_type": "image",
                "file_name": "public-token-fallback.jpg",
            },
        )

        assert resolved is not None
        assert resolved[0] is not None
        assert resolved[1] == "napcat_public_token_get_image_remote_url"
        assert downloader.sync_remote_calls == ["https://example.invalid/public-token-fallback.jpg"]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_forward_metadata_timeout_is_short_circuited_for_sibling_assets() -> None:
    fast_client = _TimeoutForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    first = downloader._download_via_forward_context(
        _build_forward_request("2C167901425EF469C0B1F0BF859E4B2C.jpg"),
        materialize=False,
    )
    second = downloader._download_via_forward_context(
        _build_forward_request("49D109C31C9FADA0A156408B75DC1620.png"),
        materialize=False,
    )

    assert first is None
    assert second is None
    assert len(fast_client.calls) == 1


def test_forward_metadata_empty_result_is_short_circuited_for_sibling_assets() -> None:
    fast_client = _EmptyForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    first = downloader._download_via_forward_context(
        _build_forward_request("2C167901425EF469C0B1F0BF859E4B2C.jpg"),
        materialize=False,
    )
    second = downloader._download_via_forward_context(
        _build_forward_request("49D109C31C9FADA0A156408B75DC1620.png"),
        materialize=False,
    )

    assert first is None
    assert second is None
    assert len(fast_client.calls) == 1
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_context_empty_count"] == 1


def test_forward_metadata_error_is_short_circuited_for_sibling_assets() -> None:
    fast_client = _ErrorForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    first = downloader._download_via_forward_context(
        _build_forward_request("2C167901425EF469C0B1F0BF859E4B2C.jpg"),
        materialize=False,
    )
    second = downloader._download_via_forward_context(
        _build_forward_request("49D109C31C9FADA0A156408B75DC1620.png"),
        materialize=False,
    )

    assert first is None
    assert second is None
    assert len(fast_client.calls) == 1
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_context_error_count"] == 1


def test_forward_route_unavailable_does_not_disable_regular_context_hydration() -> None:
    fast_client = _UnavailableForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    forward_result = downloader._download_via_forward_context(
        _build_forward_request("forward-a.jpg"),
        materialize=False,
    )
    context_payload = downloader._download_via_context(
        _build_context_hint_request("context-a.jpg")["download_hint"],
        asset_type="image",
        asset_role=None,
        request=_build_context_hint_request("context-a.jpg"),
    )

    assert forward_result is None
    assert len(fast_client.forward_calls) == 1
    assert len(fast_client.media_calls) == 1
    assert context_payload is not None
    assert downloader._fast_context_route_disabled is False
    assert downloader._fast_forward_context_route_disabled is True


def test_regular_context_unavailable_does_not_disable_forward_hydration() -> None:
    fast_client = _UnavailableContextClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    context_payload = downloader._download_via_context(
        _build_context_hint_request("context-b.jpg")["download_hint"],
        asset_type="image",
        asset_role=None,
        request=_build_context_hint_request("context-b.jpg"),
    )
    forward_result = downloader._download_via_forward_context(
        _build_forward_request("forward-ok.jpg"),
        materialize=False,
    )

    assert context_payload is None
    assert len(fast_client.media_calls) == 1
    assert len(fast_client.forward_calls) == 1
    assert forward_result == (Path(__file__).resolve(), "napcat_forward_hydrated")
    assert downloader._fast_context_route_disabled is True
    assert downloader._fast_forward_context_route_disabled is False


def test_forward_metadata_success_payload_is_reused_for_sibling_assets() -> None:
    fast_client = _SuccessForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    first = downloader._download_via_forward_context(
        _build_forward_request("2C167901425EF469C0B1F0BF859E4B2C.jpg"),
        materialize=False,
    )
    second = downloader._download_via_forward_context(
        _build_forward_request("49D109C31C9FADA0A156408B75DC1620.png"),
        materialize=False,
    )

    assert len(fast_client.calls) == 1
    assert first == (Path(__file__).resolve(), "napcat_forward_hydrated")
    assert second == (Path(__file__).resolve(), "napcat_forward_hydrated")


def test_forward_image_metadata_uses_parent_scoped_request_without_target_selectors() -> None:
    fast_client = _SuccessForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader._fast_capabilities_loaded = True
    downloader._fast_capabilities = {"features": {"forward_parent_metadata_scope": True}}

    result = downloader._download_via_forward_context(
        _build_forward_request("2C167901425EF469C0B1F0BF859E4B2C.jpg"),
        materialize=False,
    )

    assert result == (Path(__file__).resolve(), "napcat_forward_hydrated")
    assert len(fast_client.calls) == 1
    call = fast_client.calls[0]
    assert call.get("asset_type") == "image"
    assert call.get("asset_role") == "forward_media"
    assert call.get("file_name") is None
    assert call.get("md5") is None
    assert call.get("file_id") is None
    assert call.get("url") is None


def test_prefetch_forward_metadata_dedupes_parent_scoped_image_requests() -> None:
    fast_client = _TargetedForwardMetadataClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader._fast_capabilities_loaded = True
    downloader._fast_capabilities = {"features": {"forward_parent_metadata_scope": True}}
    requests = [
        _build_forward_request("A1111111111111111111111111111111.jpg"),
        _build_forward_request("B2222222222222222222222222222222.jpg"),
    ]
    progress: list[dict[str, object]] = []

    downloader._prefetch_forward_metadata_requests(
        requests,
        progress_callback=progress.append,
    )

    assert len(fast_client.calls) == 1
    call = fast_client.calls[0]
    assert call.get("file_name") is None
    assert call.get("asset_type") is None
    assert call.get("asset_role") is None
    assert call.get("md5") is None
    assert call.get("file_id") is None
    assert call.get("url") is None
    assert downloader._prefetched_forward_media[downloader._request_key(requests[0])] == (
        Path(__file__).resolve(),
        "napcat_forward_hydrated",
    )
    assert downloader._prefetched_forward_media[downloader._request_key(requests[1])] == (
        Path(__file__).resolve(),
        "napcat_forward_hydrated",
    )
    done_event = next(
        row
        for row in progress
        if row.get("phase") == "prefetch_forward_metadata" and row.get("stage") == "done"
    )
    assert done_event["request_count"] == 2
    assert done_event["group_count"] == 1
    assert done_event["processed_request_count"] == 2


def test_forward_image_can_resolve_via_direct_public_token_hint() -> None:
    client = _PublicImageClient()
    downloader = NapCatMediaDownloader(client)
    request = _build_forward_request("forward-direct-token.jpg")
    hint = dict(request["download_hint"])
    hint["file_id"] = "forward-image-public-token"
    request["download_hint"] = hint

    result = downloader._resolve_via_direct_public_token_hint(request)

    assert result == (Path(__file__).resolve(), "napcat_public_token_get_image")
    assert client.get_image_calls == 1


def test_forward_video_public_token_timeout_skips_later_retry_even_with_new_token() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request = _build_forward_video_request("slow-forward-video.mp4")

    first = downloader._call_public_action_with_token(
        "get_file",
        "first-token",
        request=request,
    )
    second = downloader._call_public_action_with_token(
        "get_file",
        "second-token",
        request=request,
    )

    assert first is None
    assert second is None
    assert client.get_file_calls == 2


def test_forward_video_materialize_timeout_skips_later_retry_for_sibling_assets() -> None:
    fast_client = _TimeoutForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    first = downloader._download_via_forward_context(
        _build_forward_video_request("slow-forward-video-a.mp4"),
        materialize=True,
    )
    second = downloader._download_via_forward_context(
        _build_forward_video_request("slow-forward-video-b.mp4"),
        materialize=True,
    )

    assert first is None
    assert second is None
    assert len(fast_client.calls) == 1


def test_forward_video_public_token_timeout_skips_later_retry_for_sibling_assets() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)

    first = downloader._call_public_action_with_token(
        "get_file",
        "first-token",
        request=_build_forward_video_request("slow-forward-video-a.mp4"),
    )
    second = downloader._call_public_action_with_token(
        "get_file",
        "second-token",
        request=_build_forward_video_request("slow-forward-video-b.mp4"),
    )

    assert first is None
    assert second is None
    assert client.get_file_calls == 2


def test_forward_speech_materialize_timeout_skips_later_retry_for_sibling_assets() -> None:
    fast_client = _TimeoutForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    first = downloader._download_via_forward_context(
        _build_forward_speech_request("slow-forward-audio-a.amr"),
        materialize=True,
    )
    second = downloader._download_via_forward_context(
        _build_forward_speech_request("slow-forward-audio-b.amr"),
        materialize=True,
    )

    assert first is None
    assert second is None
    assert len(fast_client.calls) == 1


def test_forward_speech_public_token_timeout_skips_later_retry_for_sibling_assets() -> None:
    client = _TimeoutPublicRecordClient()
    downloader = NapCatMediaDownloader(client)

    first = downloader._call_public_action_with_token(
        "get_record",
        "first-token",
        request=_build_forward_speech_request("slow-forward-audio-a.amr"),
    )
    second = downloader._call_public_action_with_token(
        "get_record",
        "second-token",
        request=_build_forward_speech_request("slow-forward-audio-b.amr"),
    )

    assert first is None
    assert second is None
    assert client.get_record_calls == 2


def test_old_forward_video_uses_shorter_public_token_timeout() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-timeout.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-timeout.mp4",
    )

    assert downloader._call_public_action_with_token(
        "get_file",
        "old-forward-timeout-token",
        request=request,
    ) is None

    assert client.get_file_calls == 1
    assert client.timeouts == [downloader.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S]


def test_recent_forward_video_uses_shorter_public_token_timeout_when_terminal_candidate() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("recent-forward-timeout.mp4"), days=7),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-03\Ori\recent-forward-timeout.mp4",
    )

    assert downloader._call_public_action_with_token(
        "get_file",
        "recent-forward-timeout-token",
        request=request,
    ) is None

    assert client.get_file_calls == 1
    assert client.timeouts == [downloader.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S]


def test_old_forward_image_uses_shorter_public_token_timeout() -> None:
    client = _TimeoutPublicImageClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_request("old-forward-image-timeout.png"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Pic\2025-05\Ori\old-forward-image-timeout.png",
    )

    assert downloader._call_public_action_with_token(
        "get_image",
        "old-forward-image-timeout-token",
        request=request,
    ) is None

    assert client.get_image_calls == 1
    assert client.timeouts == [downloader.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S]


def test_recent_forward_image_uses_shorter_public_token_timeout_when_terminal_candidate() -> None:
    client = _TimeoutPublicImageClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_request("recent-forward-image-timeout.png"), days=7),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Pic\2026-03\Ori\recent-forward-image-timeout.png",
    )

    assert downloader._call_public_action_with_token(
        "get_image",
        "recent-forward-image-timeout-token",
        request=request,
    ) is None

    assert client.get_image_calls == 1
    assert client.timeouts == [downloader.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S]


def test_old_forward_video_uses_shorter_forward_context_timeouts() -> None:
    fast_client = _RecordingForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-context.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-context.mp4",
    )

    assert downloader._download_via_forward_context(request, materialize=False) is None
    assert downloader._download_via_forward_context(request, materialize=True) is None

    assert [call.get("timeout") for call in fast_client.calls] == [
        downloader.OLD_FORWARD_EXPENSIVE_METADATA_TIMEOUT_S,
        downloader.OLD_FORWARD_EXPENSIVE_MATERIALIZE_TIMEOUT_S,
    ]


def test_recent_forward_video_uses_shorter_forward_context_timeouts_when_terminal_candidate() -> None:
    fast_client = _RecordingForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("recent-forward-context.mp4"), days=7),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-03\Ori\recent-forward-context.mp4",
    )

    assert downloader._download_via_forward_context(request, materialize=False) is None
    assert downloader._download_via_forward_context(request, materialize=True) is None

    assert [call.get("timeout") for call in fast_client.calls] == [
        downloader.OLD_FORWARD_EXPENSIVE_METADATA_TIMEOUT_S,
        downloader.OLD_FORWARD_EXPENSIVE_MATERIALIZE_TIMEOUT_S,
    ]


def test_old_forward_video_metadata_timeout_is_classified_before_targeted_materialize() -> None:
    fast_client = _OldForwardMetadataTimeoutClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-metadata-timeout.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-metadata-timeout.mp4",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_recent_forward_video_metadata_timeout_is_classified_before_targeted_materialize() -> None:
    fast_client = _OldForwardMetadataTimeoutClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("recent-forward-metadata-timeout.mp4"), days=7),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-03\Ori\recent-forward-metadata-timeout.mp4",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_old_forward_video_public_token_timeout_is_classified_before_targeted_materialize() -> None:
    stale_url = r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-timeout.mp4"
    fast_client = _OldForwardTokenOnlyClient(stale_url)
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-timeout.mp4"), days=240),
        stale_url,
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1
    assert client.timeouts == [downloader.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S]
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_recent_forward_video_public_token_timeout_is_classified_before_targeted_materialize() -> None:
    stale_url = r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-03\Ori\recent-forward-timeout.mp4"
    fast_client = _OldForwardTokenOnlyClient(stale_url)
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("recent-forward-timeout.mp4"), days=7),
        stale_url,
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1
    assert client.timeouts == [downloader.OLD_FORWARD_EXPENSIVE_PUBLIC_TOKEN_TIMEOUT_S]
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_forward_video_direct_public_timeout_without_prefetched_metadata_skips_materialize() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("direct-token-only-timeout.mp4"), days=180),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-09\Ori\direct-token-only-timeout.mp4",
    )
    request["download_hint"] = {
        **dict(request.get("download_hint") or {}),
        "file_id": "direct-token-only-timeout",
    }
    materialize_calls: list[bool] = []

    def _unexpected_forward_context(
        request_data: dict[str, object],
        *,
        materialize: bool,
        trace_callback=None,
    ):
        materialize_calls.append(materialize)
        return None

    downloader._download_via_forward_context = _unexpected_forward_context  # type: ignore[method-assign]

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1
    assert materialize_calls == [False]


def test_forward_video_metadata_only_direct_public_token_file_not_found_skips_materialize() -> None:
    stale_path = r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-09\Ori\forward-metadata-only.mp4"
    fast_client = _ForwardMetadataOnlyVideoClient(stale_path)
    client = _MissingPublicFileClient()
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("forward-metadata-only.mp4"), days=180),
        stale_path,
    )
    request["download_hint"]["file_id"] = "NTV2COMPAT.forward-metadata-only"

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_prefetched_forward_video_terminal_token_miss_is_preserved_and_skips_materialize() -> None:
    stale_path = r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-09\Ori\forward-prefetched-metadata-only.mp4"
    fast_client = _ForwardMetadataOnlyVideoClient(stale_path)
    client = _MissingPublicFileClient()
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    downloader._public_token_executor = _ImmediateExecutor()
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("forward-prefetched-metadata-only.mp4"), days=180),
        stale_path,
    )
    request["download_hint"]["file_id"] = "NTV2COMPAT.forward-prefetched-metadata-only"

    downloader._schedule_request_direct_public_token_prefetch(request)
    cache_key = downloader._public_token_prefetch_key(
        request_data=request,
        action="get_file",
        token="NTV2COMPAT.forward-prefetched-metadata-only",
    )
    cached, future = downloader._public_token_prefetch_state(cache_key)

    assert future is None
    assert isinstance(cached, dict)
    assert isinstance(cached.get("payload"), dict)
    assert cached["payload"]["_known_missing_classification"] == "qq_expired_after_napcat"
    assert cached["resolver"] == "qq_expired_after_napcat"

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_old_forward_video_materialize_timeout_is_classified_as_expired() -> None:
    fast_client = _OldForwardMaterializeOnlyTimeoutClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-materialize-timeout.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-materialize-timeout.mp4",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_old_forward_video_materialize_empty_is_classified_as_expired() -> None:
    fast_client = _OldForwardEmptyClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-materialize-empty.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-materialize-empty.mp4",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_old_forward_video_materialize_zero_local_is_classified_as_expired() -> None:
    temp_root = _workspace_temp_dir()
    try:
        zero_path = temp_root / "zero" / "old-forward-zero-local.mp4"
        zero_path.parent.mkdir(parents=True, exist_ok=True)
        zero_path.write_bytes(b"")
        fast_client = _OldForwardZeroLocalClient(str(zero_path))
        downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
        request = _set_forward_stale_local_path(
            _mark_request_old(_build_forward_video_request("old-forward-zero-local.mp4"), days=240),
            r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-zero-local.mp4",
        )

        resolved = downloader.resolve_for_export(request)

        assert resolved == (None, "qq_expired_after_napcat")
        assert len(fast_client.calls) == 1
        assert fast_client.calls[0].get("materialize") is False
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_old_forward_video_public_not_found_is_classified_as_expired() -> None:
    client = _MissingPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-public-not-found.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-public-not-found.mp4",
    )
    request["download_hint"]["file_id"] = "/fileid/old-forward-public-not-found"
    payload = {
        "public_action": "get_file",
        "public_file_token": "old-forward-public-not-found-token",
        "file_name": "old-forward-public-not-found.mp4",
        "asset_type": "video",
        "file_id": "/fileid/old-forward-public-not-found",
    }

    resolved = downloader._resolve_from_public_token(payload, request=request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1


def test_old_forward_speech_public_not_found_is_classified_as_expired() -> None:
    client = _MissingPublicRecordClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_request("old-forward-public-not-found.mp3"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Audio\2025-05\Ori\old-forward-public-not-found.mp3",
    )
    request["asset_type"] = "speech"
    payload = {
        "public_action": "get_record",
        "public_file_token": "old-forward-public-not-found-token",
        "file_name": "old-forward-public-not-found.mp3",
        "asset_type": "speech",
    }

    resolved = downloader._resolve_from_public_token(payload, request=request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_record_calls == 1


def test_exact_friend_top_level_speech_shape_currently_classifies_as_background_after_cached_retry_path() -> None:
    payload = {
        "asset_type": "speech",
        "file_name": "exact-friend-speech.amr",
        "public_action": "get_record",
        "public_file_token": "EhQExactSpeechToken",
        "file_id": "EhQExactSpeechToken",
        "file": "",
        "url": "",
    }
    fast_client = _StaticContextPayloadClient(payload)
    client = _MissingPublicRecordClient()
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    request = _mark_request_old(
        {
            "asset_type": "speech",
            "file_name": "exact-friend-speech.amr",
            "source_path": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Ptt\2025-12\Ori\exact-friend-speech.amr",
            "download_hint": {
                "file_id": "EhQExactSpeechToken",
                "message_id_raw": "7615594950568855051",
                "element_id": "7615594950568855050",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        },
        days=100,
    )
    events: list[dict[str, object]] = []

    resolved = downloader.resolve_for_export(request, trace_callback=events.append)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_record_calls == 2
    assert len(fast_client.media_calls) == 1
    speech_events = [event for event in events if event.get("asset_type") == "speech"]
    assert any(
        event.get("substep") == "public_token_get_record" and event.get("status") == "error"
        for event in speech_events
    )
    assert any(
        event.get("substep") == "public_token_get_record_fallback" and event.get("status") == "error"
        for event in speech_events
    )
    assert any(
        event.get("substep") == "context_hydration" and event.get("status") == "ok"
        for event in speech_events
    )
    assert any(
        event.get("substep") == "public_token_get_record" and event.get("status") == "cached_skip"
        for event in speech_events
    )
    assert any(
        event.get("substep") == "context_missing_classification"
        and event.get("status") == "classified_missing"
        and event.get("detail") == "qq_expired_after_napcat"
        for event in speech_events
    )


def test_exact_friend_top_level_speech_shape_second_resolve_reuses_cached_background_outcome() -> None:
    payload = {
        "asset_type": "speech",
        "file_name": "exact-friend-speech.amr",
        "public_action": "get_record",
        "public_file_token": "EhQExactSpeechToken",
        "file_id": "EhQExactSpeechToken",
        "file": "",
        "url": "",
    }
    fast_client = _StaticContextPayloadClient(payload)
    client = _MissingPublicRecordClient()
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    request = _mark_request_old(
        {
            "asset_type": "speech",
            "file_name": "exact-friend-speech.amr",
            "source_path": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Ptt\2025-12\Ori\exact-friend-speech.amr",
            "download_hint": {
                "file_id": "EhQExactSpeechToken",
                "message_id_raw": "7615594950568855051",
                "element_id": "7615594950568855050",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        },
        days=100,
    )

    first = downloader.resolve_for_export(request)
    second = downloader.resolve_for_export(request)

    assert first == (None, "qq_expired_after_napcat")
    assert second == (None, "qq_expired_after_napcat")
    assert client.get_record_calls == 2
    assert len(fast_client.media_calls) == 1


def test_old_forward_video_route_unavailable_is_classified_as_expired() -> None:
    fast_client = _UnavailableForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-unavailable.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-unavailable.mp4",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.forward_calls) == 1


def test_old_forward_video_direct_file_not_found_is_classified_as_expired() -> None:
    downloader = NapCatMediaDownloader(_MissingDirectFileClient())
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("old-forward-direct-not-found.mp4"), days=240),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\old-forward-direct-not-found.mp4",
    )
    request["download_hint"]["file_id"] = "/fileid/old-forward-direct-not-found"

    resolved = downloader._resolve_via_direct_file_id(request)

    assert resolved == (None, "qq_expired_after_napcat")


def test_malformed_forward_parent_with_live_remote_url_still_recovers() -> None:
    remote_root = _workspace_temp_dir()
    try:
        downloader = _RemoteMediaDownloader(remote_root)
        request = _set_forward_stale_local_path(
            _mark_request_old(_build_forward_video_request("malformed-forward-live-remote.mp4"), days=240),
            r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-05\Ori\malformed-forward-live-remote.mp4",
        )
        hint = dict(request.get("download_hint") or {})
        hint["_forward_parent"] = {
            "message_id_raw": "7617760641125573795",
            "element_id": "",
            "peer_uid": "u_example",
            "chat_type_raw": "2",
        }
        hint["remote_url"] = "https://assets.example.invalid/malformed-forward-live-remote.mp4"
        request["download_hint"] = hint

        resolved_path, resolver = downloader.resolve_for_export(request)

        assert resolver == "napcat_forward_remote_url"
        assert resolved_path is not None
        assert Path(resolved_path).exists()
    finally:
        shutil.rmtree(remote_root, ignore_errors=True)


def test_forward_video_public_token_timeout_breaker_skips_distinct_old_parents_after_limit() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)

    for index in range(downloader.FORWARD_TIMEOUT_STORM_LIMIT):
        request = _mark_request_old(
            _build_forward_video_request(f"storm-video-{index}.mp4"),
            days=90,
        )
        parent = request["download_hint"]["_forward_parent"]  # type: ignore[index]
        parent["message_id_raw"] = f"7618{index:012d}"  # type: ignore[index]
        parent["element_id"] = f"7618{index:012d}"  # type: ignore[index]
        assert downloader._call_public_action_with_token(
            "get_file",
            f"storm-token-{index}",
            request=request,
        ) is None

    skipped_request = _mark_request_old(
        _build_forward_video_request("storm-video-skip.mp4"),
        days=90,
    )
    skipped_parent = skipped_request["download_hint"]["_forward_parent"]  # type: ignore[index]
    skipped_parent["message_id_raw"] = "7618999999999999"  # type: ignore[index]
    skipped_parent["element_id"] = "7618999999999999"  # type: ignore[index]
    assert downloader._call_public_action_with_token(
        "get_file",
        "storm-token-skip",
        request=skipped_request,
    ) is None

    assert client.get_file_calls == downloader.FORWARD_TIMEOUT_STORM_LIMIT
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_timeout_storm_skip_count"] == 1


def test_forward_video_materialize_timeout_breaker_skips_distinct_old_parents_after_limit() -> None:
    fast_client = _TimeoutForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    for index in range(downloader.FORWARD_TIMEOUT_STORM_LIMIT):
        request = _mark_request_old(
            _build_forward_video_request(f"storm-mat-{index}.mp4"),
            days=90,
        )
        parent = request["download_hint"]["_forward_parent"]  # type: ignore[index]
        parent["message_id_raw"] = f"7628{index:012d}"  # type: ignore[index]
        parent["element_id"] = f"7628{index:012d}"  # type: ignore[index]
        assert downloader._download_via_forward_context(
            request,
            materialize=True,
        ) is None

    skipped_request = _mark_request_old(
        _build_forward_video_request("storm-mat-skip.mp4"),
        days=90,
    )
    skipped_parent = skipped_request["download_hint"]["_forward_parent"]  # type: ignore[index]
    skipped_parent["message_id_raw"] = "7628999999999999"  # type: ignore[index]
    skipped_parent["element_id"] = "7628999999999999"  # type: ignore[index]
    assert downloader._download_via_forward_context(
        skipped_request,
        materialize=True,
    ) is None

    assert len(fast_client.calls) == downloader.FORWARD_TIMEOUT_STORM_LIMIT
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_timeout_storm_skip_count"] == 1


def test_forward_video_direct_file_id_timeout_breaker_skips_distinct_old_parents_after_limit() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)

    for index in range(downloader.FORWARD_TIMEOUT_STORM_LIMIT):
        request = _mark_request_old(
            _build_forward_video_request(f"storm-direct-{index}.mp4"),
            days=90,
        )
        request["download_hint"]["file_id"] = f"/storm/{index}"  # type: ignore[index]
        parent = request["download_hint"]["_forward_parent"]  # type: ignore[index]
        parent["message_id_raw"] = f"7638{index:012d}"  # type: ignore[index]
        parent["element_id"] = f"7638{index:012d}"  # type: ignore[index]
        assert downloader._resolve_via_direct_file_id(request) is None

    skipped_request = _mark_request_old(
        _build_forward_video_request("storm-direct-skip.mp4"),
        days=90,
    )
    skipped_request["download_hint"]["file_id"] = "/storm/skip"  # type: ignore[index]
    skipped_parent = skipped_request["download_hint"]["_forward_parent"]  # type: ignore[index]
    skipped_parent["message_id_raw"] = "7638999999999999"  # type: ignore[index]
    skipped_parent["element_id"] = "7638999999999999"  # type: ignore[index]
    assert downloader._resolve_via_direct_file_id(skipped_request) is None

    assert client.get_file_calls == downloader.FORWARD_TIMEOUT_STORM_LIMIT
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_timeout_storm_skip_count"] == 1


def test_forward_video_public_token_timeout_breaker_groups_very_old_months_together() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    downloader.FORWARD_TIMEOUT_STORM_LIMIT = 2

    first = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("old-1.mp4"), days=240),
        message_id_raw="8618000000000001",
        element_id="8618000000000001",
    )
    second = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("old-2.mp4"), days=300),
        message_id_raw="8618000000000002",
        element_id="8618000000000002",
    )
    third = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("old-3.mp4"), days=330),
        message_id_raw="8618000000000003",
        element_id="8618000000000003",
    )

    assert downloader._call_public_action_with_token("get_file", "old-token-1", request=first) is None
    assert downloader._call_public_action_with_token("get_file", "old-token-2", request=second) is None
    assert downloader._call_public_action_with_token("get_file", "old-token-3", request=third) is None

    assert client.get_file_calls == 2
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_timeout_storm_skip_count"] == 1


def test_forward_video_materialize_slow_noop_contributes_to_breaker_for_very_old_assets() -> None:
    fast_client = _SlowMismatchedForwardClient(delay_s=0.02)
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader.FORWARD_TIMEOUT_STORM_LIMIT = 2
    downloader.FORWARD_TIMEOUT_STORM_SLOW_NOOP_ELAPSED_S = 0.01

    first = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("noop-1.mp4"), days=240),
        message_id_raw="8718000000000001",
        element_id="8718000000000001",
    )
    second = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("noop-2.mp4"), days=300),
        message_id_raw="8718000000000002",
        element_id="8718000000000002",
    )
    third = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("noop-3.mp4"), days=330),
        message_id_raw="8718000000000003",
        element_id="8718000000000003",
    )

    assert downloader._download_via_forward_context(first, materialize=True) in {None, (None, None)}
    assert downloader._download_via_forward_context(second, materialize=True) in {None, (None, None)}
    assert downloader._download_via_forward_context(third, materialize=True) in {None, (None, None)}

    assert len(fast_client.calls) == 2
    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["forward_timeout_storm_skip_count"] == 1


def test_prefetched_forward_remote_payload_is_used_before_metadata_requery() -> None:
    temp_root = _workspace_temp_dir()
    downloader = _RemoteMediaDownloader(temp_root / "remote_cache")
    request = _build_forward_request("prefetched-forward.jpg")
    key = downloader._request_key(request)
    downloader._prefetched_forward_media_payloads[key] = {
        "asset_type": "image",
        "file_name": "prefetched-forward.jpg",
        "remote_url": "https://example.invalid/prefetched-forward.jpg",
    }

    def _unexpected_forward_context(*args, **kwargs):
        raise AssertionError("forward metadata hydration should not re-run when a prefetched payload exists")

    downloader._download_via_forward_context = _unexpected_forward_context  # type: ignore[method-assign]

    try:
        resolved, resolver = downloader.resolve_for_export(request)
        assert resolved is not None
        assert resolver == "napcat_forward_remote_url"
        assert resolved.exists()
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_prefetched_forward_public_token_is_used_before_metadata_requery() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_request("prefetched-forward-token.jpg")
    key = downloader._request_key(request)
    downloader._prefetched_forward_media_payloads[key] = {
        "asset_type": "image",
        "file_name": "prefetched-forward-token.jpg",
        "public_file_token": "public-token",
    }

    def _unexpected_forward_context(*args, **kwargs):
        raise AssertionError("forward metadata hydration should not re-run when a prefetched payload exists")

    downloader._download_via_forward_context = _unexpected_forward_context  # type: ignore[method-assign]
    downloader._resolve_from_public_token = (  # type: ignore[method-assign]
        lambda payload, **kwargs: (Path(__file__).resolve(), "napcat_get_image")
    )

    resolved, resolver = downloader.resolve_for_export(request)

    assert resolved == Path(__file__).resolve()
    assert resolver == "napcat_get_image"


def test_forward_match_prefers_remote_url_before_public_token() -> None:
    temp_root = _workspace_temp_dir()
    downloader = _RemoteMediaDownloader(temp_root / "remote_cache")
    request = _build_forward_request("forward-remote-first.jpg")
    public_token_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _unexpected_public_token(*args, **kwargs):
        public_token_calls.append((args, kwargs))
        raise AssertionError("public token action should not run when forward remote URL already succeeds")

    downloader._resolve_from_public_token = _unexpected_public_token  # type: ignore[method-assign]

    try:
        resolved, matched = downloader._pick_forward_asset_match(
            request,
            [
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": "forward-remote-first.jpg",
                    "remote_url": "https://example.invalid/forward-remote-first.jpg",
                    "public_action": "get_image",
                    "public_file_token": "public-token",
                }
            ],
        )
        assert matched is not None
        assert resolved[0] is not None
        assert resolved[1] == "napcat_forward_remote_url"
        assert public_token_calls == []
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_forward_match_prefers_local_path_before_public_token() -> None:
    temp_root = _workspace_temp_dir()
    downloader = NapCatMediaDownloader(_DummyClient())
    local_path = temp_root / "forward-local-first.jpg"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(b"local")
    request = _build_forward_request("forward-local-first.jpg")

    try:
        resolved, matched = downloader._pick_forward_asset_match(
            request,
            [
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": "forward-local-first.jpg",
                    "public_action": "get_image",
                    "public_file_token": "public-token",
                },
                {
                    "asset_type": "image",
                    "asset_role": "forward_media",
                    "file_name": "forward-local-first.jpg",
                    "file": str(local_path),
                },
            ],
        )
        assert matched is not None
        assert resolved[0] == local_path.resolve()
        assert resolved[1] == "napcat_forward_hydrated"
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_recent_forward_video_missing_is_not_shared_without_terminal_expired_resolver() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_video_request("recent-forward-video.mp4")
    request["timestamp_ms"] = int(
        (datetime.now(timezone.utc) - timedelta(days=20)).timestamp() * 1000
    )

    assert downloader._should_share_missing_outcome(request, resolver=None) is False
    assert downloader._should_share_missing_outcome(
        request,
        resolver="missing_after_napcat",
    ) is False
    assert downloader._should_share_missing_outcome(
        request,
        resolver="qq_expired_after_napcat",
    ) is True


def test_recent_forward_speech_missing_is_not_shared_without_terminal_expired_resolver() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_speech_request("recent-forward-speech.mp3")
    request["timestamp_ms"] = int(
        (datetime.now(timezone.utc) - timedelta(days=20)).timestamp() * 1000
    )

    assert downloader._should_share_missing_outcome(request, resolver=None) is False
    assert downloader._should_share_missing_outcome(
        request,
        resolver="missing_after_napcat",
    ) is False
    assert downloader._should_share_missing_outcome(
        request,
        resolver="qq_expired_after_napcat",
    ) is True


def test_classify_forward_missing_marks_forward_image_with_dead_remote_and_failed_public_token_as_background_without_age_gate() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_base_url="http://127.0.0.1:3000",
    )
    request = _mark_request_old(_build_forward_request("forward-dead.jpg"), days=10)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-token&spec=0"
    )
    payload = {
        "asset_type": "image",
        "public_action": "get_image",
        "public_file_token": "dead-image-token",
    }
    cache_key = (
        "image",
        downloader._normalized_match_url(
            "http://127.0.0.1:3000/download?appid=1407&fileid=dead-token&spec=0"
        ),
    )
    downloader._remote_media_resolution_cache[cache_key] = None
    downloader._remember_remote_media_failure_reason(
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-token&spec=0",
        "expired_remote",
    )
    downloader._remember_remote_media_failure_reason(
        "http://127.0.0.1:3000/download?appid=1407&fileid=dead-token&spec=0",
        "unsupported_local_download",
    )
    downloader._public_token_action_outcomes[("get_image", "dead-image-token")] = None

    classification = downloader._classify_forward_missing(request, payload=payload)

    assert classification == "qq_expired_after_napcat"


def test_classify_forward_missing_keeps_forward_image_without_terminal_evidence_actionable_even_when_old() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _mark_request_old(_build_forward_request("old-forward-no-terminal.jpg"), days=240)
    payload = None

    classification = downloader._classify_forward_missing(request, payload=payload)

    assert classification is None


def test_resolve_remote_url_only_projects_relative_download_routes() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_base_url="http://127.0.0.1:3000",
    )

    assert (
        downloader._resolve_remote_url("/download?appid=1407&fileid=abc&spec=0")
        == "http://127.0.0.1:3000/download?appid=1407&fileid=abc&spec=0"
    )
    assert (
        downloader._resolve_remote_url(
            "/gchatpic_new/3348513412/922065597-2397162384-A8D7F0A6BDE1314277980B64829EE245/0?term=255&is_origin=0"
        )
        is None
    )


def test_resolve_for_export_does_not_attempt_remote_download_for_relative_non_download_image_url() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_base_url="http://127.0.0.1:3000",
    )
    request = {
        "asset_type": "image",
        "file_name": "old-image.png",
        "timestamp_ms": 1757142395000,
        "download_hint": {
            "file_id": "2397162384",
            "url": "/gchatpic_new/3348513412/922065597-2397162384-A8D7F0A6BDE1314277980B64829EE245/0?term=255&is_origin=0",
            "message_id_raw": "7616405939521354983",
            "element_id": "7616405939521354982",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }
    downloader._call_public_action_with_token = (  # type: ignore[method-assign]
        lambda *args, **kwargs: {
            "asset_type": "image",
            "public_action": "get_image",
            "public_file_token": "2397162384",
            "file_name": "old-image.png",
            "url": "/gchatpic_new/3348513412/922065597-2397162384-A8D7F0A6BDE1314277980B64829EE245/0?term=255&is_origin=0",
        }
    )
    downloader._download_remote_media = (  # type: ignore[method-assign]
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("relative non-download image URL should not trigger remote download"))
    )
    downloader._resolve_via_context_only = lambda *_args, **_kwargs: (None, None)  # type: ignore[method-assign]

    assert downloader.resolve_for_export(request) == (None, None)


def test_classify_forward_missing_marks_forward_image_with_dead_remote_and_metadata_timeout_as_background_when_no_public_or_local_handle_exists() -> None:
    fast_client = _ForwardImageMetadataTimeoutClient()
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        fast_client=fast_client,
        remote_base_url="http://127.0.0.1:3000",
    )
    request = _mark_request_old(_build_forward_request("forward-dead-timeout.jpg"), days=7)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-timeout&spec=0"
    )
    cache_key = (
        "image",
        downloader._normalized_match_url(
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-timeout&spec=0"
        ),
    )
    downloader._remote_media_resolution_cache[cache_key] = None
    downloader._remember_remote_media_failure_reason(
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-timeout&spec=0",
        "expired_remote",
    )
    downloader._remember_remote_media_failure_reason(
        "http://127.0.0.1:3000/download?appid=1407&fileid=dead-forward-timeout&spec=0",
        "unsupported_local_download",
    )

    assert downloader._download_via_forward_context(request, materialize=False) is None

    classification = downloader._classify_forward_missing(request)

    assert classification == "qq_expired_after_napcat"
    assert len(fast_client.calls) == 1
    assert [bool(call.get("materialize")) for call in fast_client.calls] == [False]


def test_classify_forward_missing_keeps_forward_image_with_dead_remote_but_no_terminal_route_signal_actionable() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _mark_request_old(_build_forward_request("forward-dead-no-route-proof.jpg"), days=7)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-no-route-proof&spec=0"
    )
    cache_key = (
        "image",
        downloader._normalized_match_url(
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-no-route-proof&spec=0"
        ),
    )
    downloader._remote_media_resolution_cache[cache_key] = None

    classification = downloader._classify_forward_missing(request)

    assert classification is None


def test_classify_forward_missing_keeps_forward_image_without_remote_or_public_handle_actionable_even_after_metadata_timeout() -> None:
    fast_client = _TimeoutForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _mark_request_old(_build_forward_request("forward-timeout-no-remote.jpg"), days=7)

    assert downloader._download_via_forward_context(request, materialize=False) is None

    classification = downloader._classify_forward_missing(request)

    assert classification is None
    assert len(fast_client.calls) == 1


def test_resolve_for_export_classifies_prefetched_forward_image_with_dead_remote_and_metadata_timeout_as_background() -> None:
    downloader = NapCatMediaDownloader(_DummyClient(), remote_base_url="http://127.0.0.1:3000")
    request = _mark_request_old(_build_forward_request("forward-prefetched-dead.jpg"), days=7)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-prefetched&spec=0"
    )
    key = downloader._request_key(request)
    timeout_cache_key = downloader._forward_context_timeout_key(request, materialize=False)
    assert timeout_cache_key is not None
    downloader._prefetched_forward_media[key] = (None, None)
    downloader._prefetched_forward_media_payloads[key] = {
        "asset_type": "image",
        "file_name": "forward-prefetched-dead.jpg",
        "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-prefetched&spec=0",
        "remote_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-prefetched&spec=0",
    }
    downloader._forward_context_timeout_cache.add(timeout_cache_key)
    downloader._remember_remote_media_failure_reason(
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-prefetched&spec=0",
        "expired_remote",
    )
    downloader._remember_remote_media_failure_reason(
        "http://127.0.0.1:3000/download?appid=1407&fileid=dead-forward-prefetched&spec=0",
        "unsupported_local_download",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")


def _build_forward_image_terminal_remote_transport() -> httpx.MockTransport:
    def _handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "multimedia.nt.qq.com.cn" in url:
            return httpx.Response(
                200,
                headers={"content-type": "application/json; charset=utf-8"},
                json={
                    "retcode": -5503042,
                    "message": "file has expired",
                    "wording": "file has expired",
                },
                request=request,
            )
        if "127.0.0.1:3000" in url or "127.0.0.1:6099" in url:
            return httpx.Response(
                200,
                headers={"content-type": "application/json; charset=utf-8"},
                json={
                    "status": "failed",
                    "retcode": 200,
                    "message": "不支持的Api download",
                    "wording": "不支持的Api download",
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    return httpx.MockTransport(_handler)


def test_forward_image_targeted_miss_with_terminal_remote_evidence_is_classified_as_expired() -> None:
    fast_client = _ForwardImageTargetedMissClient()
    temp_root = _workspace_temp_dir()
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        fast_client=fast_client,
        remote_cache_dir=temp_root / "remote_cache",
        remote_base_url="http://127.0.0.1:3000",
        remote_transport=_build_forward_image_terminal_remote_transport(),
    )
    request = _mark_request_old(_build_forward_request("forward-targeted-miss.jpg"), days=7)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-targeted-miss&spec=0"
    )

    try:
        resolved = downloader.resolve_for_export(request)

        assert resolved == (None, "qq_expired_after_napcat")
        assert fast_client.calls == []
        reasons = downloader._forward_remote_failure_reasons(request)
        assert reasons == {
            "original": "expired_remote",
            "projected": "unsupported_local_download",
        }
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_forward_image_targeted_miss_with_projected_local_download_recovers_without_materialize() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={
                "content-type": "image/gif"
                if "127.0.0.1:3000" in str(request.url)
                else "application/json; charset=utf-8"
            },
            content=b"GIF89a"
            if "127.0.0.1:3000" in str(request.url)
            else b'{"retcode":-5503042,"message":"file has expired"}',
            request=request,
        )
    )
    fast_client = _ForwardImageTargetedMissClient()
    temp_root = _workspace_temp_dir()
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        fast_client=fast_client,
        remote_cache_dir=temp_root / "remote_cache",
        remote_base_url="http://127.0.0.1:3000",
        remote_transport=transport,
    )
    request = _mark_request_old(_build_forward_request("forward-targeted-hit.gif"), days=7)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-targeted-hit&spec=0"
    )

    try:
        resolved = downloader.resolve_for_export(request)
        assert resolved[0] is not None
        assert resolved[1] == "napcat_forward_remote_url"
        assert fast_client.calls == []
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_forward_image_metadata_timeout_does_not_force_targeted_materialize() -> None:
    fast_client = _ForwardImageMetadataTimeoutClient()
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        fast_client=fast_client,
        remote_base_url="http://127.0.0.1:3000",
    )
    request = _mark_request_old(_build_forward_request("forward-timeout-success.jpg"), days=7)
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-timeout-success&spec=0"
    )
    downloader._remember_remote_media_failure_reason(
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-forward-timeout-success&spec=0",
        "expired_remote",
    )
    downloader._remember_remote_media_failure_reason(
        "http://127.0.0.1:3000/download?appid=1407&fileid=dead-forward-timeout-success&spec=0",
        "unsupported_local_download",
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert [bool(call.get("materialize")) for call in fast_client.calls] in ([], [False])


def test_resolve_from_forward_payload_candidate_prefers_existing_local_file_before_public_token_for_forward_image() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    workspace = _workspace_temp_dir()
    local_path = workspace / "forward-local-hit.png"
    local_path.write_bytes(b"png")
    request = _build_forward_request("forward-local-hit.png")
    payload = {
        "asset_type": "image",
        "file_name": "forward-local-hit.png",
        "file": str(local_path),
        "url": str(local_path),
        "public_action": "get_image",
        "public_file_token": "forward-local-token",
        "_forward_targeted_mode": "metadata_only",
    }

    downloader._resolve_from_public_token = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("forward image should use local payload before public token"))
    )

    try:
        resolved = downloader._resolve_from_forward_payload_candidate(request, payload=payload)
        assert resolved == (local_path.resolve(), "napcat_context_hydrated")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_resolve_for_export_classifies_forward_image_missing_local_payload_before_public_token_retry() -> None:
    request = _build_forward_request("forward-missing-local.png")
    request["source_path"] = r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-03\Ori\forward-missing-local.png"
    request["download_hint"]["file_id"] = "forward-missing-local-file-id"
    request["download_hint"]["url"] = "/download?appid=1407&fileid=forward-missing-local-file-id&spec=0"
    payload = {
        "asset_type": "image",
        "file_name": "forward-missing-local.png",
        "file": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-03\Ori\forward-missing-local.png",
        "url": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-03\Ori\forward-missing-local.png",
        "remote_url": "/download?appid=1407&fileid=forward-missing-local-file-id&spec=0",
        "public_action": "get_image",
        "public_file_token": "forward-missing-local-public-token",
    }
    fast_client = _ForwardImageMissingLocalPayloadClient(payload)
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        fast_client=fast_client,
        remote_base_url="http://127.0.0.1:3000",
    )
    cache_key = (
        "image",
        downloader._normalized_match_url(
            "http://127.0.0.1:3000/download?appid=1407&fileid=forward-missing-local-file-id&spec=0"
        ),
    )
    downloader._remote_media_resolution_cache[cache_key] = None
    downloader._remember_remote_media_failure_reason(
        "http://127.0.0.1:3000/download?appid=1407&fileid=forward-missing-local-file-id&spec=0",
        "unsupported_local_download",
    )
    downloader._call_public_action_with_token = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("forward image public token should be skipped after metadata proves missing"))
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.calls) == 1
    assert [bool(call.get("materialize")) for call in fast_client.calls] == [False]


def test_classify_missing_from_payload_marks_top_level_image_with_stale_local_and_terminal_remote_evidence_as_background() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_base_url="http://127.0.0.1:3000",
    )
    request = {
        "asset_type": "image",
        "file_name": "top-terminal.jpg",
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2026-01\Ori\top-terminal.jpg",
        "download_hint": {
            "file_id": "dead-top-terminal-token",
            "url": "/download?appid=1407&fileid=dead-top-terminal-token&spec=0",
            "message_id_raw": "7616396026189795572",
            "element_id": "7616396026189795571",
            "peer_uid": "922065597",
            "chat_type_raw": "2",
        },
    }
    payload = {
        "asset_type": "image",
        "public_action": "get_image",
        "public_file_token": "dead-top-terminal-token",
        "file_name": "top-terminal.jpg",
        "file_id": "dead-top-terminal-token",
        "remote_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-top-terminal-token&rkey=test",
    }
    downloader._remember_remote_media_failure_reason(
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dead-top-terminal-token&rkey=test",
        "expired_remote",
    )
    downloader._remember_remote_media_failure_reason(
        "http://127.0.0.1:3000/download?appid=1407&fileid=dead-top-terminal-token&spec=0",
        "unsupported_local_download",
    )

    classification = downloader._classify_missing_from_payload(
        payload,
        old_bucket=None,
        request=request,
    )

    assert classification == "qq_expired_after_napcat"


def test_resolve_from_public_token_marks_recent_top_level_image_dead_public_remote_as_background_without_age_gate() -> None:
    downloader = NapCatMediaDownloader(
        _DummyClient(),
        remote_base_url="http://127.0.0.1:3000",
    )
    request = {
        "asset_type": "image",
        "file_name": "recent-top-terminal.jpg",
        "timestamp_ms": int(datetime(2026, 1, 6, 9, 33, 24, tzinfo=timezone.utc).timestamp() * 1000),
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-01\Ori\recent-top-terminal.jpg",
        "download_hint": {
            "file_id": "2229606560",
            "url": "/gchatpic_new/1002303945/922065597-2229606560-RECENTTOPTERMINAL/0?term=255&is_origin=0",
            "message_id_raw": "7616401486576431266",
            "element_id": "7616401486576431265",
            "peer_uid": "922065597",
            "chat_type_raw": "2",
        },
    }
    payload = {
        "asset_type": "image",
        "public_action": "get_image",
        "public_file_token": "recent-top-terminal-token",
        "file_name": "recent-top-terminal.jpg",
        "file_id": "2229606560",
        "remote_url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-RECENTTOPTERMINAL/0",
    }
    downloader._remember_remote_media_failure_reason(
        "https://gchat.qpic.cn/gchatpic_new/0/0-0-RECENTTOPTERMINAL/0",
        "expired_remote",
    )
    downloader._public_token_action_outcomes[("get_image", "recent-top-terminal-token")] = payload

    resolved = downloader._resolve_from_public_token(
        payload,
        old_bucket=None,
        expired_candidate=False,
        request=request,
    )

    assert resolved == (None, "qq_expired_after_napcat")


def test_recent_forward_video_blank_public_payload_is_classified_as_expired_without_age_gate() -> None:
    client = _MissingPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("recent-forward-blank-public.mp4"), days=7),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-03\Ori\recent-forward-blank-public.mp4",
    )
    payload = {
        "public_action": "get_file",
        "public_file_token": "recent-forward-blank-public-token",
        "file_name": "recent-forward-blank-public.mp4",
        "asset_type": "video",
        "file_id": "/fileid/recent-forward-blank-public",
    }

    resolved = downloader._resolve_from_public_token(payload, request=request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert client.get_file_calls == 1


def test_recent_forward_video_direct_file_not_found_is_classified_as_expired_without_age_gate() -> None:
    downloader = NapCatMediaDownloader(_MissingDirectFileClient())
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("recent-forward-direct-not-found.mp4"), days=7),
        r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-03\Ori\recent-forward-direct-not-found.mp4",
    )
    request["download_hint"]["file_id"] = "/fileid/recent-forward-direct-not-found"

    resolved = downloader._resolve_via_direct_file_id(request)

    assert resolved == (None, "qq_expired_after_napcat")


def test_forward_video_metadata_local_missing_is_classified_before_targeted_materialize_without_direct_file_id() -> None:
    stale_path = r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2025-09\Ori\forward-metadata-local-missing.mp4"
    fast_client = _ForwardMetadataOnlyVideoClient(stale_path)
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _set_forward_stale_local_path(
        _mark_request_old(_build_forward_video_request("forward-metadata-local-missing.mp4"), days=180),
        stale_path,
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("materialize") is False


def test_forward_image_metadata_uses_parent_scoped_payload_across_siblings() -> None:
    fast_client = _TargetedForwardMetadataClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request_a = _build_forward_request("A1111111111111111111111111111111.jpg")
    request_a["md5"] = "a1111111111111111111111111111111"
    request_b = _build_forward_request("B2222222222222222222222222222222.jpg")
    request_b["md5"] = "b2222222222222222222222222222222"

    resolved_a = downloader.resolve_for_export(request_a)
    resolved_b = downloader.resolve_for_export(request_b)

    assert resolved_a[0] is not None
    assert resolved_b[0] is not None
    assert len(fast_client.calls) == 1
    assert fast_client.calls[0].get("file_name") in {None, ""}
    assert fast_client.calls[0].get("md5") in {None, ""}
    assert fast_client.calls[0].get("file_id") in {None, ""}
    assert fast_client.calls[0].get("url") in {None, ""}


def test_forward_file_shared_request_key_requires_strong_identity() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_video_request("generic-name.mp4")

    assert downloader._shared_request_key(request) is None

    request["md5"] = "abcd1234"
    assert downloader._shared_request_key(request) is not None


def test_request_scoped_public_timeout_key_is_candidate_aware() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_video_request("candidate-a.mp4")
    key_a = downloader._request_scoped_public_action_timeout_key(
        request,
        action="get_file",
        token="token-a",
    )
    request_b = _build_forward_video_request("candidate-b.mp4")
    key_b = downloader._request_scoped_public_action_timeout_key(
        request_b,
        action="get_file",
        token="token-b",
    )

    assert key_a is not None
    assert key_b is not None
    assert key_a != key_b


def test_forward_image_public_timeout_key_is_parent_scoped() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request_a = _set_forward_parent_identity(
        _build_forward_request("candidate-a.jpg"),
        message_id_raw="parent-1",
        element_id="element-1",
    )
    request_b = _set_forward_parent_identity(
        _build_forward_request("candidate-b.jpg"),
        message_id_raw="parent-1",
        element_id="element-1",
    )

    key_a = downloader._request_scoped_public_action_timeout_key(
        request_a,
        action="get_image",
        token="token-a",
    )
    key_b = downloader._request_scoped_public_action_timeout_key(
        request_b,
        action="get_image",
        token="token-b",
    )

    assert key_a is not None
    assert key_a == key_b


def test_forward_video_public_timeout_key_remains_request_scoped_for_same_parent_new_token() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request_a = _set_forward_parent_identity(
        _build_forward_video_request("candidate-a.mp4"),
        message_id_raw="parent-1",
        element_id="element-1",
    )
    request_b = _set_forward_parent_identity(
        _build_forward_video_request("candidate-a.mp4"),
        message_id_raw="parent-1",
        element_id="element-1",
    )

    key_a = downloader._request_scoped_public_action_timeout_key(
        request_a,
        action="get_file",
        token="token-a",
    )
    key_b = downloader._request_scoped_public_action_timeout_key(
        request_b,
        action="get_file",
        token="token-b",
    )

    assert key_a is not None
    assert key_b is not None
    assert key_a != key_b


def test_forward_video_public_timeout_key_remains_request_scoped_for_same_parent_new_file() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request_a = _set_forward_parent_identity(
        _build_forward_video_request("candidate-a.mp4"),
        message_id_raw="parent-1",
        element_id="element-1",
    )
    request_b = _set_forward_parent_identity(
        _build_forward_video_request("candidate-b.mp4"),
        message_id_raw="parent-1",
        element_id="element-1",
    )
    request_b["md5"] = "different-md5"
    request_b["download_hint"]["file_id"] = "/scope/video/b"  # type: ignore[index]

    key_a = downloader._request_scoped_public_action_timeout_key(
        request_a,
        action="get_file",
        token="token-a",
    )
    key_b = downloader._request_scoped_public_action_timeout_key(
        request_b,
        action="get_file",
        token="token-a",
    )

    assert key_a is not None
    assert key_b is not None
    assert key_a != key_b


def test_forward_file_public_timeout_key_remains_request_scoped_for_same_parent_new_token() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request_a = _set_forward_parent_identity(
        _build_forward_video_request("candidate-a.bin"),
        message_id_raw="parent-1",
        element_id="element-1",
    )
    request_a["asset_type"] = "file"
    request_b = _set_forward_parent_identity(
        _build_forward_video_request("candidate-a.bin"),
        message_id_raw="parent-1",
        element_id="element-1",
    )
    request_b["asset_type"] = "file"

    key_a = downloader._request_scoped_public_action_timeout_key(
        request_a,
        action="get_file",
        token="token-a",
    )
    key_b = downloader._request_scoped_public_action_timeout_key(
        request_b,
        action="get_file",
        token="token-b",
    )

    assert key_a is not None
    assert key_b is not None
    assert key_a != key_b


def test_forward_video_parent_scoped_timeout_skips_same_parent_new_token_for_aged_forward() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request_a = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-a.mp4"), days=90),
        message_id_raw="parent-aged-1",
        element_id="element-aged-1",
    )
    request_b = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-b.mp4"), days=90),
        message_id_raw="parent-aged-1",
        element_id="element-aged-1",
    )

    assert downloader._call_public_action_with_token("get_file", "token-a", request=request_a) is None
    assert downloader._call_public_action_with_token("get_file", "token-b", request=request_b) is None

    assert client.get_file_calls == 1


def test_forward_video_parent_scoped_timeout_skips_same_parent_new_file_for_aged_forward() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request_a = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-a.mp4"), days=90),
        message_id_raw="parent-aged-2",
        element_id="element-aged-2",
    )
    request_b = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-b.mp4"), days=90),
        message_id_raw="parent-aged-2",
        element_id="element-aged-2",
    )
    request_b["md5"] = "aged-b-md5"
    request_b["download_hint"]["file_id"] = "/scope/video/b"  # type: ignore[index]

    assert downloader._call_public_action_with_token("get_file", "token-a", request=request_a) is None
    assert downloader._call_public_action_with_token("get_file", "token-a", request=request_b) is None

    assert client.get_file_calls == 1


def test_forward_file_parent_scoped_timeout_skips_same_parent_new_token_for_aged_forward() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request_a = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-a.bin"), days=90),
        message_id_raw="parent-aged-3",
        element_id="element-aged-3",
    )
    request_a["asset_type"] = "file"
    request_b = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-a.bin"), days=90),
        message_id_raw="parent-aged-3",
        element_id="element-aged-3",
    )
    request_b["asset_type"] = "file"

    assert downloader._call_public_action_with_token("get_file", "token-a", request=request_a) is None
    assert downloader._call_public_action_with_token("get_file", "token-b", request=request_b) is None

    assert client.get_file_calls == 1


def test_forward_video_parent_scoped_timeout_does_not_leak_across_parents() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request_a = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-a.mp4"), days=90),
        message_id_raw="parent-aged-4a",
        element_id="element-aged-4a",
    )
    request_b = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-b.mp4"), days=90),
        message_id_raw="parent-aged-4b",
        element_id="element-aged-4b",
    )

    assert downloader._call_public_action_with_token("get_file", "token-a", request=request_a) is None
    assert downloader._call_public_action_with_token("get_file", "token-b", request=request_b) is None

    assert client.get_file_calls == 2


def test_forward_video_parent_scoped_timeout_is_not_enabled_for_recent_forward() -> None:
    client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(client)
    request_a = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("recent-a.mp4"), days=7),
        message_id_raw="parent-recent-1",
        element_id="element-recent-1",
    )
    request_b = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("recent-b.mp4"), days=7),
        message_id_raw="parent-recent-1",
        element_id="element-recent-1",
    )

    assert downloader._call_public_action_with_token("get_file", "token-a", request=request_a) is None
    assert downloader._call_public_action_with_token("get_file", "token-b", request=request_b) is None

    assert client.get_file_calls == 2


def test_forward_video_parent_scoped_timeout_does_not_block_direct_file_id_recovery_for_same_parent() -> None:
    timeout_client = _TimeoutPublicFileClient()
    downloader = NapCatMediaDownloader(timeout_client)
    timed_out_request = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-a.mp4"), days=90),
        message_id_raw="parent-aged-5",
        element_id="element-aged-5",
    )

    assert downloader._call_public_action_with_token("get_file", "token-a", request=timed_out_request) is None
    assert timeout_client.get_file_calls == 1

    success_client = _SuccessPublicFileClient()
    downloader._client = success_client  # type: ignore[assignment]
    recover_request = _set_forward_parent_identity(
        _mark_request_old(_build_forward_video_request("aged-b.mp4"), days=90),
        message_id_raw="parent-aged-5",
        element_id="element-aged-5",
    )
    recover_request["download_hint"]["file_id"] = "/scope/video/recover"  # type: ignore[index]

    resolved = downloader._resolve_via_direct_file_id(recover_request)

    assert resolved is not None
    assert resolved[0] is not None
    assert resolved[1] == "napcat_segment_file_id_get_file"
    assert success_client.get_file_calls == 1


def test_forward_image_public_timeout_skips_sibling_public_retry_after_first_timeout() -> None:
    client = _TimeoutPublicImageClient()
    payload_a = {
        "asset_type": "image",
        "file_name": "timeout-a.jpg",
        "public_action": "get_image",
        "public_file_token": "timeout-token-a",
    }
    payload_b = {
        "asset_type": "image",
        "file_name": "timeout-b.jpg",
        "public_action": "get_image",
        "public_file_token": "timeout-token-b",
    }
    fast_client = _ForwardImageMissingLocalPayloadClient(payload_a)
    downloader = NapCatMediaDownloader(client, fast_client=fast_client)
    request_a = _set_forward_parent_identity(
        _build_forward_request("timeout-a.jpg"),
        message_id_raw="parent-timeout",
        element_id="element-timeout",
    )
    request_b = _set_forward_parent_identity(
        _build_forward_request("timeout-b.jpg"),
        message_id_raw="parent-timeout",
        element_id="element-timeout",
    )

    resolved_a = downloader.resolve_for_export(request_a)
    fast_client.payload = dict(payload_b)
    resolved_b = downloader.resolve_for_export(request_b)

    assert resolved_a[0] is None
    assert resolved_b[0] is None
    assert client.get_image_calls == 1


def test_remote_media_download_prepares_cache_dir_on_first_use() -> None:
    temp_root = _workspace_temp_dir()
    downloader = _RemoteMediaDownloader(temp_root / "remote_cache")

    try:
        resolved, used_cached = asyncio.run(
            downloader._download_remote_media_async(
                asset_type="image",
                file_name="example.jpg",
                hint={"url": "https://example.invalid/example.jpg"},
            )
        )
        assert resolved is not None
        assert used_cached is False
        resolved_path = Path(resolved)
        assert resolved_path.exists()
        assert resolved_path.read_bytes() == b"fake-bytes"
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_remote_sticker_download_prepares_cache_dir_on_first_use() -> None:
    temp_root = _workspace_temp_dir()
    downloader = _RemoteMediaDownloader(temp_root / "remote_cache")
    downloader._download_remote_payload = lambda remote_url: b"gif-bytes"  # type: ignore[method-assign]

    try:
        resolved = downloader._download_remote_sticker(
            {"remote_url": "https://example.invalid/example.gif", "remote_file_name": "example.gif"},
            asset_role="dynamic",
            file_name="example.gif",
        )

        assert resolved is not None
        resolved_path = Path(resolved)
        assert resolved_path.exists()
        assert resolved_path.read_bytes() == b"gif-bytes"
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_public_token_placeholder_missing_is_classified_before_remote_attempt() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "Pic" / "2025-09" / "Ori" / "700B81F97B9D06E7999DF7504442D46C.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"")
    sibling_placeholder = source_path.parent.parent / "OriTemp" / source_path.name
    sibling_placeholder.parent.mkdir(parents=True, exist_ok=True)
    sibling_placeholder.write_bytes(b"")

    downloader = NapCatMediaDownloader(_DummyClient())
    public_token_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _public_token_returns_nothing(*args, **kwargs):
        public_token_calls.append((args, kwargs))
        return None

    downloader._call_public_action_with_token = _public_token_returns_nothing  # type: ignore[method-assign]

    try:
        result = downloader._resolve_from_public_token(
            {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": "public-token",
                "file_name": source_path.name,
            },
            old_bucket=("image", "2025-09"),
            request={
                "asset_type": "image",
                "file_name": source_path.name,
                "source_path": str(source_path),
            },
        )
        assert result is None
        assert len(public_token_calls) == 1
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_prepare_for_export_does_not_skip_remote_prefetch_for_placeholder_without_terminal_evidence() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "Pic" / "2025-09" / "Ori" / "PLACEHOLDER_B.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    sibling_placeholder = source_path.parent.parent / "OriTemp" / source_path.name
    sibling_placeholder.parent.mkdir(parents=True, exist_ok=True)
    sibling_placeholder.write_bytes(b"")

    class _CountingDownloader(NapCatMediaDownloader):
        def __init__(self) -> None:
            super().__init__(_DummyClient(), fast_client=_BatchFastClient())
            self.scheduled_requests: list[str] = []

        def _schedule_request_remote_prefetch(self, request):  # type: ignore[override]
            self.scheduled_requests.append(str(request.get("file_name") or ""))

    downloader = _CountingDownloader()
    request = {
        "asset_type": "image",
        "file_name": source_path.name,
        "source_path": str(source_path),
        "timestamp_ms": 1750000000000,
        "download_hint": {
            "message_id_raw": "7610000000000000003",
            "element_id": "7610000000000000002",
            "peer_uid": "u_example",
            "chat_type_raw": "2",
            "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=dummy&spec=0",
        },
    }

    try:
        downloader.prepare_for_export([request])
        assert downloader.scheduled_requests == [source_path.name]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_resolve_via_context_only_does_not_skip_old_placeholder_image_without_terminal_evidence() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "Pic" / "2025-09" / "Ori" / "PLACEHOLDER_CONTEXT_SKIP.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"")
    sibling_placeholder = source_path.parent.parent / "OriTemp" / source_path.name
    sibling_placeholder.parent.mkdir(parents=True, exist_ok=True)
    sibling_placeholder.write_bytes(b"")
    downloader = NapCatMediaDownloader(_DummyClient())
    downloader._remote_base_url = "http://127.0.0.1:6099"
    request = {
        "asset_type": "image",
        "file_name": source_path.name,
        "source_path": str(source_path),
        "timestamp_ms": 1750000000000,
        "download_hint": {
            "message_id_raw": "7610000000000000401",
            "element_id": "7610000000000000400",
            "peer_uid": "u_example",
            "chat_type_raw": "2",
        },
    }

    context_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def _context_returns_nothing(*args, **kwargs):
        context_calls.append((args, kwargs))
        return None

    downloader._download_via_context = _context_returns_nothing  # type: ignore[method-assign]

    try:
        resolved, resolver = downloader._resolve_via_context_only(request)
        assert resolved is None
        assert resolver is None
        assert len(context_calls) == 1
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_classify_missing_from_payload_uses_placeholder_background_only_after_remote_failure_evidence() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "Pic" / "2025-12" / "Ori" / "PLACEHOLDER_EVIDENCE.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"")
    sibling_placeholder = source_path.parent.parent / "OriTemp" / source_path.name
    sibling_placeholder.parent.mkdir(parents=True, exist_ok=True)
    sibling_placeholder.write_bytes(b"")
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "image",
        "file_name": source_path.name,
        "source_path": str(source_path),
        "download_hint": {
            "file_id": "token-placeholder",
            "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=token-placeholder&spec=0",
        },
    }
    payload = {
        "public_action": "get_image",
        "public_file_token": "token-placeholder",
        "file_name": source_path.name,
        "asset_type": "image",
    }
    try:
        assert (
            downloader._classify_missing_from_payload(
                payload,
                old_bucket=("image", "2025-12"),
                request=request,
            )
            is None
        )
        resolved_remote = downloader._resolve_remote_url(
            "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=token-placeholder&spec=0"
        )
        assert resolved_remote is not None
        downloader._remote_media_resolution_cache[resolved_remote] = None
        downloader._remote_media_failure_reasons[resolved_remote] = "unsupported_local_download"
        assert (
            downloader._classify_missing_from_payload(
                payload,
                old_bucket=("image", "2025-12"),
                request=request,
            )
            == "qq_not_downloaded_local_placeholder"
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_classify_missing_from_payload_uses_public_payload_remote_failure_for_placeholder_images() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "Pic" / "2025-12" / "Ori" / "PUBLIC_REMOTE_PLACEHOLDER.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"")
    sibling_placeholder = source_path.parent.parent / "OriTemp" / source_path.name
    sibling_placeholder.parent.mkdir(parents=True, exist_ok=True)
    sibling_placeholder.write_bytes(b"")
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "image",
        "file_name": source_path.name,
        "source_path": str(source_path),
        "download_hint": {
            "file_id": "token-placeholder",
            "url": "/gchatpic_new/3348513412/922065597-2566904540-D500DF0B776DD0EDF0B0B864B4F5A18E/0?term=255&is_origin=0",
            "message_id_raw": "7616405939521354825",
            "element_id": "7616405939521354824",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }
    payload = {
        "public_action": "get_image",
        "public_file_token": "token-placeholder",
        "file_name": source_path.name,
        "asset_type": "image",
        "remote_url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-D500DF0B776DD0EDF0B0B864B4F5A18E/0",
    }
    try:
        assert (
            downloader._classify_image_placeholder_missing_from_evidence(
                request,
                payload=payload,
            )
            is None
        )
        resolved_remote = downloader._resolve_remote_url(str(payload["remote_url"]))
        assert resolved_remote is not None
        downloader._remember_remote_media_failure_reason(resolved_remote, "http_error")
        assert (
            downloader._classify_image_placeholder_missing_from_evidence(
                request,
                payload=payload,
            )
            == "qq_not_downloaded_local_placeholder"
        )
        assert (
            downloader._classify_missing_from_payload(
                payload,
                old_bucket=("image", "2025-12"),
                request=request,
            )
            == "qq_not_downloaded_local_placeholder"
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_resolve_for_export_prefers_top_level_https_remote_over_direct_public_token_round_trip() -> None:
    class _ExplodingImageClient:
        def __init__(self) -> None:
            self.get_image_calls = 0

        def get_image(self, *args, **kwargs):
            self.get_image_calls += 1
            raise AssertionError("direct get_image should not run when a direct https media URL is already available")

    temp_root = _workspace_temp_dir()
    remote_target = temp_root / "remote_media" / "top-level-https-first.jpg"
    remote_target.parent.mkdir(parents=True, exist_ok=True)
    remote_target.write_bytes(b"remote")
    client = _ExplodingImageClient()
    downloader = NapCatMediaDownloader(client)
    request = {
        "asset_type": "image",
        "file_name": remote_target.name,
        "download_hint": {
            "file_id": "token-direct-https",
            "url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-directhttps/0",
        },
    }
    remote_calls: list[str] = []

    def _download_remote_media(*, asset_type: str, file_name: str | None, hint: dict[str, object]) -> str | None:
        remote_calls.append(str(hint.get("url") or ""))
        return str(remote_target)

    downloader._download_remote_media = _download_remote_media  # type: ignore[method-assign]

    try:
        resolved = downloader.resolve_for_export(request)
        assert resolved == (remote_target.resolve(), "napcat_public_token_get_image_remote_url")
        assert client.get_image_calls == 0
        assert remote_calls == ["https://gchat.qpic.cn/gchatpic_new/0/0-0-directhttps/0"]
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_resolve_for_export_prefers_existing_source_path_for_non_image_assets() -> None:
    temp_root = _workspace_temp_dir()
    downloader = NapCatMediaDownloader(_DummyClient())
    try:
        for asset_type, suffix in (
            ("video", "mp4"),
            ("file", "bin"),
            ("speech", "mp3"),
        ):
            source_path = temp_root / asset_type / f"existing-source.{suffix}"
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(f"{asset_type}-bytes".encode("utf-8"))

            resolved, resolver = downloader.resolve_for_export(
                {
                    "asset_type": asset_type,
                    "file_name": source_path.name,
                    "source_path": str(source_path),
                    "download_hint": {},
                }
            )

            assert resolved == source_path.resolve()
            assert resolver == "source_local_path"
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_stale_image_neighbor_does_not_accept_zero_byte_source_self() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "nt_qq" / "nt_data" / "Pic" / "2025-09" / "Ori" / "ZERO_SELF.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"")
    downloader = NapCatMediaDownloader(_DummyClient())
    try:
        resolved = downloader._resolve_from_stale_local_neighbors(
            {
                "asset_type": "image",
                "file_name": source_path.name,
                "source_path": str(source_path),
            }
        )
        assert resolved == (None, None)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_stale_image_neighbor_lookup_is_cached_per_source_path() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "nt_qq" / "nt_data" / "Pic" / "2025-09" / "Ori" / "SHARED.png"
    sibling_path = temp_root / "nt_qq" / "nt_data" / "Pic" / "2025-09" / "Thumb" / "SHARED_0.jpg"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    sibling_path.parent.mkdir(parents=True, exist_ok=True)
    sibling_path.write_bytes(b"thumb-bytes")
    downloader = _NeighborProbeDownloader()
    request = {
        "asset_type": "image",
        "file_name": source_path.name,
        "source_path": str(source_path),
    }
    try:
        first = downloader._resolve_from_stale_local_neighbors(request)
        first_probe_count = downloader.base_dir_index_builds
        second = downloader._resolve_from_stale_local_neighbors(request)

        assert first[0] == sibling_path.resolve()
        assert second[0] == sibling_path.resolve()
        assert first_probe_count > 0
        assert downloader.base_dir_index_builds == first_probe_count
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_source_local_path_lookup_is_cached_per_source_path() -> None:
    temp_root = _workspace_temp_dir()
    source_path = temp_root / "source" / "cached-source.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(b"image-bytes")
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "image",
        "file_name": source_path.name,
        "source_path": str(source_path),
    }
    try:
        first = downloader._resolve_from_source_local_path(request)
        cached_key = str(source_path).casefold()
        assert first == (source_path.resolve(), "source_local_path")
        assert downloader._source_local_resolution_cache[cached_key] == source_path.resolve()

        source_path.unlink()
        second = downloader._resolve_from_source_local_path(request)
        assert second == (source_path.resolve(), "source_local_path")
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def test_prepare_for_export_uses_metadata_only_batch_prefetch_with_timeout() -> None:
    fast_client = _BatchFastClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    request = _build_context_hint_request("sample-image.png")

    downloader.prepare_for_export([request])

    assert fast_client.calls
    assert fast_client.timeouts == [downloader.PREFETCH_BATCH_TIMEOUT_S]
    first_item = fast_client.calls[0][0]
    assert first_item["metadata_only"] is True


def test_prepare_for_export_emits_prepare_progress_events() -> None:
    fast_client = _BatchFastClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    requests = [
        _build_context_hint_request("sample-image-1.png"),
        _build_context_hint_request("sample-image-2.png"),
    ]
    progress_events: list[dict[str, object]] = []

    downloader.prepare_for_export(requests, progress_callback=progress_events.append)

    prepare_events = [
        event
        for event in progress_events
        if str(event.get("phase") or "") == "prefetch_media_prepare"
    ]
    assert prepare_events
    assert str(prepare_events[0].get("stage") or "") == "start"
    assert str(prepare_events[-1].get("stage") or "") == "done"
    assert int(prepare_events[-1].get("scanned_request_count") or 0) == 2
    assert int(prepare_events[-1].get("context_request_count") or 0) == 2
    assert "elapsed_s" in prepare_events[-1]


def test_prepare_for_export_stops_after_prefetch_budget_exceeded() -> None:
    fast_client = _BatchFastClient(raise_timeout=True)
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader.PREFETCH_BATCH_TIMEOUT_STRIKE_LIMIT = 99
    downloader.PREFETCH_TOTAL_BUDGET_S = 0.0
    request = _build_context_hint_request("sample-image.png")

    try:
        downloader.prepare_for_export([request])
    except RuntimeError as exc:
        assert "exceeding total budget" in str(exc)
    else:
        raise AssertionError("expected prefetch budget guard to stop the batch prefetch")


def test_prepare_for_export_skips_old_bucket_requests_in_metadata_batch_prefetch() -> None:
    fast_client = _BatchFastClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader.PREFETCH_LARGE_REQUEST_THRESHOLD = 1
    request = {
        "asset_type": "image",
        "file_name": "old-prefetch-skip.jpg",
        "timestamp_ms": 1750000000000,
        "download_hint": {
            "message_id_raw": "7610000000000000301",
            "element_id": "7610000000000000300",
            "peer_uid": "u_example",
            "chat_type_raw": "2",
        },
    }

    downloader.prepare_for_export([request])

    assert fast_client.calls == []


def test_prepare_for_export_uses_smaller_batches_and_explicit_timeout_for_large_runs() -> None:
    fast_client = _BatchFastClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader.PREFETCH_LARGE_REQUEST_THRESHOLD = 50
    downloader.PREFETCH_LARGE_BATCH_SIZE = 10
    downloader.PREFETCH_BATCH_TIMEOUT_S = 12.5
    requests = []
    for index in range(0, 51):
        request = _build_context_hint_request(f"context-{index}.jpg")
        request["timestamp_ms"] = 1770000000000
        requests.append(request)

    downloader.prepare_for_export(requests)

    assert [len(batch) for batch in fast_client.calls] == [10, 10, 10, 10, 10, 1]
    assert fast_client.timeouts == [12.5, 12.5, 12.5, 12.5, 12.5, 12.5]


def test_prepare_for_export_degrades_after_repeated_batch_timeouts() -> None:
    fast_client = _BatchFastClient(raise_timeout=True)
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader.PREFETCH_BATCH_SIZE = 5
    downloader.PREFETCH_BATCH_TIMEOUT_STRIKE_LIMIT = 2
    requests = []
    for index in range(0, 15):
        request = _build_context_hint_request(f"timeout-{index}.jpg")
        request["timestamp_ms"] = 1770000000000
        requests.append(request)
    progress_events: list[dict[str, object]] = []

    try:
        downloader.prepare_for_export(
            requests,
            progress_callback=progress_events.append,
        )
    except RuntimeError as exc:
        assert "repeated batch hydrate timeouts" in str(exc)
    else:
        raise AssertionError("prepare_for_export should degrade after repeated batch timeouts")

    assert len(fast_client.calls) == 2
    error_events = [
        event for event in progress_events
        if str(event.get("phase") or "") == "prefetch_media_chunk"
        and str(event.get("stage") or "") == "error"
    ]
    assert len(error_events) == 2
    assert all(str(event.get("reason") or "") == "chunk_timeout" for event in error_events)


def test_classify_missing_from_public_payload_marks_old_file_without_path_or_url_as_background() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())

    classification = downloader._classify_missing_from_public_payload(
        {
            "asset_type": "file",
            "public_action": "get_file",
            "file": "",
            "url": "",
            "file_name": "old-uploaded.jpg",
        },
        old_bucket=("file", "2025-09"),
        request={
            "asset_type": "file",
            "file_name": "old-uploaded.jpg",
        },
    )

    assert classification == "qq_expired_after_napcat"


def test_resolve_via_direct_file_id_keeps_top_level_old_file_not_found_unclassified_without_terminal_evidence() -> None:
    downloader = NapCatMediaDownloader(_MissingDirectFileClient())
    request = {
        "asset_type": "file",
        "file_name": "old-uploaded.jpg",
        "timestamp_ms": 1757268507000,
        "download_hint": {
            "file_id": "/494603f2-038f-4fd0-bffa-934b4553f019",
        },
    }

    resolved = downloader._resolve_via_direct_file_id(request)

    assert resolved is None


def test_resolve_via_direct_file_id_classifies_top_level_stale_local_video_not_found_as_background() -> None:
    downloader = NapCatMediaDownloader(_MissingDirectFileClient())
    request = {
        "asset_type": "video",
        "file_name": "old-stale-video.mp4",
        "timestamp_ms": 1757268507000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Video\2025-09\Ori\old-stale-video.mp4",
        "download_hint": {
            "file_id": "/494603f2-038f-4fd0-bffa-934b4553f019",
        },
    }

    resolved = downloader._resolve_via_direct_file_id(request)

    assert resolved == (None, "qq_expired_after_napcat")


def test_resolve_for_export_classifies_top_level_file_not_found_after_context_returns_no_local_path() -> None:
    fast_client = _StaticContextPayloadClient(
        {
            "asset_type": "file",
            "file_name": "old-uploaded.jpg",
            "file_id": "/494603f2-038f-4fd0-bffa-934b4553f019",
            "file_size": "12345",
            "file": "",
            "url": "",
        }
    )
    downloader = NapCatMediaDownloader(_MissingDirectFileClient(), fast_client=fast_client)
    request = {
        "asset_type": "file",
        "file_name": "old-uploaded.jpg",
        "timestamp_ms": 1757268507000,
        "download_hint": {
            "file_id": "/494603f2-038f-4fd0-bffa-934b4553f019",
            "message_id_raw": "7565810521225835815",
            "element_id": "7565810521225835816",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")
    assert fast_client.media_calls


def test_resolve_for_export_prefers_direct_hint_public_token_before_context_for_top_level_old_video() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "video",
        "file_name": "old-video.mp4",
        "timestamp_ms": 1757142395000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Video\2025-09\Ori\old-video.mp4",
        "download_hint": {
            "file_id": "NTV2COMPAT.direct-token",
            "message_id_raw": "7565810521148991491",
            "element_id": "7565810521148991492",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    def _direct_public_only(payload, **kwargs):
        assert payload["public_file_token"] == "NTV2COMPAT.direct-token"
        return Path(__file__).resolve(), "napcat_public_token_get_file"

    downloader._resolve_from_public_token = _direct_public_only  # type: ignore[method-assign]
    downloader._resolve_via_context_only = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context route should not run first"))
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (Path(__file__).resolve(), "napcat_public_token_get_file")


def test_resolve_for_export_prefers_direct_hint_public_token_before_context_for_top_level_old_image() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "image",
        "file_name": "old-image.png",
        "timestamp_ms": 1757142395000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\old-image.png",
        "download_hint": {
            "file_id": "1528803331",
            "message_id_raw": "7565810521148991491",
            "element_id": "7565810521148991492",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    def _direct_public_only(payload, **kwargs):
        assert payload["public_action"] == "get_image"
        assert payload["public_file_token"] == "1528803331"
        return Path(__file__).resolve(), "napcat_public_token_get_image"

    downloader._resolve_from_public_token = _direct_public_only  # type: ignore[method-assign]
    downloader._resolve_via_context_only = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context route should not run first"))
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (Path(__file__).resolve(), "napcat_public_token_get_image")


def test_resolve_for_export_allows_direct_hint_public_token_to_classify_terminal_old_video() -> None:
    downloader = NapCatMediaDownloader(_BlankPublicFileClient())
    request = {
        "asset_type": "video",
        "file_name": "old-video.mp4",
        "timestamp_ms": 1757142395000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Video\2025-09\Ori\missing-old-video.mp4",
        "download_hint": {
            "file_id": "NTV2COMPAT.old-video-token",
            "message_id_raw": "7565810521148991491",
            "element_id": "7565810521148991492",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    downloader._resolve_via_context_only = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context route should not run after terminal token proof"))
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")


def test_resolve_for_export_skips_context_for_terminal_top_level_image_request_state() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    zero_byte_root = _workspace_temp_dir()
    try:
        zero_byte_path = zero_byte_root / "Pic" / "2025-09" / "Ori" / "placeholder-image.png"
        zero_byte_path.parent.mkdir(parents=True, exist_ok=True)
        zero_byte_path.write_bytes(b"")
        request = {
            "asset_type": "image",
            "file_name": "placeholder-image.png",
            "timestamp_ms": 1757142395000,
            "source_path": str(zero_byte_path),
            "download_hint": {
                "file_id": "1528803331",
                "url": "/gchatpic_new/0/0-0-placeholder/0?term=2",
                "message_id_raw": "7565810521148991491",
                "element_id": "7565810521148991492",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
        downloader._public_token_action_outcomes[("get_image", "1528803331")] = None
        downloader._resolve_via_context_only = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context route should not run"))
        )

        resolved = downloader.resolve_for_export(request)

        assert resolved == (None, "qq_not_downloaded_local_placeholder")
    finally:
        shutil.rmtree(zero_byte_root, ignore_errors=True)


def test_resolve_for_export_classifies_top_level_weak_gchatpic_context_no_path_as_placeholder() -> None:
    fast_client = _StaticContextPayloadClient(
        {
            "asset_type": "image",
            "file_name": "{D8D6CB72-84C1-E1F5-D9A0-FB443715E86F}.jpg",
            "file": "",
            "url": "",
        }
    )
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader._resolve_via_direct_public_token_hint = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    request = {
        "asset_type": "image",
        "file_name": "{D8D6CB72-84C1-E1F5-D9A0-FB443715E86F}.jpg",
        "timestamp_ms": 1767618049000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2026-01\Ori\d8d6cb7284c1e1f5d9a0fb443715e86f.jpg",
        "download_hint": {
            "file_id": "2650833282",
            "url": "/gchatpic_new/3348513412/922065597-2650833282-D8D6CB7284C1E1F5D9A0FB443715E86F/0?term=255&is_origin=0",
            "message_id_raw": "7616401486576431236",
            "element_id": "7616401486576431235",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_not_downloaded_local_placeholder")


def test_resolve_for_export_classifies_top_level_weak_gchatpic_context_stale_local_as_placeholder() -> None:
    fast_client = _StaticContextPayloadClient(
        {
            "asset_type": "image",
            "file_name": "{D8D6CB72-84C1-E1F5-D9A0-FB443715E86F}.jpg",
            "file": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2026-01\Ori\d8d6cb7284c1e1f5d9a0fb443715e86f.jpg",
            "url": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2026-01\Ori\d8d6cb7284c1e1f5d9a0fb443715e86f.jpg",
        }
    )
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader._resolve_via_direct_public_token_hint = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    request = {
        "asset_type": "image",
        "file_name": "{D8D6CB72-84C1-E1F5-D9A0-FB443715E86F}.jpg",
        "timestamp_ms": 1767618049000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2026-01\Ori\d8d6cb7284c1e1f5d9a0fb443715e86f.jpg",
        "download_hint": {
            "file_id": "2650833282",
            "url": "/gchatpic_new/3348513412/922065597-2650833282-D8D6CB7284C1E1F5D9A0FB443715E86F/0?term=255&is_origin=0",
            "message_id_raw": "7616401486576431236",
            "element_id": "7616401486576431235",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_not_downloaded_local_placeholder")


def test_resolve_for_export_classifies_top_level_local_download_dead_signal_as_background() -> None:
    fast_client = _StaticContextPayloadClient(
        {
            "asset_type": "image",
            "file_name": "1B7D1B138F6F7E2AFD72F69E09DDF9C2.jpg",
            "file": "",
            "url": "",
        }
    )
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader._resolve_via_direct_public_token_hint = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    downloader._classify_image_local_placeholder_missing = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    request = {
        "asset_type": "image",
        "file_name": "1B7D1B138F6F7E2AFD72F69E09DDF9C2.jpg",
        "timestamp_ms": 1761380183000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2025-10\Ori\1b7d1b138f6f7e2afd72f69e09ddf9c2.jpg",
        "download_hint": {
            "file_id": "EhS-OrZMmHkosHhfOvHDFCJt3hKtAxiUuA8g_woo9t75wPS-kAMyBHByb2RQgL2jAVoQl-pjgWoxry5GRMVA6kCK9noC-H2CAQJuag",
            "url": "/download?appid=1407&fileid=EhS-OrZMmHkosHhfOvHDFCJt3hKtAxiUuA8g_woo9t75wPS-kAMyBHByb2RQgL2jAVoQl-pjgWoxry5GRMVA6kCK9noC-H2CAQJuag&spec=0",
            "message_id_raw": "7565810460010638737",
            "element_id": "7565810460010638736",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }
    downloader._public_token_action_outcomes[(
        "get_image",
        "EhS-OrZMmHkosHhfOvHDFCJt3hKtAxiUuA8g_woo9t75wPS-kAMyBHByb2RQgL2jAVoQl-pjgWoxry5GRMVA6kCK9noC-H2CAQJuag",
    )] = None

    resolved = downloader.resolve_for_export(request)

    assert resolved == (None, "qq_expired_after_napcat")


def test_resolve_via_direct_file_id_classifies_blank_top_level_payload_as_background_without_stale_local_path() -> None:
    downloader = NapCatMediaDownloader(_BlankDirectFilePayloadClient())
    request = {
        "asset_type": "video",
        "file_name": "blank-direct-file.mp4",
        "timestamp_ms": 1757268507000,
        "download_hint": {
            "file_id": "/fileid/blank-direct-file",
        },
    }

    resolved = downloader._resolve_via_direct_file_id(request)

    assert resolved == (None, "qq_expired_after_napcat")


def test_classify_blank_public_get_file_missing_marks_top_level_video_without_old_bucket_as_background() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())

    classification = downloader._classify_blank_public_get_file_missing(
        {
            "asset_type": "video",
            "public_action": "get_file",
            "public_file_token": "top-level-video-token",
            "file_name": "top-level-video.mp4",
            "file": "",
            "url": "",
        },
        old_bucket=None,
        request={
            "asset_type": "video",
            "file_name": "top-level-video.mp4",
            "download_hint": {
                "file_id": "top-level-video-token",
            },
        },
    )

    assert classification == "qq_expired_after_napcat"


def test_should_attempt_second_pass_public_retry_skips_terminal_top_level_image_request_state() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    zero_byte_root = _workspace_temp_dir()
    try:
        zero_byte_path = zero_byte_root / "Pic" / "2025-09" / "Ori" / "terminal-image.png"
        zero_byte_path.parent.mkdir(parents=True, exist_ok=True)
        zero_byte_path.write_bytes(b"")
        request = {
            "asset_type": "image",
            "file_name": "terminal-image.png",
            "timestamp_ms": 1757142395000,
            "source_path": str(zero_byte_path),
            "download_hint": {
                "file_id": "1528803999",
                "url": "/gchatpic_new/0/0-0-terminal/0?term=2",
            },
        }
        downloader._public_token_action_outcomes[("get_image", "1528803999")] = None

        assert downloader.should_attempt_second_pass_public_retry(request) is False
    finally:
        shutil.rmtree(zero_byte_root, ignore_errors=True)


def test_prepare_for_export_prefetches_terminal_top_level_image_without_scheduling_remote_or_token() -> None:
    class _UnusedFastClient:
        def hydrate_media_batch(self, *args, **kwargs):
            raise AssertionError("batch hydrate should not run for terminal image request")

    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=_UnusedFastClient())
    zero_byte_root = _workspace_temp_dir()
    try:
        zero_byte_path = zero_byte_root / "Pic" / "2025-09" / "Ori" / "prefetch-terminal-image.png"
        zero_byte_path.parent.mkdir(parents=True, exist_ok=True)
        zero_byte_path.write_bytes(b"")
        request = {
            "asset_type": "image",
            "file_name": "prefetch-terminal-image.png",
            "timestamp_ms": 1757142395000,
            "source_path": str(zero_byte_path),
            "download_hint": {
                "file_id": "1528804777",
                "url": "/gchatpic_new/0/0-0-terminal/0?term=2",
                "message_id_raw": "7565810521148991491",
                "element_id": "7565810521148991492",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
        downloader._public_token_action_outcomes[("get_image", "1528804777")] = None
        downloader._schedule_request_remote_prefetch = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("remote prefetch should be skipped"))
        )
        downloader._schedule_request_direct_public_token_prefetch = (  # type: ignore[method-assign]
            lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("token prefetch should be skipped"))
        )

        downloader.prepare_for_export([request], progress_callback=None)

        key = downloader._request_key(request)
        assert downloader._prefetched_media[key] == (None, "qq_not_downloaded_local_placeholder")
        assert downloader.resolve_for_export(request) == (None, "qq_not_downloaded_local_placeholder")
    finally:
        shutil.rmtree(zero_byte_root, ignore_errors=True)


def test_forward_payload_candidate_classifies_image_before_remote_or_public() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_request("forward-terminal.png")
    payload = {
        "asset_type": "image",
        "file_name": "forward-terminal.png",
    }
    downloader._classify_forward_missing = lambda *_args, **_kwargs: "qq_expired_after_napcat"  # type: ignore[method-assign]
    downloader._resolve_from_forward_remote_url = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("forward remote route should not run"))
    )
    downloader._resolve_from_public_token = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("public token route should not run"))
    )

    resolved = downloader._resolve_from_forward_payload_candidate(request, payload=payload)

    assert resolved == (None, "qq_expired_after_napcat")


def test_schedule_request_direct_public_token_prefetch_enqueues_top_level_video_token() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "video",
        "file_name": "old-video.mp4",
        "download_hint": {
            "file_id": "NTV2COMPAT.prefetch-token",
        },
    }
    scheduled: list[dict[str, object]] = []

    downloader._schedule_public_token_prefetch = (  # type: ignore[method-assign]
        lambda **kwargs: scheduled.append(kwargs["payload"])
    )

    downloader._schedule_request_direct_public_token_prefetch(request)

    assert scheduled == [
        {
            "asset_type": "video",
            "public_action": "get_file",
            "public_file_token": "NTV2COMPAT.prefetch-token",
            "file_name": "old-video.mp4",
        }
    ]


def test_schedule_request_direct_public_token_prefetch_enqueues_forward_video_token() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_video_request("forward-video.mp4")
    request["download_hint"]["file_id"] = "NTV2COMPAT.forward-prefetch-token"  # type: ignore[index]
    scheduled: list[dict[str, object]] = []

    downloader._schedule_public_token_prefetch = (  # type: ignore[method-assign]
        lambda **kwargs: scheduled.append(kwargs["payload"])
    )

    downloader._schedule_request_direct_public_token_prefetch(request)

    assert scheduled == [
        {
            "asset_type": "video",
            "public_action": "get_file",
            "public_file_token": "NTV2COMPAT.forward-prefetch-token",
            "file_name": "forward-video.mp4",
        }
    ]


def test_schedule_request_direct_public_token_prefetch_enqueues_top_level_image_token() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "image",
        "file_name": "old-image.png",
        "download_hint": {
            "file_id": "1528803331",
        },
    }
    scheduled: list[dict[str, object]] = []

    downloader._schedule_public_token_prefetch = (  # type: ignore[method-assign]
        lambda **kwargs: scheduled.append(kwargs["payload"])
    )

    downloader._schedule_request_direct_public_token_prefetch(request)

    assert scheduled == [
        {
            "asset_type": "image",
            "public_action": "get_image",
            "public_file_token": "1528803331",
            "file_name": "old-image.png",
        }
    ]


def test_schedule_public_token_prefetch_does_not_skip_napcat_local_download_image_hint() -> None:
    client = _PublicImageClient()
    downloader = NapCatMediaDownloader(client)
    downloader._public_token_executor = _ImmediateExecutor()
    request = {
        "asset_type": "image",
        "file_name": "old-image.png",
        "download_hint": {
            "file_id": "1528803331",
            "url": "/download?appid=1407&fileid=1528803331&spec=0",
        },
    }
    payload = {
        "asset_type": "image",
        "public_action": "get_image",
        "public_file_token": "1528803331",
        "file_name": "old-image.png",
        "remote_url": "/download?appid=1407&fileid=1528803331&spec=0",
        "url": "/download?appid=1407&fileid=1528803331&spec=0",
    }

    downloader._schedule_public_token_prefetch(
        request=request,
        request_data=request,
        payload=payload,
    )

    cache_key = downloader._public_token_prefetch_key(
        request_data=request,
        action="get_image",
        token="1528803331",
    )
    cached, future = downloader._public_token_prefetch_state(cache_key)

    assert future is None
    assert isinstance(cached, dict)
    assert client.get_image_calls == 1


def test_schedule_public_token_prefetch_stores_completed_result_without_explicit_consume() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    downloader._public_token_executor = _ImmediateExecutor()
    downloader._call_public_action_with_token = (  # type: ignore[method-assign]
        lambda action, token, **kwargs: {
            "asset_type": "image",
            "public_action": "get_image",
            "public_file_token": token,
            "file": str(Path(__file__).resolve()),
            "file_name": "old-image.png",
        }
    )
    request = {
        "asset_type": "image",
        "file_name": "old-image.png",
        "download_hint": {
            "file_id": "1528803331",
            "message_id_raw": "7565810521148991491",
            "element_id": "7565810521148991492",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    downloader._schedule_request_direct_public_token_prefetch(request)

    cache_key = downloader._public_token_prefetch_key(
        request_data=request,
        action="get_image",
        token="1528803331",
    )
    cached, future = downloader._public_token_prefetch_state(cache_key)

    assert future is None
    assert isinstance(cached, dict)
    assert str(cached.get("resolved_path") or "").strip() == str(Path(__file__).resolve())
    assert str(cached.get("resolver") or "").strip() == "napcat_public_token_get_image_prefetched"


def test_should_skip_eager_remote_prefetch_for_placeholder_image_with_direct_public_token() -> None:
    workspace = _workspace_temp_dir()
    downloader = NapCatMediaDownloader(_DummyClient(), remote_base_url="http://127.0.0.1:3000")
    source_path = workspace / "Pic" / "2026-01" / "Ori" / "dead-image.png"
    thumb_dir = source_path.parent.parent / "Thumb"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumb_dir / "dead-image_0.png"
    thumb_path.write_bytes(b"")
    request = {
        "asset_type": "image",
        "file_name": "dead-image.png",
        "source_path": str(source_path),
        "download_hint": {
            "file_id": "1528803331",
            "url": "/download?appid=1407&fileid=1528803331&spec=0",
        },
    }

    try:
        assert downloader._should_skip_eager_remote_prefetch(request, old_bucket=None) is True
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_should_prefer_direct_public_token_prefetch_for_placeholder_image_with_weak_relative_gchatpic_hint() -> None:
    workspace = _workspace_temp_dir()
    downloader = NapCatMediaDownloader(_DummyClient(), remote_base_url="http://127.0.0.1:3000")
    source_path = workspace / "Pic" / "2026-01" / "Ori" / "dead-image.png"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    thumb_dir = source_path.parent.parent / "Thumb"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    (thumb_dir / "dead-image_0.png").write_bytes(b"")
    request = {
        "asset_type": "image",
        "file_name": "dead-image.png",
        "source_path": str(source_path),
        "download_hint": {
            "file_id": "1528803331",
            "url": "/gchatpic_new/3348513412/922065597-2397162384-A8D7F0A6BDE1314277980B64829EE245/0?term=255&is_origin=0",
        },
    }

    try:
        assert downloader._should_prefer_direct_public_token_prefetch_for_placeholder_image(request) is True
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def test_prepare_prefetched_top_level_image_token_is_reused_by_resolve_without_second_public_call() -> None:
    client = _PublicImageClient()
    downloader = NapCatMediaDownloader(client, fast_client=_BatchFastClient())
    downloader._public_token_executor = _ImmediateExecutor()
    downloader._schedule_request_remote_prefetch = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    downloader._resolve_via_context_only = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("context route should not run"))  # type: ignore[method-assign]
    downloader.PREFETCH_LARGE_REQUEST_THRESHOLD = 1
    request = {
        "asset_type": "image",
        "file_name": "old-image.png",
        "timestamp_ms": 1757142395000,
        "download_hint": {
            "file_id": "1528803331",
            "message_id_raw": "7565810521148991491",
            "element_id": "7565810521148991492",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    downloader.prepare_for_export([request])
    cache_key = downloader._public_token_prefetch_key(
        request_data=request,
        action="get_image",
        token="1528803331",
    )
    cached, future = downloader._public_token_prefetch_state(cache_key)
    assert future is None
    assert isinstance(cached, dict)
    assert str(cached.get("resolved_path") or "").strip() == str(Path(__file__).resolve())

    downloader._call_public_action_with_token = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("resolve should reuse prefetched token result"))
    )

    resolved = downloader.resolve_for_export(request)

    assert resolved == (Path(__file__).resolve(), "napcat_public_token_get_image_prefetched")
    assert client.get_image_calls == 1


def test_schedule_request_direct_public_token_prefetch_skips_direct_file_id_shape() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = {
        "asset_type": "file",
        "file_name": "old-uploaded.jpg",
        "download_hint": {
            "file_id": "/494603f2-038f-4fd0-bffa-934b4553f019",
        },
    }
    scheduled: list[dict[str, object]] = []

    downloader._schedule_public_token_prefetch = (  # type: ignore[method-assign]
        lambda **kwargs: scheduled.append(kwargs["payload"])
    )

    downloader._schedule_request_direct_public_token_prefetch(request)

    assert scheduled == []


def test_direct_public_token_payload_for_forward_video_request_is_preserved() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _build_forward_video_request("forward-token-video.mp4")
    request["download_hint"]["file_id"] = "NTV2COMPAT.forward-token-video"  # type: ignore[index]

    payload = downloader._direct_public_token_payload_for_request(request)

    assert payload == {
        "asset_type": "video",
        "public_action": "get_file",
        "public_file_token": "NTV2COMPAT.forward-token-video",
        "file_name": "forward-token-video.mp4",
    }


def test_prepare_for_export_large_run_still_prefetches_strong_old_image_hints_before_batch_skip() -> None:
    fast_client = _BatchFastClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)
    downloader.PREFETCH_LARGE_REQUEST_THRESHOLD = 1
    request = _mark_request_old(_build_context_hint_request("old-image.png"), days=240)
    request["download_hint"]["file_id"] = "1528803331"
    request["download_hint"]["url"] = (
        "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=1528803331&spec=0"
    )
    remote_scheduled: list[str] = []
    token_scheduled: list[dict[str, object]] = []

    downloader._schedule_request_remote_prefetch = (  # type: ignore[method-assign]
        lambda scheduled_request: remote_scheduled.append(str(scheduled_request.get("file_name") or ""))
    )
    downloader._schedule_public_token_prefetch = (  # type: ignore[method-assign]
        lambda **kwargs: token_scheduled.append(kwargs["payload"])
    )

    downloader.prepare_for_export([request])

    assert fast_client.calls == []
    assert remote_scheduled == ["old-image.png"]
    assert token_scheduled == [
        {
            "asset_type": "image",
            "public_action": "get_image",
            "public_file_token": "1528803331",
            "file_name": "old-image.png",
            "remote_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=1528803331&spec=0",
            "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=1528803331&spec=0",
        }
    ]


def test_resolve_from_public_token_marks_old_video_blank_payload_as_background() -> None:
    downloader = NapCatMediaDownloader(_BlankPublicFileClient())
    request = {
        "asset_type": "video",
        "file_name": "old-video.mp4",
        "timestamp_ms": 1757142395000,
        "download_hint": {},
    }

    resolved = downloader._resolve_from_public_token(
        {
            "asset_type": "video",
            "public_action": "get_file",
            "public_file_token": "old-video-token",
            "file_name": "old-video.mp4",
        },
        old_bucket=("video", "2025-09"),
        request=request,
    )

    assert resolved == (None, "qq_expired_after_napcat")


def test_classify_missing_from_public_payload_marks_old_video_with_stale_local_url_as_background() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())

    classification = downloader._classify_missing_from_public_payload(
        {
            "asset_type": "video",
            "public_action": "get_file",
            "public_file_token": "old-video-token",
            "file": "",
            "url": r"C:\QQ\3956020260\nt_qq\nt_data\Video\2025-09\Ori\missing-old-video.mp4",
            "file_name": "missing-old-video.mp4",
            "file_id": "old-file-id",
        },
        old_bucket=("video", "2025-09"),
        request={
            "asset_type": "video",
            "file_name": "missing-old-video.mp4",
        },
    )

    assert classification == "qq_expired_after_napcat"


def test_consume_remote_media_prefetch_peek_does_not_block_on_inflight_future() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    cache_key = ("image", "https://example.invalid/test.png")
    downloader._remote_media_resolution_futures[cache_key] = Future()

    started = time.perf_counter()
    resolved = downloader._consume_remote_media_prefetch(cache_key)
    elapsed = time.perf_counter() - started

    assert resolved is ...
    assert elapsed < 0.5


def test_cleanup_remote_cache_rebuilds_prefetch_runtime_without_waiting() -> None:
    downloader = _CleanupProbeDownloader()

    stats = downloader.cleanup_remote_cache()

    assert downloader.rebuild_calls == [(False, True)]
    assert stats["cache_cleared"] is False


def test_forward_timeout_updates_download_progress_counters() -> None:
    fast_client = _TimeoutForwardClient()
    downloader = NapCatMediaDownloader(_DummyClient(), fast_client=fast_client)

    downloader._download_via_forward_context(
        _build_forward_request("2C167901425EF469C0B1F0BF859E4B2C.jpg"),
        materialize=False,
    )

    snapshot = downloader.export_download_progress_snapshot()
    assert snapshot["timeout_count"] == 1
    assert snapshot["forward_context_timeout_count"] == 1


def test_reset_export_state_generation_rejects_stale_prefetch_store_writes() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    stale_generation = downloader._transient_state_generation

    downloader.reset_export_state()

    current_generation = downloader._transient_state_generation
    assert current_generation == stale_generation + 1

    remote_key = ("image", "https://assets.example.invalid/test.png")
    public_key = ("speech", "forward_media", "get_record", "token-a", "scope-a")
    downloader._store_remote_prefetch_result(remote_key, "stale", generation=stale_generation)
    downloader._store_public_token_prefetch_result(
        public_key,
        {"payload": {"url": "https://assets.example.invalid/test.mp3"}},
        generation=stale_generation,
    )

    assert remote_key not in downloader._remote_media_resolution_cache
    assert public_key not in downloader._public_token_prefetch_cache

    downloader._store_remote_prefetch_result(remote_key, "fresh", generation=current_generation)
    downloader._store_public_token_prefetch_result(
        public_key,
        {"payload": {"url": "https://assets.example.invalid/test.mp3"}},
        generation=current_generation,
    )

    assert downloader._remote_media_resolution_cache[remote_key] == "fresh"
    assert public_key in downloader._public_token_prefetch_cache


def test_schedule_public_token_prefetch_drops_future_submitted_before_reset_boundary() -> None:
    downloader = _ResetDuringTokenPrefetchDownloader(_DummyClient())
    request = {
        "asset_type": "speech",
        "asset_role": "forward_media",
        "file_name": "speech-a.mp3",
        "download_hint": {
            "_forward_parent": {
                "message_id_raw": "parent-a",
                "element_id": "element:parent-a",
                "peer_uid": "u-reset",
                "chat_type_raw": "2",
            }
        },
    }
    payload = {
        "public_action": "get_record",
        "public_file_token": "token-a",
        "file_name": "speech-a.mp3",
    }

    downloader._schedule_public_token_prefetch(
        request=request,
        request_data=request,
        payload=payload,
    )

    assert downloader._public_token_prefetch_futures == {}
    assert downloader._public_token_prefetch_cache == {}


def test_ensure_remote_media_future_drops_future_submitted_before_reset_boundary() -> None:
    downloader = _ResetDuringRemoteSubmitDownloader(_DummyClient())

    future, created = downloader._ensure_remote_media_future(
        asset_type="image",
        file_name="image-a.jpg",
        resolved_remote_url="https://assets.example.invalid/image-a.jpg",
    )

    assert future is None
    assert created is False
    assert downloader._remote_media_resolution_futures == {}
    assert downloader._remote_media_resolution_cache == {}


def test_second_pass_public_retry_only_runs_when_prefetch_state_is_pending() -> None:
    downloader = NapCatMediaDownloader(_DummyClient())
    request = _second_pass_request()

    assert downloader.should_attempt_second_pass_public_retry(request) is False

    direct_payload = downloader._direct_public_token_payload_for_request(request)
    assert direct_payload is not None
    cache_key = downloader._public_token_prefetch_key(
        request_data=request,
        action=str(direct_payload["public_action"]),
        token=str(direct_payload["public_file_token"]),
    )

    pending_future: Future[dict[str, object] | None] = Future()
    downloader._public_token_prefetch_futures[cache_key] = pending_future
    assert downloader.should_attempt_second_pass_public_retry(request) is True

    pending_future.set_result(None)
    assert downloader.should_attempt_second_pass_public_retry(request) is True

    downloader._store_public_token_prefetch_result(cache_key, None, future=pending_future)
    assert downloader.should_attempt_second_pass_public_retry(request) is False


def test_second_pass_public_retry_gate_remains_stable_across_prefetch_and_terminal_variants() -> None:
    def _cache_key_for(
        downloader: NapCatMediaDownloader,
        request: dict[str, object],
    ) -> tuple[str, str, str, str]:
        direct_payload = downloader._direct_public_token_payload_for_request(request)
        assert direct_payload is not None
        return downloader._public_token_prefetch_key(
            request_data=request,
            action=str(direct_payload["public_action"]),
            token=str(direct_payload["public_file_token"]),
        )

    downloader = NapCatMediaDownloader(_DummyClient())
    request = _second_pass_request()
    cache_key = _cache_key_for(downloader, request)

    assert downloader.should_attempt_second_pass_public_retry(request) is False

    pending_future: Future[dict[str, object] | None] = Future()
    downloader._public_token_prefetch_futures[cache_key] = pending_future
    assert downloader.should_attempt_second_pass_public_retry(request) is True

    pending_future.set_result(None)
    assert downloader.should_attempt_second_pass_public_retry(request) is True

    downloader._store_public_token_prefetch_result(
        cache_key,
        {
            "payload": {
                "public_action": "get_image",
                "public_file_token": "live-token",
            }
        },
        future=pending_future,
    )
    assert downloader.should_attempt_second_pass_public_retry(request) is False

    downloader = NapCatMediaDownloader(_DummyClient())
    request = _second_pass_request()
    cache_key = _cache_key_for(downloader, request)
    downloader._store_public_token_prefetch_result(
        cache_key,
        {
            "payload": {
                "public_action": "get_image",
                "public_file_token": "live-token",
            },
            "remote_attempted": True,
        },
    )
    assert downloader.should_attempt_second_pass_public_retry(request) is False

    downloader = NapCatMediaDownloader(_DummyClient())
    terminal_request = {
        "asset_type": "image",
        "file_name": "terminal-image.png",
        "timestamp_ms": 1757142395000,
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\terminal-image.png",
        "download_hint": {
            "file_id": "1528803999",
            "url": "/gchatpic_new/0/0-0-terminal/0?term=2",
        },
    }
    terminal_cache_key = _cache_key_for(downloader, terminal_request)
    downloader._store_public_token_prefetch_result(
        terminal_cache_key,
        {
            "payload": {
                "_known_missing_classification": "qq_not_downloaded_local_placeholder",
                "public_action": "get_image",
                "public_file_token": "1528803999",
            }
        },
    )
    assert downloader.should_attempt_second_pass_public_retry(terminal_request) is False

    downloader = NapCatMediaDownloader(_DummyClient())
    zero_byte_root = _workspace_temp_dir()
    try:
        zero_byte_path = zero_byte_root / "Pic" / "2025-09" / "Ori" / "terminal-image.png"
        zero_byte_path.parent.mkdir(parents=True, exist_ok=True)
        zero_byte_path.write_bytes(b"")
        terminal_request = {
            "asset_type": "image",
            "file_name": "terminal-image.png",
            "timestamp_ms": 1757142395000,
            "source_path": str(zero_byte_path),
            "download_hint": {
                "file_id": "1528803999",
                "url": "/gchatpic_new/0/0-0-terminal/0?term=2",
            },
        }
        downloader._public_token_action_outcomes[("get_image", "1528803999")] = None
        assert downloader.should_attempt_second_pass_public_retry(terminal_request) is False

        context_terminal_request = dict(terminal_request)
        context_terminal_request["_context_payload"] = {
            "public_action": "get_image",
            "public_file_token": "1528803999",
            "file": str(zero_byte_path),
        }
        assert downloader.should_attempt_second_pass_public_retry(context_terminal_request) is False
    finally:
        shutil.rmtree(zero_byte_root, ignore_errors=True)
