from __future__ import annotations

from pathlib import Path

from qq_data_cli.status_display import (
    build_rich_status_text,
    colorize_status_fields_for_ansi,
    format_export_result_lines,
    format_prefetch_media_progress_line,
)
from qq_data_cli.app import _format_cli_export_progress
from qq_data_cli.repl import SlashRepl
from qq_data_core.models import ExportBundleResult


def test_colorize_status_fields_for_ansi_colors_only_exact_status_values(monkeypatch) -> None:
    monkeypatch.setattr(
        "qq_data_cli.status_display._supports_ansi_status_color",
        lambda stream=None: True,
    )
    rendered = colorize_status_fields_for_ansi(
        "status=success export_status=failed status=in progress login_status=failed status=timeout"
    )

    assert "status=\x1b[32msuccess\x1b[0m" in rendered
    assert "export_status=\x1b[31mfailed\x1b[0m" in rendered
    assert "status=\x1b[33min progress\x1b[0m" in rendered
    assert "login_status=failed" in rendered
    assert "status=timeout" in rendered


def test_build_rich_status_text_styles_only_exact_status_values() -> None:
    rendered = build_rich_status_text(
        "status=success login_status=failed export_status=failed status=in progress"
    )

    assert rendered.plain == "status=success login_status=failed export_status=failed status=in progress"
    styled_segments = {(span.start, span.end, span.style) for span in rendered.spans}
    assert (7, 14, "green") in styled_segments
    assert (49, 55, "red") in styled_segments
    assert (63, 74, "yellow") in styled_segments


def test_format_export_result_lines_builds_human_friendly_sections() -> None:
    bundle = ExportBundleResult(
        data_path=Path("exports/test.jsonl"),
        manifest_path=Path("exports/test.manifest.json"),
        assets_dir=Path("exports/test_assets"),
        record_count=2000,
        copied_asset_count=341,
        reused_asset_count=100,
        missing_asset_count=129,
    )
    summary = {
        "oldest_timestamp_iso": "2025-09-23T16:40:52+08:00",
        "latest_timestamp_iso": "2026-03-17T17:01:33+08:00",
        "history_source": "napcat_fast_history_bulk",
        "bulk_partial_fallback": False,
        "forward_detail_count": 5,
        "forward_structure_unavailable_count": 1,
        "expected_assets": {"image": 570},
        "actual_assets": {"image": 441},
        "missing_assets": {"image": 129},
        "actionable_missing_count": 0,
        "background_missing_count": 129,
        "missing_breakdown": {
            "qq_expired_after_napcat": 5,
            "qq_not_downloaded_local_placeholder": 124,
        },
        "actionable_missing_breakdown": {},
        "background_missing_breakdown": {
            "qq_expired_after_napcat": 5,
            "qq_not_downloaded_local_placeholder": 124,
        },
    }
    trace_summary = {
        "elapsed_s": 23.832,
        "pages_scanned": 11,
        "retry_events": 0,
        "prefetch_chunk_count": 8,
        "average_prefetch_chunk_s": 2.5,
        "slowest_prefetch_chunk_s": 9.8,
        "prefetch_timeout_count": 0,
        "prefetch_degraded": False,
    }

    lines = format_export_result_lines(
        session_line="export_session: uin=3956020260 nick=wiki online=True",
        content_summary=summary,
        bundle=bundle,
        trace_summary=trace_summary,
        trace_path=Path("state/export_perf/run.jsonl"),
    )

    rendered = "\n".join(lines)
    assert "export_result:" in rendered
    assert "export_status=success export_verdict=success_with_background_missing" in rendered
    assert "session=uin=3956020260 nick=wiki online=True" in rendered
    assert "files:" in rendered
    assert "summary:" in rendered
    assert "prefetch_chunks=8 avg_prefetch_chunk=2.5s slowest_prefetch_chunk=9.8s prefetch_timeout_count=0 prefetch_degraded=no" in rendered
    assert "assets:" in rendered
    assert "final_assets=441/570 copied=341 reused=100 missing=129" in rendered
    assert "background_missing_reason=[qq_expired_after_napcat:5, qq_not_downloaded_local_placeholder:124]" in rendered
    assert "当前剩余 missing 均为背景缺失" in rendered


def test_format_prefetch_media_progress_line_reports_chunk_progress() -> None:
    rendered = format_prefetch_media_progress_line(
        {
            "phase": "prefetch_media_chunk",
            "stage": "done",
            "chunk_index": 3,
            "chunk_count": 16,
            "request_count": 200,
            "total_request_count": 3150,
            "overall_request_count": 5700,
            "processed_request_count": 600,
            "hydrated_count": 188,
            "elapsed_s": 0.8,
        }
    )

    assert rendered is not None
    assert "status=in progress" in rendered
    assert "chunk=3/16" in rendered
    assert "context=600/3150 total=5700" in rendered
    assert "batch=200" in rendered
    assert "hydrated=188" in rendered
    assert "chunk_elapsed=0.8s" in rendered
    assert "rate=250.0/s" in rendered


