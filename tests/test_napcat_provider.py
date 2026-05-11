from __future__ import annotations

from datetime import datetime

import httpx
import qq_data_integrations.napcat.provider as provider_module

from qq_data_core import ChatExportService, ExportRequest, SourceChatSnapshot
from qq_data_core.models import EXPORT_TIMEZONE
from qq_data_integrations.napcat import (
    NapCatFastHistoryUnavailable,
    NapCatHistoryProvider,
    NapCatHttpClient,
)


def test_napcat_private_history_roundtrip() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_friend_msg_history")
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": [
                    {
                        "message_id": 9001,
                        "time": 1710000000,
                        "user_id": 1507833383,
                        "message_type": "private",
                        "message": [
                            {"type": "text", "data": {"text": "hello"}},
                            {
                                "type": "image",
                                "data": {
                                    "name": "from_napcat.jpg",
                                    "path": "C:\\QQ\\Pic\\from_napcat.jpg",
                                },
                            },
                            {
                                "type": "record",
                                "data": {
                                    "name": "voice.amr",
                                    "path": "C:\\QQ\\Ptt\\voice.amr",
                                },
                            },
                            {"type": "face", "data": {"id": "177"}},
                            {
                                "type": "mface",
                                "data": {
                                    "summary": "[困]",
                                    "emoji_id": "821860bafef7473b99ff6b9358035954",
                                    "emoji_package_id": 237962,
                                    "key": "k",
                                },
                            },
                        ],
                    }
                ],
            },
        )

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    source = provider.fetch_snapshot(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=1)
    )
    normalized = ChatExportService().build_snapshot(source)

    assert len(normalized.messages) == 1
    message = normalized.messages[0]
    assert (
        message.content
        == "hello [image:from_napcat.jpg] [speech audio] [emoji:id=177] [sticker:summary=[困],emoji_id=821860bafef7473b99ff6b9358035954,package_id=237962]"
    )
    assert message.image_file_names == ["from_napcat.jpg"]
    assert message.emoji_tokens == [
        "[emoji:id=177]",
        "[sticker:summary=[困],emoji_id=821860bafef7473b99ff6b9358035954,package_id=237962]",
    ]
    client.close()


def test_napcat_history_before_passes_message_seq_anchor() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/get_friend_msg_history")
        assert request.content == (
            b'{"user_id":1507833383,"count":5,"disable_get_url":true,"parse_mult_msg":false,'
            b'"message_seq":"778899","reverse_order":true}'
        )
        return httpx.Response(200, json={"status": "ok", "data": []})

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot_before(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=5),
        before_message_seq="778899",
        count=5,
    )

    assert snapshot.metadata["before_message_seq"] == "778899"
    assert snapshot.metadata["requested_count"] == 5
    assert snapshot.metadata["reverse_order"] is True
    client.close()


def test_napcat_fetch_snapshot_enriches_forward_content_recursively() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_friend_msg_history"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 700,
                                "message_seq": 700,
                                "time": 1710000700,
                                "user_id": 1,
                                "message": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "700",
                                            "title": "聊天记录",
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/get_forward_msg"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "type": "node",
                                "data": {
                                    "user_id": "111",
                                    "nickname": "甲",
                                    "message": [
                                        {"type": "text", "data": {"text": "外层正文"}},
                                        {
                                            "type": "node",
                                            "data": {
                                                "message": [
                                                    {
                                                        "type": "node",
                                                        "data": {
                                                            "user_id": "222",
                                                            "nickname": "乙",
                                                            "message": [
                                                                {
                                                                    "type": "text",
                                                                    "data": {
                                                                        "text": "内层正文"
                                                                    },
                                                                }
                                                            ],
                                                        },
                                                    }
                                                ]
                                            },
                                        },
                                    ],
                                },
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=1)
    )
    normalized = ChatExportService().build_snapshot(snapshot)

    assert snapshot.metadata["forward_detail_count"] == 1
    assert "外层正文" in normalized.messages[0].content
    assert "内层正文" in normalized.messages[0].content
    assert normalized.messages[0].segments[0].extra["forward_depth"] >= 2
    client.close()


