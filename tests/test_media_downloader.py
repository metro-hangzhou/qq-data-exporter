from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import uuid
from contextlib import contextmanager

import httpx

from qq_data_integrations.napcat.fast_history_client import (
    NapCatFastHistoryTimeoutError,
    NapCatFastHistoryUnavailable,
)
from qq_data_integrations.napcat.http_client import NapCatApiError, NapCatApiTimeoutError
from qq_data_integrations.napcat.media_downloader import NapCatMediaDownloader


class _FakeClient:
    def __init__(
        self,
        payload: dict[str, str] | None = None,
        *,
        fail: bool = False,
        timeout_for: set[str] | None = None,
    ) -> None:
        self.payload = payload or {}
        self.fail = fail
        self.timeout_for = set(timeout_for or set())
        self.calls: list[tuple[object, ...]] = []
        self.timeouts: list[tuple[str, float | None]] = []

    def get_image(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
        timeout: float | None = None,
    ):
        self.calls.append(("image", file_id, file))
        self.timeouts.append(("image", timeout))
        if "image" in self.timeout_for:
            raise NapCatApiTimeoutError("NapCat action timed out: get_image")
        if self.fail:
            raise NapCatApiError("boom")
        return self.payload

    def get_file(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
        timeout: float | None = None,
    ):
        self.calls.append(("file", file_id, file))
        self.timeouts.append(("file", timeout))
        if "file" in self.timeout_for:
            raise NapCatApiTimeoutError("NapCat action timed out: get_file")
        if self.fail:
            raise NapCatApiError("boom")
        return self.payload

    def get_record(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
        out_format: str | None = None,
        timeout: float | None = None,
    ):
        self.calls.append(("record", file_id, file, out_format))
        self.timeouts.append(("record", timeout))
        if "record" in self.timeout_for:
            raise NapCatApiTimeoutError("NapCat action timed out: get_record")
        if self.fail:
            raise NapCatApiError("boom")
        return self.payload


class _FakeFastClient:
    def __init__(
        self,
        payload: dict[str, str] | None = None,
        *,
        fail: bool = False,
        forward_payload: dict[str, object] | None = None,
        forward_payload_sequence: list[dict[str, object]] | None = None,
        forward_fail: bool = False,
        forward_exception: Exception | None = None,
    ) -> None:
        self.payload = payload or {}
        self.fail = fail
        self.forward_payload = forward_payload or {}
        self.forward_payload_sequence = list(forward_payload_sequence or [])
        self.forward_fail = forward_fail
        self.forward_exception = forward_exception
        self.calls: list[tuple[str, str, str, int, str | None, str | None]] = []
        self.forward_calls: list[tuple[str, str, str, int]] = []
        self.forward_payloads: list[dict[str, object]] = []
        self.timeouts: list[tuple[str, float | None]] = []

    def hydrate_media(
        self,
        *,
        message_id_raw: str,
        element_id: str,
        peer_uid: str,
        chat_type_raw: int | str,
        asset_type: str | None = None,
        asset_role: str | None = None,
        timeout: float | None = None,
    ):
        self.calls.append(
            (
                message_id_raw,
                element_id,
                peer_uid,
                int(chat_type_raw),
                asset_type,
                asset_role,
            )
        )
        self.timeouts.append(("hydrate_media", timeout))
        if self.fail:
            raise RuntimeError("boom")
        return self.payload

    def hydrate_forward_media(
        self,
        *,
        message_id_raw: str,
        element_id: str,
        peer_uid: str,
        chat_type_raw: int | str,
        asset_type: str | None = None,
        asset_role: str | None = None,
        file_name: str | None = None,
        md5: str | None = None,
        file_id: str | None = None,
        url: str | None = None,
        materialize: bool = False,
        download_timeout_ms: int | None = None,
        timeout: float | None = None,
    ):
        self.forward_payloads.append(
            {
                "message_id_raw": message_id_raw,
                "element_id": element_id,
                "peer_uid": peer_uid,
                "chat_type_raw": int(chat_type_raw),
                "asset_type": asset_type,
                "asset_role": asset_role,
                "file_name": file_name,
                "md5": md5,
                "file_id": file_id,
                "url": url,
                "materialize": materialize,
                "download_timeout_ms": download_timeout_ms,
                "timeout": timeout,
            }
        )
        self.forward_calls.append(
            (message_id_raw, element_id, peer_uid, int(chat_type_raw))
        )
        self.timeouts.append(("hydrate_forward_media", timeout))
        if self.forward_exception is not None:
            raise self.forward_exception
        if self.forward_fail:
            raise RuntimeError("boom")
        if self.forward_payload_sequence:
            return self.forward_payload_sequence.pop(0)
        return self.forward_payload


@contextmanager
def _repo_temp_dir(prefix: str):
    temp_dir = Path("state") / "test_tmp" / f"{prefix}_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_media_downloader_uses_get_image_for_image_assets() -> None:
    with _repo_temp_dir("media_downloader_image") as tmp_path:
        image_path = tmp_path / "image.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\npng")
        client = _FakeClient({"file": str(image_path)})
        downloader = NapCatMediaDownloader(client)  # type: ignore[arg-type]

        resolved = downloader.download_for_export(
            {
                "asset_type": "image",
                "file_name": "demo.png",
                "download_hint": {"file_id": "image-file-uuid"},
            }
        )

        assert resolved == image_path.resolve()
        assert client.calls == [("image", "image-file-uuid", "demo.png")]


def test_media_downloader_returns_none_when_get_image_fails() -> None:
    client = _FakeClient(fail=True)
    downloader = NapCatMediaDownloader(client)  # type: ignore[arg-type]

    resolved = downloader.download_for_export(
        {
            "asset_type": "image",
            "file_name": "demo.png",
            "download_hint": {"file_id": "image-file-uuid"},
        }
    )

    assert resolved is None


