from __future__ import annotations

from pathlib import Path

from qq_data_core import ChatExportService
from qq_data_core.exporters import render_txt
from qq_data_core.models import NormalizedMessage, NormalizedSegment, NormalizedSnapshot
from qq_data_integrations import FixtureSnapshotLoader


def test_render_txt_contains_required_fields() -> None:
    loader = FixtureSnapshotLoader()
    service = ChatExportService()
    snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
    normalized = service.build_snapshot(snapshot)

    rendered = render_txt(normalized)

    assert "聊天名称: 示例私聊" in rendered
    assert "好友ID: 1507833383" in rendered
    assert "发送者ID: 1507833383" in rendered
    assert "[图片: demo_image.JPG]" in rendered
    assert "  - image: demo_image.JPG" in rendered
    assert "[文件: report.png]" in rendered
    assert "  - file: report.png" in rendered
    assert "[语音]" in rendered
    assert "  - audio: VOICE.amr" in rendered


def test_render_txt_handles_forward_system_and_share_segments() -> None:
    snapshot = NormalizedSnapshot(
        chat_type="group",
        chat_id="10001",
        chat_name="分析测试群",
        messages=[
            NormalizedMessage(
                chat_type="group",
                chat_id="10001",
                group_id="10001",
                sender_id="1",
                sender_name="甲",
                timestamp_ms=1736553827000,
                timestamp_iso="2026-01-11T10:43:47+08:00",
                content="[forward message] 聊天记录 | 甲: 你好",
                text_content="聊天记录 甲: 你好",
                segments=[
                    NormalizedSegment(
                        type="forward",
                        token="[forward message]",
                        summary="聊天记录",
                        extra={"preview_text": "聊天记录 | 甲: 你好"},
                    )
                ],
            ),
            NormalizedMessage(
                chat_type="group",
                chat_id="10001",
                group_id="10001",
                sender_id="0",
                sender_name="系统",
                timestamp_ms=1736553828000,
                timestamp_iso="2026-01-11T10:43:48+08:00",
                content="Alice踢了踢Bob的HashMap",
                text_content="Alice踢了踢Bob的HashMap",
                segments=[NormalizedSegment(type="system", text="Alice踢了踢Bob的HashMap")],
            ),
            NormalizedMessage(
                chat_type="group",
                chat_id="10001",
                group_id="10001",
                sender_id="2",
                sender_name="乙",
                timestamp_ms=1736553829000,
                timestamp_iso="2026-01-11T10:43:49+08:00",
                content="[share:你的好友送你一张免单卡] 你的好友送你一张免单卡 千问请客，1分钱喝奶茶",
                text_content="你的好友送你一张免单卡 千问请客，1分钱喝奶茶",
                segments=[
                    NormalizedSegment(
                        type="share",
                        token="[share:你的好友送你一张免单卡]",
                        summary="你的好友送你一张免单卡",
                        extra={"desc": "千问请客，1分钱喝奶茶"},
                    )
                ],
            ),
        ],
    )

    rendered = render_txt(snapshot)

    assert "[转发聊天记录: 聊天记录 | 甲: 你好]" in rendered
    assert "Alice踢了踢Bob的HashMap" in rendered
    assert "[分享: 你的好友送你一张免单卡] 千问请客，1分钱喝奶茶" in rendered
