from __future__ import annotations

import httpx

from qq_data_core import ChatExportService, ExportRequest
from qq_data_integrations.napcat import (
    NapCatFastHistoryClient,
    NapCatFastHistoryTimeoutError,
    NapCatHistoryProvider,
    NapCatHttpClient,
    derive_fast_history_url,
)


def test_derive_fast_history_url_from_webui_api_url() -> None:
    assert (
        derive_fast_history_url("http://127.0.0.1:6099/api")
        == "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api"
    )


def test_napcat_history_provider_prefers_fast_history_plugin() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/plugin/napcat-plugin-qq-data-fast/api/history")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "messages": [
                        {
                            "time": 1710000000,
                            "user_id": "1507833383",
                            "message_id": "9001",
                            "message_seq": "101",
                            "anchor_message_id": "9001",
                            "sender": {
                                "uin": "1507833383",
                                "name": "朋友A",
                            },
                            "rawMessage": {
                                "msgId": "9001",
                                "msgSeq": "101",
                                "msgTime": "1710000000",
                                "senderUin": "1507833383",
                                "elements": [
                                    {
                                        "elementType": 1,
                                        "textElement": {
                                            "content": "hello fast",
                                        },
                                    }
                                ],
                            },
                        }
                    ]
                },
            },
        )

    def public_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"public history should not be called: {request.url}")

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )
    public_client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(public_handler),
    )
    provider = NapCatHistoryProvider(public_client, fast_client=fast_client, fast_mode="auto")

    source = provider.fetch_snapshot(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=1)
    )
    normalized = ChatExportService().build_snapshot(source)

    assert source.metadata["source"] == "napcat_fast_history"
    assert normalized.messages[0].content == "hello fast"
    public_client.close()
    fast_client.close()


def test_napcat_history_provider_falls_back_when_fast_history_unavailable() -> None:
    def fast_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": -1, "message": "not found"})

    def public_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_friend_msg_history")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "message_id": 1,
                            "message_seq": 1,
                            "time": 1710000000,
                            "user_id": 1507833383,
                            "message": [{"type": "text", "data": {"text": "fallback"}}],
                        }
                    ]
                },
            },
        )

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )
    public_client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(public_handler),
    )
    provider = NapCatHistoryProvider(public_client, fast_client=fast_client, fast_mode="auto")

    source = provider.fetch_snapshot(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=1)
    )

    assert source.metadata["source"] == "napcat_http"
    assert source.messages[0]["message"][0]["data"]["text"] == "fallback"
    public_client.close()
    fast_client.close()


def test_fast_history_client_sends_custom_headers() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer test-credential"
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        headers={"Authorization": "Bearer test-credential"},
        transport=httpx.MockTransport(fast_handler),
    )
    assert fast_client.health() == {"ok": True}
    fast_client.close()


def test_fast_history_client_requests_bulk_tail_route() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/plugin/napcat-plugin-qq-data-fast/api/history-tail-bulk"
        )
        assert request.content == (
            b'{"chat_type":"group","chat_id":"922065597","data_count":2000,"page_size":200}'
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chat_type": "group",
                    "chat_id": "922065597",
                    "requested_data_count": 2000,
                    "page_size": 200,
                    "pages_scanned": 10,
                    "messages": [],
                },
            },
        )

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )

    payload = fast_client.get_history_tail_bulk(
        "group",
        "922065597",
        data_count=2000,
        page_size=200,
    )

    assert payload["requested_data_count"] == 2000
    assert payload["pages_scanned"] == 10
    fast_client.close()


def test_fast_history_client_requests_bulk_tail_route_with_anchor() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/plugin/napcat-plugin-qq-data-fast/api/history-tail-bulk"
        )
        assert request.content == (
            b'{"chat_type":"private","chat_id":"1507833383","data_count":800,"page_size":200,"anchor_message_id":"123456"}'
        )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chat_type": "private",
                    "chat_id": "1507833383",
                    "requested_data_count": 800,
                    "start_anchor_message_id": "123456",
                    "page_size": 200,
                    "pages_scanned": 4,
                    "next_anchor": "120000",
                    "exhausted": False,
                    "messages": [],
                },
            },
        )

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )

    payload = fast_client.get_history_tail_bulk(
        "private",
        "1507833383",
        data_count=800,
        page_size=200,
        anchor_message_id="123456",
    )

    assert payload["start_anchor_message_id"] == "123456"
    assert payload["next_anchor"] == "120000"
    fast_client.close()


def test_fast_history_client_sends_targeted_forward_hydration_payload() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/plugin/napcat-plugin-qq-data-fast/api/hydrate-forward-media"
        )
        assert request.content == (
            b'{"message_id_raw":"parent-msg-1","element_id":"forward-element-1","peer_uid":"922065597",'
            b'"chat_type_raw":2,"asset_type":"video","file_name":"demo-video.mp4","md5":"abc123",'
            b'"file_id":"/forward-file-id","url":"D:\\\\QQHOT\\\\Tencent Files\\\\2141129832\\\\nt_qq\\\\nt_data\\\\Video\\\\2026-02\\\\Ori\\\\demo-video.mp4"}'
        )
        return httpx.Response(200, json={"code": 0, "data": {"assets": []}})

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )

    payload = fast_client.hydrate_forward_media(
        message_id_raw="parent-msg-1",
        element_id="forward-element-1",
        peer_uid="922065597",
        chat_type_raw=2,
        asset_type="video",
        file_name="demo-video.mp4",
        md5="abc123",
        file_id="/forward-file-id",
        url=r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\demo-video.mp4",
    )

    assert payload == {"assets": []}
    fast_client.close()


def test_fast_history_client_sends_materialized_forward_hydration_payload() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(
            "/plugin/napcat-plugin-qq-data-fast/api/hydrate-forward-media"
        )
        assert request.content == (
            b'{"message_id_raw":"parent-msg-1","element_id":"forward-element-1","peer_uid":"922065597",'
            b'"chat_type_raw":2,"asset_type":"video","file_name":"demo-video.mp4","materialize":true,'
            b'"download_timeout_ms":20000}'
        )
        return httpx.Response(200, json={"code": 0, "data": {"assets": [], "targeted_mode": "single_target_download"}})

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )

    payload = fast_client.hydrate_forward_media(
        message_id_raw="parent-msg-1",
        element_id="forward-element-1",
        peer_uid="922065597",
        chat_type_raw=2,
        asset_type="video",
        file_name="demo-video.mp4",
        materialize=True,
        download_timeout_ms=20000,
    )

    assert payload["targeted_mode"] == "single_target_download"
    fast_client.close()


def test_fast_history_client_raises_timeout_error_on_read_timeout() -> None:
    def fast_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    fast_client = NapCatFastHistoryClient(
        "http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        transport=httpx.MockTransport(fast_handler),
    )

    try:
        fast_client.hydrate_media(
            message_id_raw="msg-timeout",
            element_id="element-timeout",
            peer_uid="922065597",
            chat_type_raw=2,
            asset_type="image",
            timeout=3.0,
        )
    except NapCatFastHistoryTimeoutError as exc:
        assert "/hydrate-media" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected NapCatFastHistoryTimeoutError")
    finally:
        fast_client.close()
