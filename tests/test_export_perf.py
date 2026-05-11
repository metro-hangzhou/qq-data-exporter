from __future__ import annotations

from pathlib import Path

from qq_data_core.export_perf import ExportPerfTraceWriter


def test_export_perf_trace_writer_records_summary() -> None:
    writer = ExportPerfTraceWriter(
        Path("state"),
        chat_type="group",
        chat_id="42",
        mode="watch_export",
    )

    writer.write_event("export_start", {"chat_name": "Alpha"})
    writer.write_event(
        "full_scan",
        {
            "pages_scanned": 1,
            "collected_messages": 100,
            "page_duration_s": 0.5,
        },
    )
    writer.write_event(
        "full_scan",
        {
            "pages_scanned": 2,
            "collected_messages": 220,
            "page_duration_s": 1.25,
        },
    )
    writer.write_event(
        "page_retry",
        {
            "mode": "full_scan",
            "reason": "read_timeout",
            "requested_count": 200,
            "next_page_size": 100,
        },
    )
    writer.write_event(
        "materialize_asset_step",
        {
            "stage": "done",
            "current": 17,
            "asset_type": "image",
            "asset_role": "",
            "file_name": "slow-one.png",
            "status": "missing",
            "resolver": "qq_expired_after_napcat",
            "step_elapsed_s": 12.5,
        },
    )
    summary = writer.build_summary(record_count=220)
    writer.close()

    assert writer.path.exists()
    assert summary["pages_scanned"] == 2
    assert summary["retry_events"] == 1
    assert summary["record_count"] == 220
    assert summary["average_page_s"] == 0.875
    assert summary["slowest_page_s"] == 1.25
    assert summary["materialize_step_count"] == 1
    assert summary["average_materialize_step_s"] == 12.5
    assert summary["slowest_materialize_step_s"] == 12.5
    assert summary["slowest_materialize_step"]["file_name"] == "slow-one.png"


def test_export_perf_trace_writer_samples_fast_materialize_steps_but_keeps_summary() -> None:
    writer = ExportPerfTraceWriter(
        Path("state"),
        chat_type="group",
        chat_id="99",
        mode="watch_export",
    )

    writer.write_event(
        "materialize_asset_step",
        {
            "stage": "start",
            "current": 2,
            "total": 500,
            "file_name": "fast-step.png",
        },
    )
    writer.write_event(
        "materialize_asset_step",
        {
            "stage": "done",
            "current": 2,
            "total": 500,
            "asset_type": "image",
            "file_name": "fast-step.png",
            "status": "copied",
            "resolver": "segment_path",
            "step_elapsed_s": 0.02,
        },
    )
    summary = writer.build_summary(record_count=1)
    writer.close()

    lines = writer.path.read_text(encoding="utf-8").splitlines()

    assert summary["materialize_step_count"] == 1
    assert summary["average_materialize_step_s"] == 0.02
    assert not any("fast-step.png" in line for line in lines)
