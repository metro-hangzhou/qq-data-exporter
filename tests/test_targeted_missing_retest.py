from __future__ import annotations

from pathlib import Path

from targeted_missing_retest import (
    RetryCluster,
    _collect_retry_clusters,
    _infer_chat_target,
    _resolve_time_token,
)


def test_collect_retry_clusters_extracts_valid_entries() -> None:
    manifest = {
        "content_summary": {
            "missing_retry_plan": {
                "clusters": [
                    {
                        "start_token": "2025-09-08_02-08-12",
                        "end_token": "2025-09-08_02-08-42",
                        "repl_command": "/export group 922065597 2025-09-08_02-08-12 2025-09-08_02-08-42",
                    }
                ]
            }
        }
    }

    clusters = _collect_retry_clusters(manifest)

    assert clusters == [
        RetryCluster(
            index=1,
            start_token="2025-09-08_02-08-12",
            end_token="2025-09-08_02-08-42",
            repl_command="/export group 922065597 2025-09-08_02-08-12 2025-09-08_02-08-42",
        )
    ]


def test_collect_retry_clusters_falls_back_to_actionable_manifest_assets() -> None:
    manifest = {
        "assets": [
            {
                "status": "missing",
                "resolver": "missing_after_napcat",
                "timestamp_iso": "2025-09-08T02:08:27+08:00",
            },
            {
                "status": "missing",
                "resolver": "qq_expired_after_napcat",
                "timestamp_iso": "2025-09-08T02:08:28+08:00",
            },
        ]
    }

    clusters = _collect_retry_clusters(manifest)

    assert clusters == [
        RetryCluster(
            index=1,
            start_token="2025-09-08_02-08-12",
            end_token="2025-09-08_02-08-42",
            repl_command=None,
        )
    ]


def test_infer_chat_target_falls_back_to_manifest_name() -> None:
    manifest_path = Path("exports/group_922065597_20260321_020956_391299.manifest.json")
    manifest = {"content_summary": {}}

    chat_type, chat_id, chat_name = _infer_chat_target(manifest_path, manifest)

    assert chat_type == "group"
    assert chat_id == "922065597"
    assert chat_name is None


def test_resolve_time_token_parses_explicit_datetime_literal() -> None:
    resolved = _resolve_time_token("2025-09-08_02-08-12")

    assert resolved.isoformat() == "2025-09-08T02:08:12+08:00"