def test_format_prefetch_media_progress_line_supports_prefix_and_final_success() -> None:
    rendered = format_prefetch_media_progress_line(
        {
            "phase": "prefetch_media",
            "stage": "done",
            "request_count": 3150,
            "elapsed_s": 14.0,
        },
        prefix="batch[1] ",
    )

    assert rendered == (
        "status=success batch[1] export_progress: prefetched media context "
        "requests=3150 elapsed=14.0s rate=225.0/s"
    )


def test_format_prefetch_media_progress_line_reports_prepare_progress() -> None:
    rendered = format_prefetch_media_progress_line(
        {
            "phase": "prefetch_media_prepare",
            "stage": "progress",
            "overall_request_count": 3150,
            "scanned_request_count": 1250,
            "context_request_count": 642,
            "prefetched_local_count": 71,
            "skipped_old_bucket_count": 537,
            "elapsed_s": 18.5,
        }
    )

    assert rendered == (
        "status=in progress export_progress: planning media prefetch "
        "scanned=1250/3150 context=642 local=71 skip_old=537 rate=67.6/s elapsed=18.5s"
    )


def test_format_export_result_lines_marks_actionable_missing_as_success_with_note() -> None:
    bundle = ExportBundleResult(
        data_path=Path("exports/test.jsonl"),
        manifest_path=Path("exports/test.manifest.json"),
        assets_dir=Path("exports/test_assets"),
        record_count=20,
        copied_asset_count=10,
        reused_asset_count=3,
        missing_asset_count=2,
    )
    summary = {
        "oldest_timestamp_iso": "2026-03-21T01:00:00+08:00",
        "latest_timestamp_iso": "2026-03-21T01:10:00+08:00",
        "history_source": "napcat_fast_history",
        "forward_detail_count": 0,
        "forward_structure_unavailable_count": 0,
        "expected_assets": {"image": 15},
        "actual_assets": {"image": 13},
        "missing_assets": {"image": 2},
        "actionable_missing_count": 2,
        "background_missing_count": 0,
        "missing_breakdown": {"missing_after_napcat": 2},
        "actionable_missing_breakdown": {"missing_after_napcat": 2},
        "background_missing_breakdown": {},
    }
    trace_summary = {"elapsed_s": 3.0, "pages_scanned": 1, "retry_events": 0}

    lines = format_export_result_lines(
        session_line="export_session: uin=3956020260 nick=wiki online=True",
        content_summary=summary,
        bundle=bundle,
        trace_summary=trace_summary,
        trace_path=Path("state/export_perf/run.jsonl"),
    )

    rendered = "\n".join(lines)
    assert "export_status=success export_verdict=success_with_actionable_missing" in rendered
    assert "actionable_missing_reason=[missing_after_napcat:2]" in rendered
    assert "当前导出已完成，但仍有可行动 missing" in rendered


def test_format_prefetch_media_progress_line_surfaces_prefetch_timeout_detail() -> None:
    rendered = format_prefetch_media_progress_line(
        {
            "phase": "prefetch_media_chunk",
            "stage": "error",
            "chunk_index": 2,
            "chunk_count": 8,
            "request_count": 50,
            "total_request_count": 1576,
            "overall_request_count": 2531,
            "processed_request_count": 200,
            "elapsed_s": 20.0,
            "reason": "chunk_timeout",
            "timeout_s": 20.0,
        }
    )

    assert rendered == (
        "status=failed export_progress: prefetch chunk=2/8 "
        "context=200/1576 total=2531 batch=50 chunk_elapsed=20.0s "
        "reason=chunk_timeout timeout=20.0s"
    )


def test_cli_export_progress_routes_prefetch_prepare_phase() -> None:
    rendered = _format_cli_export_progress(
        {
            "phase": "prefetch_media_prepare",
            "stage": "progress",
            "overall_request_count": 3150,
            "scanned_request_count": 500,
            "context_request_count": 123,
            "prefetched_local_count": 17,
            "skipped_old_bucket_count": 240,
            "elapsed_s": 10.0,
        }
    )

    assert rendered == (
        "status=in progress export_progress: planning media prefetch "
        "scanned=500/3150 context=123 local=17 skip_old=240 rate=50.0/s elapsed=10.0s"
    )


def test_repl_export_progress_routes_prefetch_prepare_phase() -> None:
    repl = SlashRepl()

    rendered = repl._format_root_export_progress(
        {
            "phase": "prefetch_media_prepare",
            "stage": "done",
            "overall_request_count": 3150,
            "scanned_request_count": 3150,
            "context_request_count": 1180,
            "prefetched_local_count": 64,
            "skipped_old_bucket_count": 1906,
            "elapsed_s": 21.0,
        },
        prefix="",
    )

    assert rendered == (
        "status=success export_progress: planned media prefetch "
        "scanned=3150/3150 context=1180 local=64 skip_old=1906 rate=150.0/s elapsed=21.0s"
    )