def test_napcat_fetch_snapshot_prefers_parse_mult_msg_history_for_forward_detail() -> (
    None
):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        requests.append(f"{request.url.path} {body}")
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"parse_mult_msg":false' in body
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 700,
                                "message_seq": 700,
                                "time": 1710000700,
                                "user_id": 1,
                                "rawMessage": {
                                    "msgId": "700",
                                    "msgSeq": "700",
                                    "elements": [
                                        {
                                            "elementType": 16,
                                            "multiForwardMsgElement": {
                                                "resId": "forward-700",
                                                "fileName": "forward-file",
                                                "xmlContent": "<msg brief='[聊天记录]'></msg>",
                                            },
                                        }
                                    ],
                                },
                                "message": [],
                            }
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"parse_mult_msg":true' in body
        ):
            assert '"message_seq":"700"' in body
            assert '"count":1' in body
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 700,
                                "message_seq": 700,
                                "time": 1710000700,
                                "user_id": 1,
                                "message": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "forward-700",
                                            "content": [
                                                {
                                                    "self_id": 1,
                                                    "user_id": 11,
                                                    "time": 1710000600,
                                                    "message_id": 1,
                                                    "message_seq": 1,
                                                    "sender": {
                                                        "user_id": 11,
                                                        "nickname": "甲",
                                                        "card": "",
                                                    },
                                                    "message": [
                                                        {
                                                            "type": "forward",
                                                            "data": {
                                                                "id": "inner-1",
                                                                "content": [
                                                                    {
                                                                        "self_id": 1,
                                                                        "user_id": 12,
                                                                        "time": 1710000500,
                                                                        "message_id": 2,
                                                                        "message_seq": 2,
                                                                        "sender": {
                                                                            "user_id": 12,
                                                                            "nickname": "乙",
                                                                            "card": "",
                                                                        },
                                                                        "message": [
                                                                            {
                                                                                "type": "text",
                                                                                "data": {
                                                                                    "text": "套娃正文"
                                                                                },
                                                                            }
                                                                        ],
                                                                    }
                                                                ],
                                                            },
                                                        }
                                                    ],
                                                }
                                            ],
                                        },
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        if request.url.path.endswith("/get_forward_msg"):
            raise AssertionError(
                "get_forward_msg should not be called when parse_mult_msg history succeeds"
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=1)
    )
    normalized = ChatExportService().build_snapshot(snapshot)

    assert snapshot.metadata["forward_detail_count"] == 1
    assert "套娃正文" in normalized.messages[0].content
    assert normalized.messages[0].segments[0].extra["forward_depth"] >= 2
    assert sum(1 for item in requests if "/get_friend_msg_history" in item) == 2
    client.close()


def test_napcat_fetch_snapshot_tolerates_string_raw_message_in_private_forward_history() -> (
    None
):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        requests.append(f"{request.url.path} {body}")
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"parse_mult_msg":false' in body
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 700,
                                "message_seq": 700,
                                "time": 1710000700,
                                "user_id": 1,
                                "rawMessage": "legacy-client-string",
                                "raw_message": {
                                    "msgId": "700",
                                    "msgSeq": "700",
                                    "elements": [
                                        {
                                            "elementType": 16,
                                            "multiForwardMsgElement": {
                                                "resId": "forward-700",
                                                "fileName": "forward-file",
                                                "xmlContent": "<msg brief='[聊天记录]'></msg>",
                                            },
                                        }
                                    ],
                                },
                                "message": [],
                            }
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"parse_mult_msg":true' in body
        ):
            assert '"message_seq":"700"' in body
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            "legacy-string-item",
                            {
                                "message_id": 700,
                                "message_seq": 700,
                                "time": 1710000700,
                                "user_id": 1,
                                "rawMessage": "legacy-client-string",
                                "message": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "forward-700",
                                            "content": [
                                                {
                                                    "message": [
                                                        {
                                                            "type": "text",
                                                            "data": {
                                                                "text": "脏payload正文"
                                                            },
                                                        }
                                                    ]
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                        ]
                    },
                },
            )
        if request.url.path.endswith("/get_forward_msg"):
            raise AssertionError(
                "get_forward_msg should not be called when parse_mult_msg history succeeds"
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot(
        ExportRequest(chat_type="private", chat_id="1507833383", limit=1)
    )
    normalized = ChatExportService().build_snapshot(snapshot)

    assert snapshot.metadata["forward_detail_count"] == 1
    assert "脏payload正文" in normalized.messages[0].content
    assert sum(1 for item in requests if "/get_friend_msg_history" in item) == 2
    client.close()


def test_napcat_fetch_snapshot_between_collects_closed_interval_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' not in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                            {
                                "message_id": 500,
                                "message_seq": 500,
                                "time": 1710000500,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m500"}}],
                            },
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 100,
                                "message_seq": 100,
                                "time": 1710000100,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m100"}}],
                            },
                            {
                                "message_id": 200,
                                "message_seq": 200,
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m200"}}],
                            },
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot_between(
        ExportRequest(
            chat_type="private",
            chat_id="1507833383",
            since=datetime.fromtimestamp(1710000200, tz=EXPORT_TIMEZONE),
            until=datetime.fromtimestamp(1710000400, tz=EXPORT_TIMEZONE),
        )
    )

    assert [message["message_seq"] for message in snapshot.messages] == [200, 300, 400]
    client.close()