def test_media_downloader_prefers_fast_context_hydration_for_images() -> None:
    with _repo_temp_dir("media_downloader_fast_hydrate") as tmp_path:
        image_path = tmp_path / "hydrated.gif"
        image_path.write_bytes(b"GIF89afast")
        client = _FakeClient({"file": "should-not-be-used"})
        fast_client = _FakeFastClient({"file": str(image_path)})
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.download_for_export(
            {
                "asset_type": "image",
                "file_name": "demo.gif",
                "download_hint": {
                    "file_id": "raw-file-uuid",
                    "message_id_raw": "msg-1",
                    "element_id": "element-2",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == image_path.resolve()
        assert fast_client.calls == [
            ("msg-1", "element-2", "922065597", 2, "image", None)
        ]
        assert client.calls == []


def test_media_downloader_prefers_public_token_over_raw_fast_payload() -> None:
    with _repo_temp_dir("media_downloader_public_token") as tmp_path:
        image_path = tmp_path / "token-image.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\ntoken-image")
        client = _FakeClient({"file": str(image_path)})
        fast_client = _FakeFastClient(
            {
                "file": "C:/stale/path.png",
                "public_action": "get_image",
                "public_file_token": "token-123",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "demo.png",
                "download_hint": {
                    "message_id_raw": "msg-token",
                    "element_id": "element-token",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == (image_path.resolve(), "napcat_public_token_get_image")
        assert client.calls == [("image", None, "token-123")]


def test_media_downloader_disables_forward_context_after_unavailable_route() -> None:
    fast_client = _FakeFastClient(
        forward_exception=NapCatFastHistoryUnavailable("forward route unavailable")
    )
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient(),
        fast_client=fast_client,  # type: ignore[arg-type]
    )
    request = {
        "asset_type": "image",
        "file_name": "nested.png",
        "download_hint": {
            "_forward_parent": {
                "message_id_raw": "msg-parent",
                "element_id": "el-parent",
                "peer_uid": "peer-parent",
                "chat_type_raw": 2,
            }
        },
    }

    assert downloader.resolve_for_export(request) == (None, None)
    assert downloader.resolve_for_export(request) == (None, None)
    assert fast_client.forward_calls == [("msg-parent", "el-parent", "peer-parent", 2)]


def test_media_downloader_download_for_export_uses_public_token_before_fast_payload() -> None:
    with _repo_temp_dir("media_downloader_public_token_export") as tmp_path:
        image_path = tmp_path / "token-export.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\ntoken-export")
        client = _FakeClient({"file": str(image_path)})
        fast_client = _FakeFastClient(
            {
                "file": "C:/stale/path.png",
                "public_action": "get_image",
                "public_file_token": "token-export",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.download_for_export(
            {
                "asset_type": "image",
                "file_name": "demo.png",
                "download_hint": {
                    "message_id_raw": "msg-export",
                    "element_id": "element-export",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == image_path.resolve()
        assert client.calls == [("image", None, "token-export")]


def test_media_downloader_downloads_remote_url_from_public_token_payload() -> None:
    with _repo_temp_dir("media_downloader_public_token_remote") as tmp_path:
        image_payload = b"\x89PNG\r\n\x1a\npublic-token-remote"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc")
            return httpx.Response(200, content=image_payload)

        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=abc",
                "file_name": "demo.png",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": "token-remote-1",
                "file_name": "demo.png",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "demo.png",
                "download_hint": {
                    "message_id_raw": "msg-remote",
                    "element_id": "element-remote",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved[0] is not None
        assert resolved[0].name == "demo.png"
        assert resolved[0].read_bytes() == image_payload
        assert resolved[1] == "napcat_public_token_get_image_remote_url"
        assert client.calls == [("image", None, "token-remote-1")]


def test_media_downloader_resolve_for_export_uses_direct_file_id_for_file_assets() -> None:
    with _repo_temp_dir("media_downloader_direct_file_id_file") as tmp_path:
        exported_file = tmp_path / "exports2.zip"
        exported_file.write_bytes(b"PK\x03\x04zip")
        client = _FakeClient({"file": str(exported_file)})
        downloader = NapCatMediaDownloader(client)  # type: ignore[arg-type]

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "file",
                "file_name": "exports2.zip",
                "download_hint": {
                    "file_id": "/2e3b729d-c6ab-4236-93f9-660bb90bfe3c",
                },
            }
        )

        assert resolved == (exported_file.resolve(), "napcat_segment_file_id_get_file")
        assert client.calls == [("file", "/2e3b729d-c6ab-4236-93f9-660bb90bfe3c", None)]
        assert client.timeouts == [("file", 12.0)]


def test_media_downloader_resolve_for_export_downloads_remote_url_from_direct_file_id() -> None:
    with _repo_temp_dir("media_downloader_direct_file_id_remote") as tmp_path:
        file_payload = b"PK\x03\x04remote-zip"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=filezip")
            return httpx.Response(200, content=file_payload)

        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=filezip",
                "file_name": "exports2.zip",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "file",
                "file_name": "exports2.zip",
                "download_hint": {
                    "file_id": "/2e3b729d-c6ab-4236-93f9-660bb90bfe3c",
                },
            }
        )

        assert resolved[0] is not None
        assert resolved[0].name == "exports2.zip"
        assert resolved[0].read_bytes() == file_payload
        assert resolved[1] == "napcat_segment_file_id_get_file_remote_url"
        assert client.calls == [("file", "/2e3b729d-c6ab-4236-93f9-660bb90bfe3c", None)]


def test_media_downloader_prefers_forward_hydration_before_direct_file_id_for_forward_file() -> None:
    with _repo_temp_dir("media_downloader_forward_context_first") as tmp_path:
        exported_file = tmp_path / "forward-uploaded.bin"
        exported_file.write_bytes(b"forward-file")
        client = _FakeClient({"file": str(exported_file)})
        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "file",
                        "file_name": "uploaded_file",
                        "file_id": "/8311cda0-18b5-11f1-bb89-5254001607c6",
                        "public_action": "get_file",
                        "public_file_token": "forward-token-1",
                    }
                ]
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "file",
                "file_name": "uploaded_file",
                "download_hint": {
                    "file_id": "/8311cda0-18b5-11f1-bb89-5254001607c6",
                    "_forward_parent": {
                        "message_id_raw": "forward-parent-msg",
                        "element_id": "forward-parent-element",
                        "peer_uid": "751365230",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (exported_file.resolve(), "napcat_public_token_get_file")
        assert client.calls == [("file", None, "forward-token-1")]
        assert fast_client.forward_calls == [
            ("forward-parent-msg", "forward-parent-element", "751365230", 2)
        ]


def test_media_downloader_classifies_old_public_token_remote_miss_as_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=expired")
        return httpx.Response(404, text="expired")

    with _repo_temp_dir("media_downloader_expired_public_token") as tmp_path:
        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=expired",
                "file_name": "expired.png",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": "token-expired-1",
                "remote_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=expired",
                "file_name": "expired.png",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "expired.png",
                "timestamp_ms": 1704067200000,
                "download_hint": {
                    "message_id_raw": "msg-expired",
                    "element_id": "element-expired",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == (None, "qq_expired_after_napcat")
        assert client.calls == [("image", None, "token-expired-1")]


def test_media_downloader_classifies_old_public_action_remote_miss_as_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://gchat.qpic.cn/gchatpic_new/0/0-0-50A19C64412230A8F5CCAE45795D7BAC/0")
        return httpx.Response(404, text="expired")

    with _repo_temp_dir("media_downloader_expired_public_action_payload") as tmp_path:
        client = _FakeClient(
            {
                "file": "",
                "url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-50A19C64412230A8F5CCAE45795D7BAC/0",
                "file_name": "",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "file": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\50a19c64412230a8f5ccae45795d7bac",
                "url": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\50a19c64412230a8f5ccae45795d7bac",
                "public_action": "get_image",
                "public_file_token": "token-public-action-expired",
                "file_name": "50a19c64412230a8f5ccae45795d7bac",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "50a19c64412230a8f5ccae45795d7bac",
                "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\50a19c64412230a8f5ccae45795d7bac",
                "timestamp_ms": 1758615758000,
                "download_hint": {
                    "message_id_raw": "msg-expired-public-action",
                    "element_id": "element-expired-public-action",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == (None, "qq_expired_after_napcat")
        assert client.calls == [("image", None, "token-public-action-expired")]


def test_media_downloader_does_not_auto_classify_non_old_public_action_remote_miss_as_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=recent-expired")
        return httpx.Response(404, text="expired")

    with _repo_temp_dir("media_downloader_recent_expired_public_action") as tmp_path:
        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=recent-expired",
                "file_name": "recent-expired.png",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "file": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Ori\recent-expired.png",
                "url": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Ori\recent-expired.png",
                "public_action": "get_image",
                "public_file_token": "token-recent-expired",
                "file_name": "recent-expired.png",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )
        recent_timestamp_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "recent-expired.png",
                "timestamp_ms": recent_timestamp_ms,
                "download_hint": {
                    "message_id_raw": "msg-recent-expired",
                    "element_id": "element-recent-expired",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == (None, None)
        assert client.calls == [("image", None, "token-recent-expired")]


def test_media_downloader_caches_failed_public_token_attempts_within_forward_resolution() -> None:
    client = _FakeClient(timeout_for={"file"})
    fast_client = _FakeFastClient(
        forward_payload_sequence=[
            {
                "assets": [
                    {
                        "asset_type": "video",
                        "file_name": "demo.mp4",
                        "public_action": "get_file",
                        "public_file_token": "timeout-token-1",
                    }
                ]
            },
            {
                "assets": [
                    {
                        "asset_type": "video",
                        "file_name": "demo.mp4",
                        "public_action": "get_file",
                        "public_file_token": "timeout-token-1",
                    }
                ],
                "targeted_mode": "single_target_download",
            },
        ]
    )
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    resolved = downloader.resolve_for_export(
        {
            "asset_type": "video",
            "file_name": "demo.mp4",
            "download_hint": {
                "_forward_parent": {
                    "message_id_raw": "forward-parent-msg",
                    "element_id": "forward-parent-element",
                    "peer_uid": "751365230",
                    "chat_type_raw": 2,
                }
            },
        }
    )

    assert resolved == (None, None)
    assert client.calls == [("file", None, "timeout-token-1")]
    assert fast_client.forward_calls == [
        ("forward-parent-msg", "forward-parent-element", "751365230", 2),
        ("forward-parent-msg", "forward-parent-element", "751365230", 2),
    ]


def test_media_downloader_classifies_stale_non_old_public_action_remote_miss_as_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=stale-expired")
        return httpx.Response(404, text="expired")

    with _repo_temp_dir("media_downloader_stale_expired_public_action") as tmp_path:
        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=stale-expired",
                "file_name": "stale-expired.png",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "file": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Ori\stale-expired.png",
                "url": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Ori\stale-expired.png",
                "public_action": "get_image",
                "public_file_token": "token-stale-expired",
                "file_name": "stale-expired.png",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        stale_timestamp_ms = int(
            (datetime.now(timezone.utc) - timedelta(days=35)).timestamp() * 1000
        )
        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "stale-expired.png",
                "timestamp_ms": stale_timestamp_ms,
                "download_hint": {
                    "message_id_raw": "msg-stale-expired",
                    "element_id": "element-stale-expired",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved == (None, "qq_expired_after_napcat")
        assert client.calls == [("image", None, "token-stale-expired")]


def test_media_downloader_falls_back_to_stale_thumb_neighbor_for_image() -> None:
    with _repo_temp_dir("media_downloader_stale_thumb_neighbor") as tmp_path:
        pic_root = tmp_path / "QQ" / "3956020260" / "nt_qq" / "nt_data" / "Pic" / "2026-02"
        thumb_dir = pic_root / "Thumb"
        oritemp_dir = pic_root / "OriTemp"
        thumb_dir.mkdir(parents=True, exist_ok=True)
        oritemp_dir.mkdir(parents=True, exist_ok=True)
        zero_variant = thumb_dir / "3d056a0f987123794ba2fa2c84a1e742_0.jpg"
        big_variant = thumb_dir / "3d056a0f987123794ba2fa2c84a1e742_720.jpg"
        empty_oritemp = oritemp_dir / "3d056a0f987123794ba2fa2c84a1e742"
        zero_variant.write_bytes(b"\xff\xd8\xff\xe0small")
        big_variant.write_bytes(b"\xff\xd8\xff\xe0bigger-thumb")
        empty_oritemp.write_bytes(b"")

        downloader = NapCatMediaDownloader(_FakeClient({}))  # type: ignore[arg-type]

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "3D056A0F987123794BA2FA2C84A1E742.jpg",
                "source_path": str(pic_root / "Ori" / "3d056a0f987123794ba2fa2c84a1e742.jpg"),
                "download_hint": {},
            }
        )

        assert resolved == (big_variant.resolve(), "stale_source_neighbor")


def test_media_downloader_shared_old_missing_cache_skips_repeated_context_calls() -> None:
    client = _FakeClient({"file": "should-not-be-used"})
    fast_client = _FakeFastClient(fail=True)
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    request_a = {
        "asset_type": "image",
        "file_name": "95EA10C2DD53F66B9F480DC1980FD340.jpg",
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2024-01\Ori\95ea10c2dd53f66b9f480dc1980fd340.jpg",
        "md5": "95ea10c2dd53f66b9f480dc1980fd340",
        "timestamp_ms": 1704067200000,
        "download_hint": {
            "message_id_raw": "msg-old-a",
            "element_id": "element-old-a",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }
    request_b = {
        **request_a,
        "download_hint": {
            "message_id_raw": "msg-old-b",
            "element_id": "element-old-b",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    assert downloader.resolve_for_export(request_a) == (None, None)
    assert downloader.resolve_for_export(request_b) == (None, None)
    assert fast_client.calls == [
        ("msg-old-a", "element-old-a", "922065597", 2, "image", None)
    ]
    assert client.calls == []


def test_media_downloader_skips_old_bucket_after_expired_public_token_signal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="expired")

    with _repo_temp_dir("media_downloader_expired_bucket_skip") as tmp_path:
        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=expired-bucket",
                "file_name": "expired-bucket.png",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": "token-expired-bucket",
                "remote_url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=expired-bucket",
                "file_name": "expired-bucket.png",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )
        request_a = {
            "asset_type": "image",
            "file_name": "expired-bucket-a.png",
            "timestamp_ms": 1704067200000,
            "download_hint": {
                "message_id_raw": "msg-expired-a",
                "element_id": "element-expired-a",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
        request_b = {
            "asset_type": "image",
            "file_name": "expired-bucket-b.png",
            "timestamp_ms": 1704153600000,
            "download_hint": {
                "message_id_raw": "msg-expired-b",
                "element_id": "element-expired-b",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }

        assert downloader.resolve_for_export(request_a) == (None, "qq_expired_after_napcat")
        assert downloader.resolve_for_export(request_b) == (None, "qq_expired_after_napcat")
        assert fast_client.calls == [
            ("msg-expired-a", "element-expired-a", "922065597", 2, "image", None)
        ]
        assert client.calls == [("image", None, "token-expired-bucket")]


def test_media_downloader_skips_old_bucket_after_public_action_expired_signal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="expired")

    with _repo_temp_dir("media_downloader_expired_public_action_bucket_skip") as tmp_path:
        client = _FakeClient(
            {
                "file": "",
                "url": "https://gchat.qpic.cn/gchatpic_new/0/0-0-50A19C64412230A8F5CCAE45795D7BAC/0",
                "file_name": "",
            }
        )
        fast_client = _FakeFastClient(
            {
                "asset_type": "image",
                "file": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\50a19c64412230a8f5ccae45795d7bac",
                "url": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\50a19c64412230a8f5ccae45795d7bac",
                "public_action": "get_image",
                "public_file_token": "token-public-action-expired-bucket",
                "file_name": "50a19c64412230a8f5ccae45795d7bac",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )
        request_a = {
            "asset_type": "image",
            "file_name": "50a19c64412230a8f5ccae45795d7bac",
            "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-09\Ori\50a19c64412230a8f5ccae45795d7bac",
            "timestamp_ms": 1758615758000,
            "download_hint": {
                "message_id_raw": "msg-public-action-expired-a",
                "element_id": "element-public-action-expired-a",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
        request_b = {
            **request_a,
            "download_hint": {
                "message_id_raw": "msg-public-action-expired-b",
                "element_id": "element-public-action-expired-b",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }

        assert downloader.resolve_for_export(request_a) == (None, "qq_expired_after_napcat")
        assert downloader.resolve_for_export(request_b) == (None, "qq_expired_after_napcat")
        assert fast_client.calls == [
            ("msg-public-action-expired-a", "element-public-action-expired-a", "922065597", 2, "image", None)
        ]
        assert client.calls == [("image", None, "token-public-action-expired-bucket")]


def test_media_downloader_recent_image_gets_fresh_public_retry() -> None:
    class _FlakyFastClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, int, str | None, str | None]] = []

        def hydrate_media(
            self,
            *,
            message_id_raw: str,
            element_id: str,
            peer_uid: str,
            chat_type_raw: int | str,
            asset_type: str | None = None,
            asset_role: str | None = None,
            timeout: float | None = None,
        ):
            self.calls.append(
                (
                    message_id_raw,
                    element_id,
                    peer_uid,
                    int(chat_type_raw),
                    asset_type,
                    asset_role,
                )
            )
            return {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": f"token-{len(self.calls)}",
                "file_name": "recent.png",
            }

    with _repo_temp_dir("media_downloader_recent_fresh_retry") as tmp_path:
        image_payload = b"\x89PNG\r\n\x1a\nrecent-fresh-retry"
        request_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            request_count["n"] += 1
            if request_count["n"] == 1:
                return httpx.Response(404, text="not-ready-yet")
            return httpx.Response(200, content=image_payload)

        client = _FakeClient(
            {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=recent",
                "file_name": "recent.png",
            }
        )
        fast_client = _FlakyFastClient()
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "recent.png",
                "timestamp_ms": 1773460800000,
                "download_hint": {
                    "message_id_raw": "msg-recent-retry",
                    "element_id": "element-recent-retry",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved[0] is not None
        assert resolved[0].read_bytes() == image_payload
        assert resolved[1] == "napcat_public_token_get_image_remote_url"
        assert fast_client.calls == [
            ("msg-recent-retry", "element-recent-retry", "922065597", 2, "image", None),
            ("msg-recent-retry", "element-recent-retry", "922065597", 2, "image", None),
        ]
        assert client.calls == [
            ("image", None, "token-1"),
            ("image", None, "token-2"),
        ]


def test_media_downloader_prefetched_stale_public_token_falls_back_to_fresh_retry() -> None:
    class _BatchThenFreshFastClient:
        def __init__(self) -> None:
            self.batch_calls: list[list[dict[str, object]]] = []
            self.calls: list[tuple[str, str, str, int, str | None, str | None]] = []

        def hydrate_media_batch(self, items, *, timeout: float | None = None):
            self.batch_calls.append(list(items))
            return {
                "items": [
                    {
                        "ok": True,
                        "data": {
                            "asset_type": "image",
                            "public_action": "get_image",
                            "public_file_token": "token-batch",
                            "file_name": "prefetched.png",
                        },
                    }
                ]
            }

        def hydrate_media(
            self,
            *,
            message_id_raw: str,
            element_id: str,
            peer_uid: str,
            chat_type_raw: int | str,
            asset_type: str | None = None,
            asset_role: str | None = None,
            timeout: float | None = None,
        ):
            self.calls.append(
                (
                    message_id_raw,
                    element_id,
                    peer_uid,
                    int(chat_type_raw),
                    asset_type,
                    asset_role,
                )
            )
            return {
                "asset_type": "image",
                "public_action": "get_image",
                "public_file_token": "token-fresh",
                "file_name": "prefetched.png",
            }

    class _TokenAwareClient:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def get_image(
            self,
            *,
            file_id: str | None = None,
            file: str | None = None,
            timeout: float | None = None,
        ):
            token = file or file_id
            self.calls.append(("image", file_id, file))
            if token == "token-batch":
                return {
                    "file": "",
                    "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=batch-miss",
                    "file_name": "prefetched.png",
                }
            return {
                "file": "",
                "url": "https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=fresh-hit",
                "file_name": "prefetched.png",
            }

    with _repo_temp_dir("media_downloader_prefetched_fresh_retry") as tmp_path:
        image_payload = b"\x89PNG\r\n\x1a\nprefetched-fresh-retry"

        def handler(request: httpx.Request) -> httpx.Response:
            if "batch-miss" in str(request.url):
                return httpx.Response(404, text="prefetched-token-stale")
            return httpx.Response(200, content=image_payload)

        client = _TokenAwareClient()
        fast_client = _BatchThenFreshFastClient()
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )
        request = {
            "asset_type": "image",
            "file_name": "prefetched.png",
            "timestamp_ms": 1773460800000,
            "download_hint": {
                "message_id_raw": "msg-prefetched-retry",
                "element_id": "element-prefetched-retry",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }

        downloader.prepare_for_export([request])
        resolved = downloader.resolve_for_export(request)

        assert resolved[0] is not None
        assert resolved[0].read_bytes() == image_payload
        assert resolved[1] == "napcat_public_token_get_image_remote_url"
        assert len(fast_client.batch_calls) == 1
        assert fast_client.calls == [
            ("msg-prefetched-retry", "element-prefetched-retry", "922065597", 2, "image", None)
        ]
        assert client.calls == [
            ("image", None, "token-batch"),
            ("image", None, "token-fresh"),
        ]


def test_media_downloader_uses_get_record_with_public_token() -> None:
    with _repo_temp_dir("media_downloader_public_token_record") as tmp_path:
        audio_path = tmp_path / "token-record.amr"
        audio_path.write_bytes(b"#!AMR\nrecord")
        client = _FakeClient({"file": str(audio_path)})
        fast_client = _FakeFastClient(
            {
                "file": "C:/stale/record.amr",
                "public_action": "get_record",
                "public_file_token": "record-token-1",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "speech",
                "file_name": "demo.amr",
                "download_hint": {
                    "message_id_raw": "msg-record",
                    "element_id": "element-record",
                    "peer_uid": "1507833383",
                    "chat_type_raw": 1,
                },
            }
        )

        assert resolved == (audio_path.resolve(), "napcat_public_token_get_record")
        assert client.calls == [("record", None, "record-token-1", "mp3")]


def test_media_downloader_prefetch_batches_requests_in_chunks() -> None:
    class _ChunkingFastClient:
        def __init__(self) -> None:
            self.batch_calls: list[list[dict[str, object]]] = []

        def hydrate_media_batch(self, items):
            self.batch_calls.append(list(items))
            return {
                "items": [
                    {
                        "ok": True,
                        "data": {
                            "file": "",
                            "asset_type": item.get("asset_type"),
                        },
                    }
                    for item in items
                ]
            }

    client = _FakeClient({})
    fast_client = _ChunkingFastClient()
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )
    downloader.PREFETCH_BATCH_SIZE = 2
    requests = [
        {
            "asset_type": "image",
            "file_name": f"demo-{index}.png",
            "timestamp_ms": 1773460800000,
            "download_hint": {
                "message_id_raw": f"msg-{index}",
                "element_id": f"element-{index}",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
        for index in range(5)
    ]

    downloader.prepare_for_export(requests)

    assert [len(chunk) for chunk in fast_client.batch_calls] == [2, 2, 1]


def test_media_downloader_prefetch_allows_old_bucket_before_failure_evidence() -> None:
    class _OldBucketFastClient:
        def __init__(self) -> None:
            self.batch_calls: list[list[dict[str, object]]] = []

        def hydrate_media_batch(self, items):
            self.batch_calls.append(list(items))
            return {"items": [{"ok": False} for _item in items]}

    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient({}),
        fast_client=_OldBucketFastClient(),  # type: ignore[arg-type]
    )
    fast_client = downloader._fast_client
    request = {
        "asset_type": "image",
        "file_name": "old-prefetch.jpg",
        "timestamp_ms": 1704067200000,
        "download_hint": {
            "message_id_raw": "msg-old-prefetch",
            "element_id": "element-old-prefetch",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    downloader.prepare_for_export([request])

    assert fast_client is not None
    assert len(fast_client.batch_calls) == 1
    downloader._note_old_bucket_expired_like(("image", "2024-01"))
    downloader.prepare_for_export([request])
    assert len(fast_client.batch_calls) == 1


def test_media_downloader_old_prefetched_public_token_failures_enable_bucket_skip() -> None:
    client = _FakeClient({"file": "", "url": ""})
    downloader = NapCatMediaDownloader(client)  # type: ignore[arg-type]
    downloader.OLD_CONTEXT_BUCKET_FAILURE_LIMIT = 1

    request_a = {
        "asset_type": "image",
        "file_name": "old-a.jpg",
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2024-01\Ori\old-a.jpg",
        "timestamp_ms": 1704067200000,
        "download_hint": {
            "message_id_raw": "msg-old-a",
            "element_id": "element-old-a",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }
    request_b = {
        "asset_type": "image",
        "file_name": "old-b.jpg",
        "source_path": r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2024-01\Ori\old-b.jpg",
        "timestamp_ms": 1704067200000,
        "download_hint": {
            "message_id_raw": "msg-old-b",
            "element_id": "element-old-b",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    key_a = downloader._request_key(request_a)
    key_b = downloader._request_key(request_b)
    downloader._prefetched_media[key_a] = (None, None)
    downloader._prefetched_media_payloads[key_a] = {
        "asset_type": "image",
        "public_action": "get_image",
        "public_file_token": "old-token-a",
    }
    downloader._prefetched_media[key_b] = (None, None)
    downloader._prefetched_media_payloads[key_b] = {
        "asset_type": "image",
        "public_action": "get_image",
        "public_file_token": "old-token-b",
    }

    assert downloader.resolve_for_export(request_a) == (None, None)
    assert downloader.resolve_for_export(request_b) == (None, None)
    assert client.calls == [("image", None, "old-token-a")]


def test_media_downloader_does_not_call_public_file_after_context_hydration_failure() -> (
    None
):
    client = _FakeClient({"file": "should-not-be-used"})
    fast_client = _FakeFastClient(fail=True)
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    resolved = downloader.download_for_export(
        {
            "asset_type": "file",
            "file_name": "demo.mp4",
            "download_hint": {
                "file_id": "raw-fast-file-uuid",
                "message_id_raw": "msg-2",
                "element_id": "element-3",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
    )

    assert resolved is None
    assert fast_client.calls == [("msg-2", "element-3", "922065597", 2, "file", None)]
    assert client.calls == []


def test_media_downloader_skips_old_context_bucket_after_repeated_failures() -> None:
    client = _FakeClient({"file": "should-not-be-used"})
    fast_client = _FakeFastClient(fail=True)
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    request = {
        "asset_type": "image",
        "file_name": "demo.png",
        "timestamp_ms": 1758445200000,  # 2025-09-21T21:00:00+08:00
        "download_hint": {
            "file_id": "raw-fast-image-uuid",
            "message_id_raw": "msg-old",
            "element_id": "element-old",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    for _ in range(NapCatMediaDownloader.OLD_CONTEXT_BUCKET_FAILURE_LIMIT):
        assert downloader.download_for_export(dict(request)) is None

    assert len(fast_client.calls) == NapCatMediaDownloader.OLD_CONTEXT_BUCKET_FAILURE_LIMIT
    assert downloader.download_for_export(dict(request)) is None
    assert len(fast_client.calls) == NapCatMediaDownloader.OLD_CONTEXT_BUCKET_FAILURE_LIMIT
    assert client.calls == []


def test_media_downloader_does_not_bucket_skip_recentish_context_assets() -> None:
    client = _FakeClient({"file": "should-not-be-used"})
    fast_client = _FakeFastClient(fail=True)
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    request = {
        "asset_type": "image",
        "file_name": "demo.png",
        "timestamp_ms": 1767225600000,  # 2026-01-01T00:00:00+08:00-ish; should stay hydratable
        "download_hint": {
            "file_id": "raw-fast-image-uuid",
            "message_id_raw": "msg-recentish",
            "element_id": "element-recentish",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
        },
    }

    for _ in range(6):
        assert downloader.download_for_export(dict(request)) is None

    assert len(fast_client.calls) == 6
    assert client.calls == []


def test_media_downloader_downloads_dynamic_sticker_via_remote_url() -> None:
    with _repo_temp_dir("media_downloader_remote_sticker_dynamic") as tmp_path:
        gif_payload = b"GIF89a\x02\x00\x02\x00\x80\x00\x00\xff\x00\x00\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x02\x00\x02\x00\x00\x02\x03D\x02\x05\x00;"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(
                "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/raw300.gif"
            )
            return httpx.Response(200, content=gif_payload)

        client = _FakeClient({"file": "should-not-be-used"})
        fast_client = _FakeFastClient({"file": "should-not-be-used"})
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.download_for_export(
            {
                "asset_type": "sticker",
                "asset_role": "dynamic",
                "download_hint": {
                    "emoji_id": "abcdef",
                    "remote_url": "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/raw300.gif",
                    "remote_file_name": "ab-abcdef.gif",
                    "message_id_raw": "msg-9",
                    "element_id": "element-7",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            }
        )

        assert resolved is not None
        assert resolved.name == "ab-abcdef.gif"
        assert resolved.read_bytes() == gif_payload
        assert fast_client.calls == []
        assert client.calls == []


def test_media_downloader_resolve_for_export_falls_back_to_remote_sticker_url() -> None:
    with _repo_temp_dir("media_downloader_resolve_remote_sticker") as tmp_path:
        gif_payload = b"GIF89a\x02\x00\x02\x00\x80\x00\x00\xff\x00\x00\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x02\x00\x02\x00\x00\x02\x03D\x02\x05\x00;"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(
                "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/raw300.gif"
            )
            return httpx.Response(200, content=gif_payload)

        client = _FakeClient({"file": "should-not-be-used"})
        fast_client = _FakeFastClient(fail=True)
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "sticker",
                "asset_role": "dynamic",
                "file_name": "ab-abcdef.gif",
                "download_hint": {
                    "message_id_raw": "msg-sticker",
                    "element_id": "element-sticker",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                    "remote_url": "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/raw300.gif",
                    "remote_file_name": "ab-abcdef.gif",
                },
            }
        )

        assert resolved is not None
        assert resolved[0] is not None
        assert resolved[0].name == "ab-abcdef.gif"
        assert resolved[0].read_bytes() == gif_payload
        assert resolved[1] == "sticker_remote_download"
        assert fast_client.calls == [
            ("msg-sticker", "element-sticker", "922065597", 2, "sticker", "dynamic")
        ]
        assert client.calls == []


def test_media_downloader_keeps_static_sticker_as_native_gif_from_remote_url() -> None:
    with _repo_temp_dir("media_downloader_remote_sticker_static") as tmp_path:
        gif_payload = b"GIF89a\x02\x00\x02\x00\x80\x00\x00\x00\xff\x00\x00\x00\x00!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x02\x00\x02\x00\x00\x02\x03D\x02\x05\x00;"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=gif_payload)

        client = _FakeClient({"file": "should-not-be-used"})
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.download_for_export(
            {
                "asset_type": "sticker",
                "asset_role": "static",
                "file_name": "abcdef_aio.gif",
                "download_hint": {
                    "emoji_id": "abcdef",
                    "remote_url": "https://gxh.vip.qq.com/club/item/parcel/item/ab/abcdef/raw300.gif",
                    "remote_file_name": "ab-abcdef.gif",
                },
            }
        )

        assert resolved is not None
        assert resolved.name == "ab-abcdef.gif"
        assert resolved.suffix.lower() == ".gif"
        assert resolved.read_bytes() == gif_payload
        assert client.calls == []


def test_media_downloader_skips_public_token_route_for_sticker_assets() -> None:
    client = _FakeClient({"file": "should-not-be-used"})
    fast_client = _FakeFastClient(
        {
            "asset_type": "sticker",
            "file": "",
            "public_action": "get_image",
            "public_file_token": "sticker-token-1",
        }
    )
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    resolved = downloader.resolve_via_public_token_route(
        {
            "asset_type": "sticker",
            "asset_role": "dynamic",
            "file_name": "ab-abcdef.gif",
            "download_hint": {
                "message_id_raw": "msg-sticker-token",
                "element_id": "element-sticker-token",
                "peer_uid": "922065597",
                "chat_type_raw": 2,
            },
        }
    )

    assert resolved == (None, None)
    assert fast_client.calls == [
        (
            "msg-sticker-token",
            "element-sticker-token",
            "922065597",
            2,
            "sticker",
            "dynamic",
        )
    ]
    assert client.calls == []


def test_media_downloader_downloads_image_via_remote_url_when_public_lookup_is_unavailable() -> (
    None
):
    with _repo_temp_dir("media_downloader_remote_image") as tmp_path:
        image_payload = b"\x89PNG\r\n\x1a\nremote-image"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(
                "https://multimedia.nt.qq.com.cn/demo/nested-forward.png"
            )
            return httpx.Response(200, content=image_payload)

        client = _FakeClient(fail=True)
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.download_for_export(
            {
                "asset_type": "image",
                "file_name": "nested-forward.png",
                "download_hint": {
                    "url": "https://multimedia.nt.qq.com.cn/demo/nested-forward.png",
                },
            }
        )

        assert resolved is not None


def test_media_downloader_downloads_image_via_relative_remote_url() -> None:
    with _repo_temp_dir("media_downloader_relative_remote_image") as tmp_path:
        image_payload = b"\x89PNG\r\n\x1a\nrelative-image"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL(
                "http://127.0.0.1:3000/download?appid=1407&fileid=abc"
            )
            return httpx.Response(200, content=image_payload)

        client = _FakeClient(fail=True)
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            remote_cache_dir=tmp_path / "cache",
            remote_base_url="http://127.0.0.1:3000",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.download_for_export(
            {
                "asset_type": "image",
                "file_name": "relative.png",
                "download_hint": {
                    "url": "/download?appid=1407&fileid=abc",
                },
            }
        )

        assert resolved is not None
        assert resolved.name == "relative.png"
        assert resolved.read_bytes() == image_payload
        assert resolved.read_bytes() == image_payload


def test_media_downloader_matches_forward_assets_by_file_id() -> None:
    with _repo_temp_dir("media_downloader_forward_file_id") as tmp_path:
        image_path = tmp_path / "forward-by-file-id.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nforward-file-id")
        client = _FakeClient({"file": str(image_path)})
        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "image",
                        "asset_role": "",
                        "file_name": "completely-different-name.png",
                        "file_id": "forward-file-id",
                        "public_action": "get_image",
                        "public_file_token": "forward-token-1",
                    }
                ]
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "request-name.png",
                "download_hint": {
                    "file_id": "forward-file-id",
                    "_forward_parent": {
                        "message_id_raw": "parent-msg-1",
                        "element_id": "forward-element-1",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (image_path.resolve(), "napcat_public_token_get_image")
        assert fast_client.forward_calls == [
            ("parent-msg-1", "forward-element-1", "922065597", 2)
        ]
        assert client.calls == [("image", None, "forward-token-1")]


def test_media_downloader_passes_target_hints_into_forward_hydration() -> None:
    fast_client = _FakeFastClient(forward_payload={"assets": []})
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient(),
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    assert downloader.resolve_for_export(
        {
            "asset_type": "video",
            "file_name": "demo-video.mp4",
            "md5": "abc123",
            "download_hint": {
                "file_id": "/forward-file-id",
                "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\demo-video.mp4",
                "_forward_parent": {
                    "message_id_raw": "parent-msg-video",
                    "element_id": "forward-element-video",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                },
            },
        }
    ) == (None, None)

    assert fast_client.forward_calls == [
        ("parent-msg-video", "forward-element-video", "922065597", 2),
        ("parent-msg-video", "forward-element-video", "922065597", 2),
    ]
    assert fast_client.forward_payloads == [
        {
            "message_id_raw": "parent-msg-video",
            "element_id": "forward-element-video",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
            "asset_type": "video",
            "asset_role": None,
            "file_name": "demo-video.mp4",
            "md5": "abc123",
            "file_id": "/forward-file-id",
            "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\demo-video.mp4",
            "materialize": False,
            "download_timeout_ms": None,
            "timeout": 12.0,
        },
        {
            "message_id_raw": "parent-msg-video",
            "element_id": "forward-element-video",
            "peer_uid": "922065597",
            "chat_type_raw": 2,
            "asset_type": "video",
            "asset_role": None,
            "file_name": "demo-video.mp4",
            "md5": "abc123",
            "file_id": "/forward-file-id",
            "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\demo-video.mp4",
            "materialize": True,
            "download_timeout_ms": 20000,
            "timeout": 25.0,
        },
    ]


def test_media_downloader_matches_forward_assets_by_remote_url() -> None:
    with _repo_temp_dir("media_downloader_forward_url") as tmp_path:
        image_path = tmp_path / "forward-by-url.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nforward-url")
        client = _FakeClient({"file": str(image_path)})
        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "image",
                        "asset_role": "",
                        "file_name": "different-name.png",
                        "remote_url": "https://multimedia.nt.qq.com.cn/demo/nested-forward.png",
                        "public_action": "get_image",
                        "public_file_token": "forward-token-2",
                    }
                ]
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "request-other-name.png",
                "download_hint": {
                    "url": "https://multimedia.nt.qq.com.cn/demo/nested-forward.png",
                    "_forward_parent": {
                        "message_id_raw": "parent-msg-2",
                        "element_id": "forward-element-2",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (image_path.resolve(), "napcat_public_token_get_image")
        assert fast_client.forward_calls == [
            ("parent-msg-2", "forward-element-2", "922065597", 2)
        ]
        assert client.calls == [("image", None, "forward-token-2")]


def test_media_downloader_matches_forward_assets_by_file_stem() -> None:
    with _repo_temp_dir("media_downloader_forward_stem") as tmp_path:
        image_path = tmp_path / "same-stem.webp"
        image_path.write_bytes(b"RIFFxxxxWEBPforward-stem")
        client = _FakeClient({"file": str(image_path)})
        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "image",
                        "asset_role": "",
                        "file_name": "same-stem.webp",
                        "public_action": "get_image",
                        "public_file_token": "forward-token-3",
                    }
                ]
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "same-stem.jpg",
                "download_hint": {
                    "_forward_parent": {
                        "message_id_raw": "parent-msg-3",
                        "element_id": "forward-element-3",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (image_path.resolve(), "napcat_public_token_get_image")
        assert fast_client.forward_calls == [
            ("parent-msg-3", "forward-element-3", "922065597", 2)
        ]
        assert client.calls == [("image", None, "forward-token-3")]


def test_media_downloader_matches_forward_assets_by_md5_with_direct_payload_path() -> None:
    with _repo_temp_dir("media_downloader_forward_md5_direct") as tmp_path:
        image_path = tmp_path / "forward-md5.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nforward-md5")
        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "image",
                        "asset_role": "",
                        "file": str(image_path),
                        "md5": "abcd1234",
                        "source_message_id_raw": "msg-forward-inner",
                    }
                ]
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            _FakeClient({}),
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "whatever.png",
                "md5": "abcd1234",
                "download_hint": {
                    "_forward_parent": {
                        "message_id_raw": "msg-forward-parent",
                        "element_id": "element-forward-parent",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    }
                },
            }
        )

        assert resolved == (image_path.resolve(), "napcat_forward_hydrated")
    assert fast_client.forward_calls == [
        ("msg-forward-parent", "element-forward-parent", "922065597", 2)
    ]


def test_media_downloader_uses_forward_remote_url_when_token_and_path_absent() -> None:
    with _repo_temp_dir("media_downloader_forward_remote_url") as tmp_path:
        gif_payload = (
            b"GIF89a\x02\x00\x02\x00\x80\x00\x00\xff\x00\x00\x00\x00\x00"
            b"!\xf9\x04\x00\x00\x00\x00\x00,\x00\x00\x00\x00\x02\x00\x02\x00"
            b"\x00\x02\x03D\x02\x05\x00;"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?file=forward-image")
            return httpx.Response(200, content=gif_payload, headers={"Content-Type": "image/gif"})

        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "image",
                        "asset_role": "",
                        "file_name": "nested-forward.jpg",
                        "url": "https://multimedia.nt.qq.com.cn/download?file=forward-image",
                        "source_message_id_raw": "msg-forward-inner",
                    }
                ]
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            _FakeClient({}),
            fast_client=fast_client,  # type: ignore[arg-type]
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "nested-forward.jpg",
                "download_hint": {
                    "_forward_parent": {
                        "message_id_raw": "msg-forward-parent",
                        "element_id": "element-forward-parent",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    }
                },
            }
        )

        assert resolved[1] == "napcat_forward_remote_url"
        assert resolved[0] is not None
        assert resolved[0].read_bytes() == gif_payload
        assert fast_client.forward_calls == [
            ("msg-forward-parent", "element-forward-parent", "922065597", 2)
        ]


def test_media_downloader_uses_request_forward_url_before_plugin_route() -> None:
    with _repo_temp_dir("media_downloader_request_forward_remote_url") as tmp_path:
        image_payload = b"\x89PNG\r\n\x1a\nforward-request-url"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == httpx.URL("https://multimedia.nt.qq.com.cn/download?file=request-forward-image")
            return httpx.Response(200, content=image_payload, headers={"Content-Type": "image/png"})

        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            _FakeClient({}),
            remote_cache_dir=tmp_path / "cache",
            remote_transport=httpx.MockTransport(handler),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "image",
                "file_name": "nested-forward.png",
                "download_hint": {
                    "url": "https://multimedia.nt.qq.com.cn/download?file=request-forward-image",
                    "_forward_parent": {
                        "message_id_raw": "msg-forward-parent",
                        "element_id": "element-forward-parent",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved[1] == "napcat_forward_remote_url"
        assert resolved[0] is not None
        assert resolved[0].read_bytes() == image_payload


def test_media_downloader_uses_local_path_hidden_in_forward_hint_url_for_video() -> None:
    with _repo_temp_dir("media_downloader_forward_hint_local_video") as tmp_path:
        video_path = tmp_path / "Video" / "2026-02" / "Ori" / "dc4fdfa37904fb8e25a551363ab52389.mp4"
        video_path.parent.mkdir(parents=True, exist_ok=True)
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42forward-video")

        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            _FakeClient({}),
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "video",
                "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                "download_hint": {
                    "url": str(video_path),
                    "_forward_parent": {
                        "message_id_raw": "msg-forward-parent",
                        "element_id": "element-forward-parent",
                        "peer_uid": "922065597",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (video_path.resolve(), "hint_local_path")


def test_media_downloader_resolves_forward_video_via_public_token_without_hydration_path() -> None:
    with _repo_temp_dir("media_downloader_forward_video_public_token") as tmp_path:
        video_path = tmp_path / "forward-video.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp42forward-video-token")
        client = _FakeClient({"file": str(video_path)})
        fast_client = _FakeFastClient(
            forward_payload={
                "assets": [
                    {
                        "asset_type": "video",
                        "asset_role": "",
                        "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                        "public_action": "get_file",
                        "public_file_token": "forward-video-token-1",
                        "file_id": "",
                        "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\dc4fdfa37904fb8e25a551363ab52389.mp4",
                    }
                ],
                "targeted": True,
                "targeted_mode": "metadata_only",
            }
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "video",
                "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                "download_hint": {
                    "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\dc4fdfa37904fb8e25a551363ab52389.mp4",
                    "_forward_parent": {
                        "message_id_raw": "msg-forward-parent",
                        "element_id": "element-forward-parent",
                        "peer_uid": "751365230",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (video_path.resolve(), "napcat_public_token_get_file")
        assert client.calls == [("file", None, "forward-video-token-1")]


def test_media_downloader_materializes_forward_video_after_metadata_only_miss() -> None:
    with _repo_temp_dir("media_downloader_forward_video_materialize") as tmp_path:
        video_path = tmp_path / "dc4fdfa37904fb8e25a551363ab52389.mp4"
        video_path.write_bytes(b"video-bytes")
        client = _FakeClient({})
        fast_client = _FakeFastClient(
            forward_payload_sequence=[
                {
                    "assets": [
                        {
                            "asset_type": "video",
                            "asset_role": "",
                            "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                            "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\dc4fdfa37904fb8e25a551363ab52389.mp4",
                        }
                    ],
                    "targeted": True,
                    "targeted_mode": "metadata_only",
                },
                {
                    "assets": [
                        {
                            "asset_type": "video",
                            "asset_role": "",
                            "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                            "file": str(video_path),
                        }
                    ],
                    "targeted": True,
                    "targeted_mode": "single_target_download",
                },
            ]
        )
        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            client,
            fast_client=fast_client,  # type: ignore[arg-type]
        )

        resolved = downloader.resolve_for_export(
            {
                "asset_type": "video",
                "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                "download_hint": {
                    "url": r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\dc4fdfa37904fb8e25a551363ab52389.mp4",
                    "_forward_parent": {
                        "message_id_raw": "forward-parent-msg",
                        "element_id": "forward-parent-element",
                        "peer_uid": "751365230",
                        "chat_type_raw": 2,
                    },
                },
            }
        )

        assert resolved == (video_path.resolve(), "napcat_forward_hydrated")
        assert [payload["materialize"] for payload in fast_client.forward_payloads] == [False, True]
        assert fast_client.forward_payloads[1]["download_timeout_ms"] == 20000
        assert [payload["timeout"] for payload in fast_client.forward_payloads] == [12.0, 25.0]
        assert fast_client.calls == []


def test_media_downloader_shares_recent_forward_file_missing_outcome() -> None:
    fast_client = _FakeFastClient(
        forward_payload_sequence=[
            {
                "assets": [
                    {
                        "asset_type": "file",
                        "asset_role": "",
                        "file_name": "uploaded_file",
                        "file_id": "/8311cda0-18b5-11f1-bb89-5254001607c6",
                    }
                ],
                "targeted": True,
                "targeted_mode": "metadata_only",
            },
            {
                "assets": [
                    {
                        "asset_type": "file",
                        "asset_role": "",
                        "file_name": "uploaded_file",
                        "file_id": "/8311cda0-18b5-11f1-bb89-5254001607c6",
                    }
                ],
                "targeted": True,
                "targeted_mode": "single_target_download",
            },
        ]
    )
    client = _FakeClient({})
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        client,
        fast_client=fast_client,  # type: ignore[arg-type]
    )
    request = {
        "asset_type": "file",
        "file_name": "uploaded_file",
        "timestamp_ms": 1773594349000,
        "download_hint": {
            "file_id": "/8311cda0-18b5-11f1-bb89-5254001607c6",
            "_forward_parent": {
                "message_id_raw": "forward-parent-msg",
                "element_id": "forward-parent-element",
                "peer_uid": "751365230",
                "chat_type_raw": 2,
            },
        },
    }

    first = downloader.resolve_for_export(request)
    second = downloader.resolve_for_export(dict(request))

    assert first == (None, None)
    assert second == (None, None)
    assert len(fast_client.forward_payloads) == 2
    assert [payload["materialize"] for payload in fast_client.forward_payloads] == [False, True]
    assert client.calls == [("file", "/8311cda0-18b5-11f1-bb89-5254001607c6", None)]


def test_media_downloader_treats_forward_timeout_as_retryable_missing() -> None:
    fast_client = _FakeFastClient(forward_exception=NapCatFastHistoryTimeoutError("timed out"))
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient({}),
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    resolved = downloader.resolve_for_export(
        {
            "asset_type": "video",
            "file_name": "demo-video.mp4",
            "download_hint": {
                "_forward_parent": {
                    "message_id_raw": "forward-parent-msg",
                    "element_id": "forward-parent-element",
                    "peer_uid": "751365230",
                    "chat_type_raw": 2,
                },
            },
        }
    )

    assert resolved == (None, None)


def test_media_downloader_skips_forward_hydration_when_parent_element_id_blank() -> None:
    fast_client = _FakeFastClient(
        forward_payload={
            "assets": [
                {
                    "asset_type": "image",
                    "asset_role": "",
                    "file_name": "unused.png",
                    "public_action": "get_image",
                    "public_file_token": "forward-token-unused",
                }
            ]
        }
    )
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient({}),
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    resolved = downloader.resolve_for_export(
        {
            "asset_type": "image",
            "file_name": "whatever.png",
            "download_hint": {
                "_forward_parent": {
                    "message_id_raw": "msg-forward-parent",
                    "element_id": "   ",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                }
            },
        }
    )

    assert resolved == (None, None)
    assert fast_client.forward_calls == []


def test_media_downloader_classifies_stale_forward_residual_image_as_expired() -> None:
    fast_client = _FakeFastClient(forward_payload={"assets": []})
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient({}),
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    stale_timestamp_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=35)).timestamp() * 1000
    )
    resolved = downloader.resolve_for_export(
        {
            "asset_type": "image",
            "file_name": "forward-residual.png",
            "timestamp_ms": stale_timestamp_ms,
            "download_hint": {
                "_forward_parent": {
                    "message_id_raw": "msg-forward-parent-stale",
                    "element_id": "element-forward-parent-stale",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                }
            },
        }
    )

    assert resolved == (None, "qq_expired_after_napcat")
    assert fast_client.forward_calls == []


def test_media_downloader_keeps_recent_forward_residual_image_unclassified() -> None:
    fast_client = _FakeFastClient(forward_payload={"assets": []})
    downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
        _FakeClient({}),
        fast_client=fast_client,  # type: ignore[arg-type]
    )

    recent_timestamp_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000
    )
    resolved = downloader.resolve_for_export(
        {
            "asset_type": "image",
            "file_name": "forward-recent.png",
            "timestamp_ms": recent_timestamp_ms,
            "download_hint": {
                "_forward_parent": {
                    "message_id_raw": "msg-forward-parent-recent",
                    "element_id": "element-forward-parent-recent",
                    "peer_uid": "922065597",
                    "chat_type_raw": 2,
                }
            },
        }
    )

    assert resolved == (None, None)
    assert fast_client.forward_calls == [
        ("msg-forward-parent-recent", "element-forward-parent-recent", "922065597", 2)
    ]


def test_media_downloader_cleanup_remote_cache_clears_disk_and_memory_state() -> None:
    with _repo_temp_dir("media_downloader_cleanup_remote_cache") as tmp_path:
        cache_root = tmp_path / "cache"
        remote_image = cache_root / "remote_media" / "image" / "demo.png"
        remote_sticker = cache_root / "remote_stickers" / "static" / "demo.gif"
        remote_image.parent.mkdir(parents=True, exist_ok=True)
        remote_sticker.parent.mkdir(parents=True, exist_ok=True)
        image_bytes = b"\x89PNG\r\n\x1a\ncleanup"
        sticker_bytes = b"GIF89acleanup"
        remote_image.write_bytes(image_bytes)
        remote_sticker.write_bytes(sticker_bytes)

        downloader = NapCatMediaDownloader(  # type: ignore[arg-type]
            _FakeClient({}),
            remote_cache_dir=cache_root,
        )
        downloader._prefetched_media[("request",)] = (remote_image, "prefetched")
        downloader._prefetched_media_payloads[("request",)] = {"file_id": "abc"}
        downloader._shared_media_outcomes[("shared",)] = (remote_sticker, "shared")
        downloader._old_context_failure_buckets[("image", "2026-03")] = 2
        downloader._old_context_skip_logged.add(("image", "2026-03"))
        downloader._old_context_expired_buckets.add(("image", "2026-03"))

        stats = downloader.cleanup_remote_cache()

        assert stats["cache_cleared"] is True
        assert stats["removed_files"] == 2
        assert stats["freed_bytes"] == len(image_bytes) + len(sticker_bytes)
        assert cache_root.exists()
        assert list(cache_root.iterdir()) == []
        assert downloader._prefetched_media == {}
        assert downloader._prefetched_media_payloads == {}
        assert downloader._shared_media_outcomes == {}
        assert downloader._old_context_failure_buckets == {}
        assert downloader._old_context_skip_logged == set()
        assert downloader._old_context_expired_buckets == set()
