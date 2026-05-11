from __future__ import annotations

from pathlib import Path

from qq_data_process import detect_source_type


def test_detect_source_type_by_suffix() -> None:
    assert detect_source_type(Path("a.jsonl")) == "exporter_jsonl"
    assert detect_source_type(Path("a.json")) == "qce_json"
    assert detect_source_type(Path("a.txt")) == "qq_txt"