def test_napcat_fetch_snapshot_between_sorts_reverse_ordered_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' not in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 500,
                                "message_seq": 500,
                                "time": 1710000500,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m500"}}],
                            },
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                            {
                                "message_id": 200,
                                "message_seq": 200,
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m200"}}],
                            },
                            {
                                "message_id": 100,
                                "message_seq": 100,
                                "time": 1710000100,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m100"}}],
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot_between(
        ExportRequest(
            chat_type="private",
            chat_id="1507833383",
            since=datetime.fromtimestamp(1710000200, tz=EXPORT_TIMEZONE),
            until=datetime.fromtimestamp(1710000400, tz=EXPORT_TIMEZONE),
        )
    )

    assert [message["message_seq"] for message in snapshot.messages] == [200, 300, 400]
    client.close()


def test_napcat_fetch_snapshot_tail_between_collects_latest_messages_in_interval() -> (
    None
):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        requests.append(payload)
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"400"' not in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                            {
                                "message_id": 500,
                                "message_seq": 500,
                                "time": 1710000500,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m500"}}],
                            },
                            {
                                "message_id": 600,
                                "message_seq": 600,
                                "time": 1710000600,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m600"}}],
                            },
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"400"' in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 200,
                                "message_seq": 200,
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m200"}}],
                            },
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot_tail_between(
        ExportRequest(
            chat_type="private",
            chat_id="1507833383",
            since=datetime.fromtimestamp(1710000300, tz=EXPORT_TIMEZONE),
            until=datetime.fromtimestamp(1710000550, tz=EXPORT_TIMEZONE),
        ),
        data_count=2,
    )

    assert [message["message_seq"] for message in snapshot.messages] == [400, 500]
    assert snapshot.metadata["requested_data_count"] == 2
    assert snapshot.metadata["interval_mode"] == "closed_tail"
    assert len(requests) >= 1
    client.close()


def test_napcat_fetch_snapshot_tail_collects_latest_messages_across_pages() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        requests.append(payload)
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"400"' not in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                            {
                                "message_id": 500,
                                "message_seq": 500,
                                "time": 1710000500,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m500"}}],
                            },
                            {
                                "message_id": 600,
                                "message_seq": 600,
                                "time": 1710000600,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m600"}}],
                            },
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"400"' in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 100,
                                "message_seq": 100,
                                "time": 1710000100,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m100"}}],
                            },
                            {
                                "message_id": 200,
                                "message_seq": 200,
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m200"}}],
                            },
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_snapshot_tail(
        ExportRequest(
            chat_type="private",
            chat_id="1507833383",
        ),
        data_count=4,
        page_size=3,
    )

    assert [message["message_seq"] for message in snapshot.messages] == [
        200,
        400,
        500,
        600,
    ]
    assert snapshot.metadata["requested_data_count"] == 4
    assert snapshot.metadata["interval_mode"] == "latest_tail"
    assert len(requests) >= 2
    client.close()


def test_napcat_fetch_snapshot_tail_caps_fast_history_page_size_hint() -> None:
    class FakeFastClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def get_history(
            self,
            chat_type: str,
            chat_id: str,
            *,
            message_id: str | None = None,
            count: int = 20,
            reverse_order: bool = False,
        ):
            self.calls.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "count": count,
                    "reverse_order": reverse_order,
                }
            )
            if message_id in {None, "", "0"}:
                return {
                    "messages": [
                        {
                            "message_id": 400,
                            "message_seq": 400,
                            "time": 1710000400,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m400"}}],
                        },
                        {
                            "message_id": 500,
                            "message_seq": 500,
                            "time": 1710000500,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m500"}}],
                        },
                        {
                            "message_id": 600,
                            "message_seq": 600,
                            "time": 1710000600,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m600"}}],
                        },
                    ]
                }
            return {
                "messages": [
                    {
                        "message_id": 100,
                        "message_seq": 100,
                        "time": 1710000100,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m100"}}],
                    },
                    {
                        "message_id": 200,
                        "message_seq": 200,
                        "time": 1710000200,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m200"}}],
                    },
                    {
                        "message_id": 400,
                        "message_seq": 400,
                        "time": 1710000400,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m400"}}],
                    },
                ]
            }

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"http fallback should not be used: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    fast_client = FakeFastClient()
    provider = NapCatHistoryProvider(client, fast_client=fast_client)
    progress_events: list[dict[str, object]] = []

    snapshot = provider.fetch_snapshot_tail(
        ExportRequest(chat_type="private", chat_id="1507833383"),
        data_count=4,
        page_size=500,
        progress_callback=progress_events.append,
    )

    assert [message["message_seq"] for message in snapshot.messages] == [200, 400, 500, 600]
    assert fast_client.calls[0]["count"] == 200
    assert snapshot.metadata["page_size"] == 200
    assert progress_events[0]["page_size"] == 200
    client.close()


