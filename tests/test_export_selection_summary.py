from __future__ import annotations

from pathlib import Path

from qq_data_core.export_selection import (
    build_export_content_summary,
    format_actionable_missing_breakdown_compact,
    format_background_missing_breakdown_compact,
    format_export_content_summary,
    format_export_content_summary_compact,
    format_export_verdict_compact,
    format_missing_breakdown_compact,
    format_missing_retry_hints_compact,
    format_watch_export_result_summary,
)
from qq_data_core.models import (
    ExportBundleResult,
    MaterializedAsset,
    NormalizedMessage,
    NormalizedSegment,
    NormalizedSnapshot,
)


def _build_snapshot() -> NormalizedSnapshot:
    return NormalizedSnapshot(
        chat_type="group",
        chat_id="922065597",
        metadata={
            "source": "napcat_fast_history_bulk+napcat_http",
            "bulk_partial_fallback": True,
            "pages_scanned": 11,
            "forward_detail_count": 5,
            "forward_structure_unavailable_count": 2,
        },
        messages=[
            NormalizedMessage(
                chat_type="group",
                chat_id="922065597",
                group_id="922065597",
                sender_id="10001",
                message_id="m1",
                timestamp_ms=1,
                timestamp_iso="2026-03-20T12:00:00+08:00",
                content="[image:a.jpg]",
                text_content="",
                segments=[NormalizedSegment(type="image", file_name="a.jpg")],
            ),
            NormalizedMessage(
                chat_type="group",
                chat_id="922065597",
                group_id="922065597",
                sender_id="10002",
                message_id="m2",
                timestamp_ms=2,
                timestamp_iso="2026-03-20T12:05:00+08:00",
                content="[image:b.jpg]",
                text_content="",
                segments=[NormalizedSegment(type="image", file_name="b.jpg")],
            ),
        ],
    )


def _build_bundle() -> ExportBundleResult:
    return ExportBundleResult(
        data_path=Path("exports/test.jsonl"),
        manifest_path=Path("exports/test.manifest.json"),
        assets_dir=Path("exports/test_assets"),
        record_count=2,
        copied_asset_count=0,
        reused_asset_count=0,
        missing_asset_count=3,
        assets=[
            MaterializedAsset(
                message_id="m1",
                sender_id="10001",
                timestamp_iso="2026-03-20T12:00:00+08:00",
                asset_type="image",
                file_name="a.jpg",
                status="missing",
                resolver="missing_after_napcat",
                missing_kind="missing_after_napcat",
            ),
            MaterializedAsset(
                message_id="m1",
                sender_id="10001",
                timestamp_iso="2026-03-20T12:00:01+08:00",
                asset_type="image",
                file_name="a2.jpg",
                status="missing",
                resolver="qq_not_downloaded_local_placeholder",
                missing_kind="qq_not_downloaded_local_placeholder",
            ),
            MaterializedAsset(
                message_id="m2",
                sender_id="10002",
                timestamp_iso="2026-03-20T12:05:00+08:00",
                asset_type="image",
                file_name="b.jpg",
                status="missing",
                resolver="qq_expired_after_napcat",
                missing_kind="qq_expired_after_napcat",
            ),
        ],
    )


def test_export_content_summary_separates_actionable_and_background_missing() -> None:
    summary = build_export_content_summary(
        _build_snapshot(),
        _build_bundle(),
        profile="all",
        fmt="jsonl",
    )

    assert summary["missing_breakdown"] == {
        "missing_after_napcat": 1,
        "qq_expired_after_napcat": 1,
        "qq_not_downloaded_local_placeholder": 1,
    }
    assert summary["actionable_missing_breakdown"] == {"missing_after_napcat": 1}
    assert summary["background_missing_breakdown"] == {
        "qq_expired_after_napcat": 1,
        "qq_not_downloaded_local_placeholder": 1,
    }
    assert summary["actionable_missing_count"] == 1
    assert summary["background_missing_count"] == 2


