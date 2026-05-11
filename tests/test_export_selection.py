from __future__ import annotations

from pathlib import Path

from qq_data_core.export_selection import build_export_content_summary
from qq_data_core.models import ExportBundleResult, MaterializedAsset, NormalizedMessage, NormalizedSegment, NormalizedSnapshot


def test_build_export_content_summary_counts_nested_forward_assets_in_expected_totals() -> None:
    snapshot = NormalizedSnapshot(
        chat_type="private",
        chat_id="1507833383",
        chat_name="friend",
        messages=[
            NormalizedMessage(
                chat_type="private",
                chat_id="1507833383",
                peer_id="1507833383",
                sender_id="1507833383",
                sender_name="friend",
                message_id="top-image",
                message_seq="1",
                timestamp_ms=1,
                timestamp_iso="2026-03-15T02:03:03+08:00",
                content="[image:top.png]",
                text_content="",
                image_file_names=["top.png"],
                segments=[
                    NormalizedSegment(type="image", token="[image:top.png]", file_name="top.png"),
                ],
            ),
            NormalizedMessage(
                chat_type="private",
                chat_id="1507833383",
                peer_id="1507833383",
                sender_id="1507833383",
                sender_name="friend",
                message_id="forward-parent",
                message_seq="2",
                timestamp_ms=2,
                timestamp_iso="2026-03-15T02:03:04+08:00",
                content="[forward message]",
                text_content="[forward message]",
                segments=[
                    NormalizedSegment(
                        type="forward",
                        token="[forward message]",
                        summary="聊天记录",
                        extra={
                            "forward_messages": [
                                {
                                    "sender_id": "111",
                                    "sender_name": "甲",
                                    "content": "[image:a.png]",
                                    "segments": [
                                        {"type": "image", "file_name": "a.png", "extra": {}},
                                    ],
                                },
                                {
                                    "sender_id": "112",
                                    "sender_name": "乙",
                                    "content": "[image:b.png]",
                                    "segments": [
                                        {"type": "image", "file_name": "b.png", "extra": {}},
                                    ],
                                },
                                {
                                    "sender_id": "113",
                                    "sender_name": "丙",
                                    "content": "[image:c.png]",
                                    "segments": [
                                        {"type": "image", "file_name": "c.png", "extra": {}},
                                    ],
                                },
                            ]
                        },
                    )
                ],
            ),
            NormalizedMessage(
                chat_type="private",
                chat_id="1507833383",
                peer_id="1507833383",
                sender_id="3956020260",
                sender_name="wiki",
                message_id="nested-forward-parent",
                message_seq="3",
                timestamp_ms=3,
                timestamp_iso="2026-03-15T02:03:05+08:00",
                content="[forward message]",
                text_content="[forward message]",
                segments=[
                    NormalizedSegment(
                        type="forward",
                        token="[forward message]",
                        summary="聊天记录",
                        extra={
                            "forward_messages": [
                                {
                                    "sender_id": "114",
                                    "sender_name": "丁",
                                    "content": "[forward message]",
                                    "segments": [
                                        {
                                            "type": "forward",
                                            "extra": {
                                                "forward_messages": [
                                                    {
                                                        "sender_id": "211",
                                                        "sender_name": "内层甲",
                                                        "content": "[image:a.png]",
                                                        "segments": [
                                                            {"type": "image", "file_name": "a.png", "extra": {}},
                                                        ],
                                                    },
                                                    {
                                                        "sender_id": "212",
                                                        "sender_name": "内层乙",
                                                        "content": "[image:b.png]",
                                                        "segments": [
                                                            {"type": "image", "file_name": "b.png", "extra": {}},
                                                        ],
                                                    },
                                                    {
                                                        "sender_id": "213",
                                                        "sender_name": "内层丙",
                                                        "content": "[image:c.png]",
                                                        "segments": [
                                                            {"type": "image", "file_name": "c.png", "extra": {}},
                                                        ],
                                                    },
                                                ]
                                            },
                                        }
                                    ],
                                }
                            ]
                        },
                    )
                ],
            ),
            NormalizedMessage(
                chat_type="private",
                chat_id="1507833383",
                peer_id="1507833383",
                sender_id="3956020260",
                sender_name="wiki",
                message_id="speech-parent",
                message_seq="4",
                timestamp_ms=4,
                timestamp_iso="2026-03-15T02:03:06+08:00",
                content="[speech audio]",
                text_content="",
                segments=[
                    NormalizedSegment(type="speech", token="[speech audio]", file_name="voice.amr"),
                ],
            ),
        ],
    )

    bundle = ExportBundleResult(
        data_path=Path("friend.jsonl"),
        manifest_path=Path("friend.manifest.json"),
        assets_dir=Path("friend_assets"),
        record_count=5,
        copied_asset_count=5,
        reused_asset_count=3,
        missing_asset_count=0,
        assets=[
            MaterializedAsset(
                message_id="top-image",
                message_seq="1",
                sender_id="1507833383",
                timestamp_iso="2026-03-15T02:03:03+08:00",
                asset_type="image",
                file_name="top.png",
                status="copied",
            ),
            MaterializedAsset(
                message_id="forward-parent",
                message_seq="2",
                sender_id="1507833383",
                timestamp_iso="2026-03-15T02:03:04+08:00",
                asset_type="image",
                file_name="a.png",
                status="copied",
            ),
            MaterializedAsset(
                message_id="forward-parent",
                message_seq="2",
                sender_id="1507833383",
                timestamp_iso="2026-03-15T02:03:04+08:00",
                asset_type="image",
                file_name="b.png",
                status="copied",
            ),
            MaterializedAsset(
                message_id="forward-parent",
                message_seq="2",
                sender_id="1507833383",
                timestamp_iso="2026-03-15T02:03:04+08:00",
                asset_type="image",
                file_name="c.png",
                status="copied",
            ),
            MaterializedAsset(
                message_id="nested-forward-parent",
                message_seq="3",
                sender_id="3956020260",
                timestamp_iso="2026-03-15T02:03:05+08:00",
                asset_type="image",
                file_name="a.png",
                status="reused",
            ),
            MaterializedAsset(
                message_id="nested-forward-parent",
                message_seq="3",
                sender_id="3956020260",
                timestamp_iso="2026-03-15T02:03:05+08:00",
                asset_type="image",
                file_name="b.png",
                status="reused",
            ),
            MaterializedAsset(
                message_id="nested-forward-parent",
                message_seq="3",
                sender_id="3956020260",
                timestamp_iso="2026-03-15T02:03:05+08:00",
                asset_type="image",
                file_name="c.png",
                status="reused",
            ),
            MaterializedAsset(
                message_id="speech-parent",
                message_seq="4",
                sender_id="3956020260",
                timestamp_iso="2026-03-15T02:03:06+08:00",
                asset_type="speech",
                file_name="voice.amr",
                status="copied",
            ),
        ],
    )

    summary = build_export_content_summary(snapshot, bundle, profile="all")

    assert summary["expected_assets"]["image"] == 7
    assert summary["actual_assets"]["image"] == 7
    assert summary["expected_assets"]["speech"] == 1
    assert summary["actual_assets"]["speech"] == 1