def test_napcat_fetch_snapshot_tail_prefers_bulk_fast_history_route() -> None:
    class FakeFastClient:
        def __init__(self) -> None:
            self.bulk_calls: list[dict[str, object]] = []
            self.page_calls: list[dict[str, object]] = []

        def get_history_tail_bulk(
            self,
            chat_type: str,
            chat_id: str,
            *,
            data_count: int,
            page_size: int = 200,
            anchor_message_id: str | None = None,
        ):
            self.bulk_calls.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "data_count": data_count,
                    "page_size": page_size,
                    "anchor_message_id": anchor_message_id,
                }
            )
            return {
                "page_size": page_size,
                "pages_scanned": 3,
                "exhausted": True,
                "messages": [
                    {
                        "message_id": 300,
                        "message_seq": 300,
                        "time": 1710000300,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m300"}}],
                    },
                    {
                        "message_id": 400,
                        "message_seq": 400,
                        "time": 1710000400,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m400"}}],
                    },
                    {
                        "message_id": 500,
                        "message_seq": 500,
                        "time": 1710000500,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m500"}}],
                    },
                ],
            }

        def get_history(self, *args, **kwargs):
            self.page_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError("page-by-page fast history should not be used")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"http fallback should not be used: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    fast_client = FakeFastClient()
    provider = NapCatHistoryProvider(client, fast_client=fast_client)
    progress_events: list[dict[str, object]] = []

    snapshot = provider.fetch_snapshot_tail(
        ExportRequest(chat_type="private", chat_id="1507833383"),
        data_count=3,
        page_size=500,
        progress_callback=progress_events.append,
    )

    assert [message["message_seq"] for message in snapshot.messages] == [300, 400, 500]
    assert snapshot.metadata["source"] == "napcat_fast_history_bulk"
    assert snapshot.metadata["pages_scanned"] == 3
    assert snapshot.metadata["bulk_chunks"] == 1
    assert fast_client.bulk_calls[0]["page_size"] == 200
    assert fast_client.bulk_calls[0]["anchor_message_id"] is None
    assert not fast_client.page_calls
    assert progress_events[0]["history_source"] == "napcat_fast_history_bulk"
    client.close()


def test_napcat_fetch_snapshot_tail_records_bulk_duration(monkeypatch) -> None:
    class FakeFastClient:
        def get_history_tail_bulk(
            self,
            chat_type: str,
            chat_id: str,
            *,
            data_count: int,
            page_size: int = 200,
            anchor_message_id: str | None = None,
        ):
            return {
                "page_size": page_size,
                "pages_scanned": 2,
                "exhausted": True,
                "messages": [
                    {
                        "message_id": 100,
                        "message_seq": 100,
                        "time": 1710000100,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m100"}}],
                    },
                    {
                        "message_id": 200,
                        "message_seq": 200,
                        "time": 1710000200,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m200"}}],
                    },
                ],
            }

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"http fallback should not be used: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client, fast_client=FakeFastClient())
    progress_events: list[dict[str, object]] = []
    ticks = iter([10.0, 10.5, 11.0, 12.5])
    monkeypatch.setattr(provider_module, "perf_counter", lambda: next(ticks))

    snapshot = provider.fetch_snapshot_tail(
        ExportRequest(chat_type="private", chat_id="1507833383"),
        data_count=2,
        page_size=500,
        progress_callback=progress_events.append,
    )

    assert snapshot.metadata["bulk_duration_s"] == 2.5
    assert progress_events[0]["bulk_duration_s"] == 2.5
    assert progress_events[0]["page_duration_s"] == 0.5
    client.close()