def test_missing_retry_hints_exclude_placeholder_and_expired_only_clusters() -> None:
    summary = build_export_content_summary(
        _build_snapshot(),
        _build_bundle(),
        profile="all",
        fmt="jsonl",
    )

    retry_hints = format_missing_retry_hints_compact(summary, shell="repl")

    assert len(retry_hints) == 1
    assert "kinds=[missing_after_napcat:1]" in retry_hints[0]
    assert "assets=1" in retry_hints[0]
    assert "cmd=/export group 922065597" in retry_hints[0]


def test_compact_and_detailed_format_include_actionable_and_background_counts() -> None:
    summary = build_export_content_summary(
        _build_snapshot(),
        _build_bundle(),
        profile="all",
        fmt="jsonl",
    )

    compact = format_export_content_summary_compact(summary)
    watch_summary = format_watch_export_result_summary(summary)
    detailed = "\n".join(format_export_content_summary(summary))

    assert "actionable_missing=1" in compact
    assert "background_missing=2" in compact
    assert "src=napcat_fast_history_bulk+napcat_http" in compact
    assert "history_fallback=partial" in compact
    assert "fwd_gap=2" in compact
    assert "actionable_miss=1" in watch_summary
    assert "background_miss=2" in watch_summary
    assert "src=napcat_fast_history_bulk+napcat_http" in watch_summary
    assert "history_fallback=partial" in watch_summary
    assert "fwd_gap=2" in watch_summary
    assert "actionable_missing_reason=[missing_after_napcat:1]" in detailed
    assert "history_source=napcat_fast_history_bulk+napcat_http history_fallback=partial pages_scanned=11" in detailed
    assert "forward_detail_count=5 forward_structure_unavailable=2" in detailed
    assert (
        "background_missing_reason=[qq_expired_after_napcat:1, qq_not_downloaded_local_placeholder:1]"
        in detailed
    )
    assert format_missing_breakdown_compact(summary) == (
        "missing_after_napcat:1, qq_expired_after_napcat:1, qq_not_downloaded_local_placeholder:1"
    )
    assert format_actionable_missing_breakdown_compact(summary) == "missing_after_napcat:1"
    assert format_background_missing_breakdown_compact(summary) == (
        "qq_expired_after_napcat:1, qq_not_downloaded_local_placeholder:1"
    )
    assert (
        format_export_verdict_compact(summary)
        == "export_verdict: success_with_actionable_missing final_assets=0/2 final_missing=3 actionable_missing=1 background_missing=2"
    )


def test_detailed_format_emits_background_only_missing_note() -> None:
    bundle = ExportBundleResult(
        data_path=Path("exports/test.jsonl"),
        manifest_path=Path("exports/test.manifest.json"),
        assets_dir=Path("exports/test_assets"),
        record_count=2,
        copied_asset_count=0,
        reused_asset_count=0,
        missing_asset_count=2,
        assets=[
            MaterializedAsset(
                message_id="m1",
                sender_id="10001",
                timestamp_iso="2026-03-20T12:00:00+08:00",
                asset_type="image",
                file_name="a.jpg",
                status="missing",
                resolver="qq_not_downloaded_local_placeholder",
                missing_kind="qq_not_downloaded_local_placeholder",
            ),
            MaterializedAsset(
                message_id="m2",
                sender_id="10002",
                timestamp_iso="2026-03-20T12:05:00+08:00",
                asset_type="image",
                file_name="b.jpg",
                status="missing",
                resolver="qq_expired_after_napcat",
                missing_kind="qq_expired_after_napcat",
            ),
        ],
    )
    summary = build_export_content_summary(
        _build_snapshot(),
        bundle,
        profile="all",
        fmt="jsonl",
    )

    detailed = "\n".join(format_export_content_summary(summary))

    assert "missing_note=当前剩余 missing 均为背景缺失" in detailed
    assert (
        format_export_verdict_compact(summary)
        == "export_verdict: success_with_background_missing final_assets=0/2 final_missing=2 actionable_missing=0 background_missing=2"
    )
