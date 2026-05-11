"""Tests for export diagnostics functionality."""

from __future__ import annotations

from pathlib import Path

import pytest

from qq_data_process.diagnostics import diagnose_export, ExportDiagnosticReport


def test_diagnose_export_handles_small_fixture() -> None:
    """Test diagnostic on small smoke fixture."""
    export_path = Path("tests/fixtures/smoke.jsonl")
    manifest_path = Path("tests/fixtures/smoke.manifest.json")

    if not manifest_path.exists():
        pytest.skip("Manifest fixture not available")

    report = diagnose_export(export_path, manifest_path)

    assert report.message_count == 6
    assert report.chat_type == "private"
    assert report.export_path == export_path
    assert report.manifest_path == manifest_path


def test_diagnose_export2_matches_expected_counts() -> None:
    """Test diagnostic on export2 real export."""
    export_path = Path(
        r"C:\Users\Peter\Downloads\export2\group_856972560_20260311_000132.jsonl"
    )

    if not export_path.exists():
        pytest.skip("export2 not available")

    report = diagnose_export(export_path)

    # Expected counts from task requirements
    assert report.image_stats.referenced == 1195, "export2 image refs should be 1195"
    assert report.image_stats.missing == 479, "export2 image missing should be 479"
    assert report.video_stats.referenced == 41, "export2 video refs should be 41"
    assert report.file_stats.referenced == 7, "export2 file refs should be 7"

    assert report.message_count == 6246
    assert report.chat_id == "856972560"
    assert report.manifest_total_assets == 1243


def test_diagnose_export3_matches_expected_counts() -> None:
    """Test diagnostic on export3 real export."""
    export_path = Path(
        r"C:\Users\Peter\Downloads\export3\group_763328502_20260311_000925.jsonl"
    )

    if not export_path.exists():
        pytest.skip("export3 not available")

    report = diagnose_export(export_path)

    # Expected counts from task requirements
    assert report.image_stats.referenced == 16336, "export3 image refs should be 16336"
    assert report.image_stats.missing == 9865, "export3 image missing should be 9865"
    assert report.speech_stats.referenced == 163, "export3 speech refs should be 163"
    assert (
        report.sticker_static_stats.referenced + report.sticker_dynamic_stats.referenced
        == 138
    ), "export3 sticker refs should be 138"

    assert report.message_count == 69459
    assert report.chat_id == "763328502"
    assert report.manifest_total_assets == 16937


def test_diagnostic_report_format_summary_produces_readable_output() -> None:
    """Test that format_summary produces human-readable output."""
    export_path = Path(
        r"C:\Users\Peter\Downloads\export2\group_856972560_20260311_000132.jsonl"
    )

    if not export_path.exists():
        pytest.skip("export2 not available")

    report = diagnose_export(export_path)
    summary = report.format_summary()

    assert "Export Diagnostic Report" in summary
    assert "Media Coverage Summary" in summary
    assert "Images" in summary
    assert "refs=" in summary
    assert "mat=" in summary
    assert "miss=" in summary
