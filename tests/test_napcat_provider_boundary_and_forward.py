from __future__ import annotations

from datetime import datetime

from qq_data_core.models import EXPORT_TIMEZONE, ExportRequest, SourceChatSnapshot
from qq_data_integrations.napcat.http_client import NapCatApiError
from qq_data_integrations.napcat.provider import NapCatHistoryProvider


class _DummyClient:
    def get_forward_msg(self, message_id: str):
        raise NotImplementedError


def _request() -> ExportRequest:
    return ExportRequest(chat_type="group", chat_id="922065597", chat_name="test", limit=3)


def _message(message_id: str, seq: str) -> dict[str, object]:
    second = int(seq) % 60
    return {
        "message_id": message_id,
        "message_seq": seq,
        "time": 1750000000 + int(seq),
        "timestamp_iso": f"2025-09-02T00:00:{second:02d}+08:00",
    }


def _snapshot(messages: list[dict[str, object]], *, source: str = "napcat_fast_history") -> SourceChatSnapshot:
    return SourceChatSnapshot(
        chat_type="group",
        chat_id="922065597",
        chat_name="test",
        exported_at=datetime.now(EXPORT_TIMEZONE),
        metadata={"source": source},
        messages=messages,
    )


def test_collect_fast_history_tail_bulk_bridges_duplicate_anchor_boundary() -> None:
    provider = NapCatHistoryProvider(_DummyClient(), fast_client=object())
    payloads = iter(
        [
            {
                "messages": [_message("m1", "1"), _message("m2", "2")],
                "pages_scanned": 1,
                "next_anchor": "anchor-2",
                "page_size": 200,
                "exhausted": False,
            },
            {
                "messages": [_message("m2", "2")],
                "pages_scanned": 1,
                "next_anchor": "anchor-2",
                "page_size": 200,
                "exhausted": False,
            },
        ]
    )

    def fake_fetch_fast_history_tail_bulk(*args, **kwargs):
        try:
            return next(payloads)
        except StopIteration:
            return None

    def fake_fetch_history_page(*args, **kwargs):
        return (
            _snapshot([_message("m3", "3")]),
            {
                "history_source": "napcat_fast_history",
                "page_duration_s": 0.01,
                "page_size": 1,
                "page_message_count": 1,
                "retry_count": 0,
            },
        )

    provider._fetch_fast_history_tail_bulk = fake_fetch_fast_history_tail_bulk  # type: ignore[method-assign]
    provider._fetch_history_page = fake_fetch_history_page  # type: ignore[method-assign]

    state = provider._collect_fast_history_tail_bulk(
        _request(),
        data_count=3,
        page_size=200,
        progress_callback=None,
    )

    assert state is not None
    assert state["completed"] is True
    assert state["partial_fallback"] is False
    assert state["history_source"] == "napcat_fast_history_bulk+napcat_fast_history"
    assert [item["message_id"] for item in state["messages"]] == ["m1", "m2", "m3"]


def test_collect_fast_history_tail_bulk_boundary_bridge_keeps_fallback_when_no_progress() -> None:
    provider = NapCatHistoryProvider(_DummyClient(), fast_client=object())
    payloads = iter(
        [
            {
                "messages": [_message("m1", "1"), _message("m2", "2")],
                "pages_scanned": 1,
                "next_anchor": "anchor-2",
                "page_size": 200,
                "exhausted": False,
            },
            {
                "messages": [_message("m2", "2")],
                "pages_scanned": 1,
                "next_anchor": "anchor-2",
                "page_size": 200,
                "exhausted": False,
            },
        ]
    )

    def fake_fetch_fast_history_tail_bulk(*args, **kwargs):
        try:
            return next(payloads)
        except StopIteration:
            return None

    def fake_fetch_history_page(*args, **kwargs):
        return (
            _snapshot([_message("m2", "2")]),
            {
                "history_source": "napcat_fast_history",
                "page_duration_s": 0.01,
                "page_size": 1,
                "page_message_count": 1,
                "retry_count": 0,
            },
        )

    provider._fetch_fast_history_tail_bulk = fake_fetch_fast_history_tail_bulk  # type: ignore[method-assign]
    provider._fetch_history_page = fake_fetch_history_page  # type: ignore[method-assign]

    state = provider._collect_fast_history_tail_bulk(
        _request(),
        data_count=3,
        page_size=200,
        progress_callback=None,
    )

    assert state is not None
    assert state["completed"] is False
    assert state["partial_fallback"] is True
    assert [item["message_id"] for item in state["messages"]] == ["m1", "m2"]