def test_napcat_fetch_snapshot_tail_falls_back_when_bulk_fast_history_unavailable() -> None:
    class FakeFastClient:
        def __init__(self) -> None:
            self.bulk_calls = 0
            self.page_calls: list[dict[str, object]] = []

        def get_history_tail_bulk(
            self,
            chat_type: str,
            chat_id: str,
            *,
            data_count: int,
            page_size: int = 200,
            anchor_message_id: str | None = None,
        ):
            self.bulk_calls += 1
            raise NapCatFastHistoryUnavailable("route unavailable")

        def get_history(
            self,
            chat_type: str,
            chat_id: str,
            *,
            message_id: str | None = None,
            count: int = 20,
            reverse_order: bool = False,
        ):
            self.page_calls.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "count": count,
                    "reverse_order": reverse_order,
                }
            )
            if message_id in {None, "", "0"}:
                return {
                    "messages": [
                        {
                            "message_id": 400,
                            "message_seq": 400,
                            "time": 1710000400,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m400"}}],
                        },
                        {
                            "message_id": 500,
                            "message_seq": 500,
                            "time": 1710000500,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m500"}}],
                        },
                        {
                            "message_id": 600,
                            "message_seq": 600,
                            "time": 1710000600,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m600"}}],
                        },
                    ]
                }
            return {
                "messages": [
                    {
                        "message_id": 100,
                        "message_seq": 100,
                        "time": 1710000100,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m100"}}],
                    },
                    {
                        "message_id": 200,
                        "message_seq": 200,
                        "time": 1710000200,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m200"}}],
                    },
                    {
                        "message_id": 400,
                        "message_seq": 400,
                        "time": 1710000400,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m400"}}],
                    },
                ]
            }

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"http fallback should not be used: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    fast_client = FakeFastClient()
    provider = NapCatHistoryProvider(client, fast_client=fast_client)

    snapshot = provider.fetch_snapshot_tail(
        ExportRequest(chat_type="private", chat_id="1507833383"),
        data_count=4,
        page_size=500,
    )

    assert [message["message_seq"] for message in snapshot.messages] == [200, 400, 500, 600]
    assert snapshot.metadata["source"] == "napcat_fast_history"
    assert fast_client.bulk_calls == 1
    assert len(fast_client.page_calls) >= 2
    client.close()


def test_napcat_fetch_snapshot_tail_bulk_keeps_page_level_forward_hydration() -> None:
    class FakeFastClient:
        def get_history_tail_bulk(
            self,
            chat_type: str,
            chat_id: str,
            *,
            data_count: int,
            page_size: int = 200,
            anchor_message_id: str | None = None,
        ):
            return {
                "page_size": page_size,
                "pages_scanned": 2,
                "exhausted": True,
                "messages": [
                    {
                        "message_id": "m1",
                        "message_seq": "100",
                        "time": 1710000100,
                        "user_id": 1,
                        "message": [],
                        "rawMessage": {
                            "msgId": "m1",
                            "msgSeq": "100",
                            "elements": [
                                {
                                    "elementType": 16,
                                    "multiForwardMsgElement": {
                                        "resId": "forward-100",
                                        "fileName": "",
                                        "xmlContent": "",
                                    },
                                }
                            ],
                        },
                    },
                    {
                        "message_id": "m2",
                        "message_seq": "200",
                        "time": 1710000200,
                        "user_id": 1,
                        "message": [],
                        "rawMessage": {
                            "msgId": "m2",
                            "msgSeq": "200",
                            "elements": [
                                {
                                    "elementType": 16,
                                    "multiForwardMsgElement": {
                                        "resId": "forward-200",
                                        "fileName": "",
                                        "xmlContent": "",
                                    },
                                }
                            ],
                        },
                    },
                ],
            }

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        requests.append(f"{request.url.path} {body}")
        if request.url.path.endswith("/get_group_msg_history") and '"message_seq":"100"' not in body:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": "m1",
                                "message_seq": "100",
                                "time": 1710000100,
                                "user_id": 1,
                                "message": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "m1",
                                            "content": [
                                                {
                                                    "message": [
                                                        {
                                                            "type": "text",
                                                            "data": {"text": "forward one"},
                                                        }
                                                    ]
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "message_id": "m2",
                                "message_seq": "200",
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [
                                    {
                                        "type": "forward",
                                        "data": {
                                            "id": "m2",
                                            "content": [
                                                {
                                                    "message": [
                                                        {
                                                            "type": "text",
                                                            "data": {"text": "forward two"},
                                                        }
                                                    ]
                                                }
                                            ],
                                        },
                                    }
                                ],
                            },
                        ]
                    },
                },
            )
        if request.url.path.endswith("/get_group_msg_history") and '"message_seq":"100"' in body:
            return httpx.Response(200, json={"status": "ok", "data": {"messages": []}})
        raise AssertionError(f"unexpected path: {request.url.path} {body}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client, fast_client=FakeFastClient())

    snapshot = provider.fetch_snapshot_tail(
        ExportRequest(chat_type="group", chat_id="922065597"),
        data_count=2,
        page_size=2,
    )

    assert snapshot.metadata["source"] == "napcat_fast_history_bulk"
    assert snapshot.metadata["forward_detail_count"] == 2
    assert provider._message_has_resolved_forward_content(snapshot.messages[0]) is True
    assert provider._message_has_resolved_forward_content(snapshot.messages[1]) is True
    assert any('parse_mult_msg":true' in item for item in requests)
    client.close()


def test_napcat_finalize_snapshot_skips_single_message_history_retry_for_fast_history() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        requests.append(f"{request.url.path} {body}")
        if request.url.path.endswith("/get_group_msg_history"):
            raise AssertionError("single-message parse_mult retry should be skipped for fast history snapshots")
        if request.url.path.endswith("/get_forward_msg"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message": [
                                    {
                                        "type": "text",
                                        "data": {"text": "resolved by fallback"},
                                    }
                                ]
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path} {body}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)

    snapshot = SourceChatSnapshot(
        chat_type="group",
        chat_id="922065597",
        chat_name=None,
        exported_at=datetime.now(EXPORT_TIMEZONE),
        metadata={"source": "napcat_fast_history"},
        messages=[
            {
                "message_id": "m-forward-1",
                "message_seq": "100",
                "time": 1710000100,
                "user_id": 1,
                "message": [
                    {
                        "type": "forward",
                        "data": {
                            "id": "forward-100",
                        },
                    }
                ],
            }
        ],
    )

    finalized = provider._finalize_snapshot(snapshot)

    assert finalized.metadata["forward_detail_count"] == 1
    assert finalized.messages[0]["message"][0]["data"]["content"][0]["message"][0]["data"]["text"] == "resolved by fallback"
    assert len([item for item in requests if item.startswith("/get_forward_msg")]) == 1
    client.close()


