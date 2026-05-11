from __future__ import annotations

from pathlib import Path

from qq_data_core import (
    ChatExportService,
    NormalizedMessage,
    NormalizedSegment,
    render_debug_content,
    render_watch_line,
)
from qq_data_integrations import FixtureSnapshotLoader


def test_render_debug_content_hides_heavy_payloads() -> None:
    loader = FixtureSnapshotLoader()
    service = ChatExportService()
    snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
    normalized = service.build_snapshot(snapshot)

    assert render_debug_content(normalized.messages[1]) == "[image]"
    assert render_debug_content(normalized.messages[4]) == "[meme or emoji]"

    line = render_watch_line(normalized.messages[5])
    assert "private=1507833383" in line
    assert "sender=1585729597" in line
    assert "content=[speech audio]" in line


def test_render_debug_content_handles_forward_system_and_share() -> None:
    system_message = NormalizedMessage(
        chat_type="group",
        chat_id="10001",
        group_id="10001",
        sender_id="0",
        sender_name="系统",
        timestamp_ms=1736553827000,
        timestamp_iso="2026-01-11T10:43:47+08:00",
        content="Alice踢了踢Bob的HashMap",
        text_content="Alice踢了踢Bob的HashMap",
        segments=[NormalizedSegment(type="system", text="Alice踢了踢Bob的HashMap")],
    )
    forward_message = NormalizedMessage(
        chat_type="group",
        chat_id="10001",
        group_id="10001",
        sender_id="1",
        sender_name="甲",
        timestamp_ms=1736553828000,
        timestamp_iso="2026-01-11T10:43:48+08:00",
        content="[forward message] 甲：你好",
        text_content="甲：你好",
        segments=[
            NormalizedSegment(
                type="forward",
                token="[forward message]",
                summary="聊天记录",
                extra={"preview_text": "甲：你好"},
            )
        ],
    )
    share_message = NormalizedMessage(
        chat_type="group",
        chat_id="10001",
        group_id="10001",
        sender_id="2",
        sender_name="乙",
        timestamp_ms=1736553829000,
        timestamp_iso="2026-01-11T10:43:49+08:00",
        content="[share:你的好友送你一张免单卡] 你的好友送你一张免单卡 千问请客，1分钱喝奶茶 通义",
        text_content="你的好友送你一张免单卡 千问请客，1分钱喝奶茶 通义",
        segments=[
            NormalizedSegment(
                type="share",
                token="[share:你的好友送你一张免单卡]",
                summary="你的好友送你一张免单卡",
                extra={"desc": "千问请客，1分钱喝奶茶", "tag": "通义"},
            )
        ],
    )

    assert render_debug_content(system_message) == "Alice踢了踢Bob的HashMap"
    assert render_debug_content(forward_message) == "[forward message]"
    assert render_debug_content(share_message) == "[share:你的好友送你一张免单卡]"