def test_enrich_forward_details_uses_history_as_last_chance_after_get_forward_msg_failure() -> None:
    class _ForwardFailClient:
        def get_forward_msg(self, message_id: str):
            raise NapCatApiError("找不到相关的聊天记录")

    provider = NapCatHistoryProvider(_ForwardFailClient())
    target_message = {
        "message_id": "m-forward",
        "message_seq": "23388",
        "message": [
            {
                "type": "forward",
                "data": {"id": "fwd-1"},
                "extra": {"forward_messages": [], "detailed_text": None},
            }
        ],
    }

    def fake_hydrate_forward_message_via_history(message: dict[str, object], *, chat_type: str, chat_id: str):
        message["message"][0]["extra"]["forward_messages"] = [{"message_id": "nested"}]  # type: ignore[index]
        message["message"][0]["extra"]["detailed_text"] = "nested text"  # type: ignore[index]
        return True, False

    provider._hydrate_forward_message_via_history = fake_hydrate_forward_message_via_history  # type: ignore[method-assign]

    enriched, unavailable = provider._enrich_forward_details(
        [target_message],
        chat_type="group",
        chat_id="922065597",
        skip_history_retry=True,
        progress_callback=None,
    )

    assert enriched == 1
    assert unavailable == 0
    assert target_message["message"][0]["extra"]["forward_messages"] == [{"message_id": "nested"}]  # type: ignore[index]


def test_enrich_forward_details_marks_unavailable_when_forward_and_history_both_fail() -> None:
    class _ForwardFailClient:
        def get_forward_msg(self, message_id: str):
            raise NapCatApiError("找不到相关的聊天记录")

    provider = NapCatHistoryProvider(_ForwardFailClient())
    target_message = {
        "message_id": "m-forward",
        "message_seq": "23388",
        "message": [
            {
                "type": "forward",
                "data": {"id": "fwd-1"},
                "extra": {"forward_messages": [], "detailed_text": None},
            }
        ],
    }

    def fake_hydrate_forward_message_via_history(message: dict[str, object], *, chat_type: str, chat_id: str):
        return False, True

    provider._hydrate_forward_message_via_history = fake_hydrate_forward_message_via_history  # type: ignore[method-assign]

    enriched, unavailable = provider._enrich_forward_details(
        [target_message],
        chat_type="group",
        chat_id="922065597",
        skip_history_retry=True,
        progress_callback=None,
    )

    assert enriched == 0
    assert unavailable == 1
    assert str(
        target_message["message"][0]["data"]["_qq_data_forward_unavailable_reason"]  # type: ignore[index]
    )


def test_message_has_resolved_forward_content_requires_nested_forwards_to_be_resolved() -> None:
    provider = NapCatHistoryProvider(_DummyClient())
    message = {
        "message": [
            {
                "type": "forward",
                "data": {
                    "id": "outer-fwd",
                    "content": [
                        {
                            "message": [
                                {
                                    "type": "forward",
                                    "data": {
                                        "id": "inner-fwd",
                                        "content": [],
                                        "preview_text": "只有预览，没有展开",
                                    },
                                }
                            ]
                        }
                    ],
                },
            }
        ]
    }

    assert provider._message_has_resolved_forward_content(message) is False


def test_iter_forward_targets_discovers_unresolved_nested_forwards_inside_resolved_outer_forward() -> None:
    provider = NapCatHistoryProvider(_DummyClient())
    message = {
        "message_id": "outer-msg",
        "message_seq": "101",
        "message": [
            {
                "type": "forward",
                "data": {
                    "id": "outer-fwd",
                    "content": [
                        {
                            "message": [
                                {
                                    "type": "text",
                                    "data": {"text": "甲：先看外层"},
                                },
                                {
                                    "type": "forward",
                                    "data": {
                                        "id": "inner-fwd",
                                        "preview_text": "内层只有 preview",
                                        "content": [],
                                    },
                                },
                            ]
                        }
                    ],
                },
            }
        ],
    }

    targets = list(provider._iter_forward_targets([message]))
    target_ids = [item["forward_id"] for item in targets]

    assert "inner-fwd" in target_ids