def test_napcat_finalize_snapshot_breaks_forward_fallback_after_known_unavailable_error() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        requests.append(f"{request.url.path} {body}")
        if request.url.path.endswith("/get_group_msg_history"):
            raise AssertionError("single-message parse_mult retry should be skipped for fast history snapshots")
        if request.url.path.endswith("/get_forward_msg"):
            return httpx.Response(
                200,
                json={
                    "status": "failed",
                    "message": "protocolFallbackLogic: 找不到相关的聊天记录",
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path} {body}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)

    snapshot = SourceChatSnapshot(
        chat_type="group",
        chat_id="922065597",
        chat_name=None,
        exported_at=datetime.now(EXPORT_TIMEZONE),
        metadata={"source": "napcat_fast_history"},
        messages=[
            {
                "message_id": "m-forward-1",
                "message_seq": "100",
                "time": 1710000100,
                "user_id": 1,
                "message": [{"type": "forward", "data": {"id": "forward-100"}}],
            },
            {
                "message_id": "m-forward-2",
                "message_seq": "200",
                "time": 1710000200,
                "user_id": 1,
                "message": [{"type": "forward", "data": {"id": "forward-200"}}],
            },
        ],
    )

    finalized = provider._finalize_snapshot(snapshot)

    assert "forward_detail_count" not in finalized.metadata
    assert len([item for item in requests if item.startswith("/get_forward_msg")]) == 1
    client.close()


def test_napcat_fetch_snapshot_tail_splits_large_bulk_requests_into_chunks() -> None:
    class FakeFastClient:
        def __init__(self) -> None:
            self.bulk_calls: list[dict[str, object]] = []
            self.page_calls: list[dict[str, object]] = []

        def get_history_tail_bulk(
            self,
            chat_type: str,
            chat_id: str,
            *,
            data_count: int,
            page_size: int = 200,
            anchor_message_id: str | None = None,
        ):
            self.bulk_calls.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "data_count": data_count,
                    "page_size": page_size,
                    "anchor_message_id": anchor_message_id,
                }
            )
            if anchor_message_id in {None, ""}:
                return {
                    "page_size": page_size,
                    "pages_scanned": 3,
                    "next_anchor": "300",
                    "exhausted": False,
                    "messages": [
                        {
                            "message_id": 300,
                            "message_seq": 300,
                            "time": 1710000300,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m300"}}],
                        },
                        {
                            "message_id": 400,
                            "message_seq": 400,
                            "time": 1710000400,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m400"}}],
                        },
                        {
                            "message_id": 500,
                            "message_seq": 500,
                            "time": 1710000500,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m500"}}],
                        },
                    ],
                }
            assert anchor_message_id == "300"
            return {
                "page_size": page_size,
                "pages_scanned": 2,
                "next_anchor": "100",
                "exhausted": True,
                "messages": [
                    {
                        "message_id": 100,
                        "message_seq": 100,
                        "time": 1710000100,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m100"}}],
                    },
                    {
                        "message_id": 200,
                        "message_seq": 200,
                        "time": 1710000200,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m200"}}],
                    },
                ],
            }

        def get_history(self, *args, **kwargs):
            self.page_calls.append({"args": args, "kwargs": kwargs})
            raise AssertionError("page-by-page fast history should not be used")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"http fallback should not be used: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    fast_client = FakeFastClient()
    provider = NapCatHistoryProvider(client, fast_client=fast_client)

    original_limit = provider_module.FAST_HISTORY_BULK_SAFE_DATA_COUNT
    provider_module.FAST_HISTORY_BULK_SAFE_DATA_COUNT = 3
    try:
        snapshot = provider.fetch_snapshot_tail(
            ExportRequest(chat_type="private", chat_id="1507833383"),
            data_count=5,
            page_size=500,
        )
    finally:
        provider_module.FAST_HISTORY_BULK_SAFE_DATA_COUNT = original_limit

    assert [message["message_seq"] for message in snapshot.messages] == [
        100,
        200,
        300,
        400,
        500,
    ]
    assert snapshot.metadata["source"] == "napcat_fast_history_bulk"
    assert snapshot.metadata["bulk_chunks"] == 2
    assert snapshot.metadata["pages_scanned"] == 5
    assert fast_client.bulk_calls == [
        {
            "chat_type": "private",
            "chat_id": "1507833383",
            "data_count": 3,
            "page_size": 200,
            "anchor_message_id": None,
        },
        {
            "chat_type": "private",
            "chat_id": "1507833383",
            "data_count": 2,
            "page_size": 200,
            "anchor_message_id": "300",
        },
    ]
    assert not fast_client.page_calls
    client.close()


def test_napcat_fetch_snapshot_tail_bulk_degrades_to_page_history_after_partial_success() -> None:
    class FakeFastClient:
        def __init__(self) -> None:
            self.bulk_calls: list[dict[str, object]] = []
            self.page_calls: list[dict[str, object]] = []

        def get_history_tail_bulk(
            self,
            chat_type: str,
            chat_id: str,
            *,
            data_count: int,
            page_size: int = 200,
            anchor_message_id: str | None = None,
        ):
            self.bulk_calls.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "data_count": data_count,
                    "page_size": page_size,
                    "anchor_message_id": anchor_message_id,
                }
            )
            if anchor_message_id in {None, ""}:
                return {
                    "page_size": page_size,
                    "pages_scanned": 3,
                    "next_anchor": "300",
                    "exhausted": False,
                    "messages": [
                        {
                            "message_id": 300,
                            "message_seq": 300,
                            "time": 1710000300,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m300"}}],
                        },
                        {
                            "message_id": 400,
                            "message_seq": 400,
                            "time": 1710000400,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m400"}}],
                        },
                        {
                            "message_id": 500,
                            "message_seq": 500,
                            "time": 1710000500,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m500"}}],
                        },
                    ],
                }
            raise NapCatFastHistoryUnavailable("chunk route unavailable")

        def get_history(
            self,
            chat_type: str,
            chat_id: str,
            *,
            message_id: str | None = None,
            count: int = 20,
            reverse_order: bool = False,
        ):
            self.page_calls.append(
                {
                    "chat_type": chat_type,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "count": count,
                    "reverse_order": reverse_order,
                }
            )
            assert message_id == "300"
            return {
                "messages": [
                    {
                        "message_id": 100,
                        "message_seq": 100,
                        "time": 1710000100,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m100"}}],
                    },
                    {
                        "message_id": 200,
                        "message_seq": 200,
                        "time": 1710000200,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m200"}}],
                    },
                    {
                        "message_id": 300,
                        "message_seq": 300,
                        "time": 1710000300,
                        "user_id": 1,
                        "message": [{"type": "text", "data": {"text": "m300"}}],
                    },
                ]
            }

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"http fallback should not be used: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    fast_client = FakeFastClient()
    provider = NapCatHistoryProvider(client, fast_client=fast_client)

    original_limit = provider_module.FAST_HISTORY_BULK_SAFE_DATA_COUNT
    provider_module.FAST_HISTORY_BULK_SAFE_DATA_COUNT = 3
    try:
        snapshot = provider.fetch_snapshot_tail(
            ExportRequest(chat_type="private", chat_id="1507833383"),
            data_count=5,
            page_size=500,
        )
    finally:
        provider_module.FAST_HISTORY_BULK_SAFE_DATA_COUNT = original_limit

    assert [message["message_seq"] for message in snapshot.messages] == [
        100,
        200,
        300,
        400,
        500,
    ]
    assert snapshot.metadata["source"] == "napcat_fast_history_bulk+napcat_fast_history"
    assert snapshot.metadata["bulk_partial_fallback"] is True
    assert snapshot.metadata["bulk_chunks"] == 1
    assert snapshot.metadata["pages_scanned"] == 4
    assert len(fast_client.bulk_calls) == 2
    assert len(fast_client.page_calls) == 1
    client.close()


def test_napcat_fetch_full_snapshot_collects_all_pages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' not in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                            {
                                "message_id": 500,
                                "message_seq": 500,
                                "time": 1710000500,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m500"}}],
                            },
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 100,
                                "message_seq": 100,
                                "time": 1710000100,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m100"}}],
                            },
                            {
                                "message_id": 200,
                                "message_seq": 200,
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m200"}}],
                            },
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_full_snapshot(
        ExportRequest(
            chat_type="private",
            chat_id="1507833383",
        )
    )

    assert [message["message_seq"] for message in snapshot.messages] == [
        100,
        200,
        300,
        400,
        500,
    ]
    assert snapshot.metadata["full_history"] is True
    client.close()


def test_napcat_get_history_bounds_sorts_reverse_ordered_latest_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.read().decode("utf-8")
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"100"' in payload
        ):
            return httpx.Response(200, json={"status": "ok", "data": {"messages": []}})
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' not in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 500,
                                "message_seq": 500,
                                "time": 1710000500,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m500"}}],
                            },
                            {
                                "message_id": 400,
                                "message_seq": 400,
                                "time": 1710000400,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m400"}}],
                            },
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                        ]
                    },
                },
            )
        if (
            request.url.path.endswith("/get_friend_msg_history")
            and '"message_seq":"300"' in payload
        ):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": {
                        "messages": [
                            {
                                "message_id": 300,
                                "message_seq": 300,
                                "time": 1710000300,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m300"}}],
                            },
                            {
                                "message_id": 200,
                                "message_seq": 200,
                                "time": 1710000200,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m200"}}],
                            },
                            {
                                "message_id": 100,
                                "message_seq": 100,
                                "time": 1710000100,
                                "user_id": 1,
                                "message": [{"type": "text", "data": {"text": "m100"}}],
                            },
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    bounds = provider.get_history_bounds(
        ExportRequest(chat_type="private", chat_id="1507833383"),
    )

    assert bounds.earliest_content_at == datetime.fromtimestamp(
        1710000100, tz=EXPORT_TIMEZONE
    )
    assert bounds.final_content_at == datetime.fromtimestamp(
        1710000500, tz=EXPORT_TIMEZONE
    )
    client.close()


def test_napcat_fetch_full_snapshot_retries_with_smaller_page_after_timeout() -> None:
    requests: list[str] = []
    first_attempt = True
    progress_events: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal first_attempt
        payload = request.read().decode("utf-8")
        requests.append(payload)
        if '"message_seq":"100"' in payload:
            return httpx.Response(200, json={"status": "ok", "data": {"messages": []}})
        if first_attempt:
            first_attempt = False
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "messages": [
                        {
                            "message_id": 100,
                            "message_seq": 100,
                            "time": 1710000100,
                            "user_id": 1,
                            "message": [{"type": "text", "data": {"text": "m100"}}],
                        }
                    ]
                },
            },
        )

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    provider = NapCatHistoryProvider(client)
    snapshot = provider.fetch_full_snapshot(
        ExportRequest(
            chat_type="private",
            chat_id="1507833383",
        ),
        page_size=200,
        progress_callback=progress_events.append,
    )

    assert len(snapshot.messages) == 1
    assert '"count":200' in requests[0]
    assert '"count":100' in requests[1]
    assert any(event.get("phase") == "page_retry" for event in progress_events)
    client.close()
