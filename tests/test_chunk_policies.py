from __future__ import annotations

from qq_data_process.chunking import (
    HybridChunkPolicy,
    NoChunkPolicy,
    TimeGapChunkPolicy,
    WindowChunkPolicy,
)
from qq_data_process.models import CanonicalMessageRecord, ChunkPolicySpec


def _message(idx: int, timestamp_ms: int) -> CanonicalMessageRecord:
    return CanonicalMessageRecord(
        message_uid=f"msg_{idx}",
        import_source="exporter_jsonl",
        fidelity="high",
        chat_type="group",
        chat_id="123",
        chat_name="示例群",
        sender_id_raw="sender_1",
        sender_name_raw="sender",
        timestamp_ms=timestamp_ms,
        timestamp_iso=f"2026-03-07T00:00:0{idx}+08:00",
        content=f"message {idx}",
        text_content=f"message {idx}",
    )


def test_no_chunk_policy_returns_empty_result() -> None:
    policy = NoChunkPolicy()
    result = policy.build(
        run_id="run1",
        chat_id="123",
        spec=ChunkPolicySpec(name="none"),
        messages=[_message(1, 1_000)],
    )
    assert result.chunk_set is None
    assert result.chunks == []


def test_window_policy_builds_multiple_chunks() -> None:
    policy = WindowChunkPolicy()
    messages = [_message(idx, idx * 1000) for idx in range(5)]
    result = policy.build(
        run_id="run1",
        chat_id="123",
        spec=ChunkPolicySpec(name="window", params={"window_size": 2, "overlap": 1}),
        messages=messages,
    )
    assert result.chunk_set is not None
    assert len(result.chunks) >= 4
    assert len(result.memberships) >= len(messages)


def test_timegap_and_hybrid_policies_produce_distinct_chunk_sets() -> None:
    messages = [
        _message(1, 1_000),
        _message(2, 2_000),
        _message(3, 700_000),
        _message(4, 701_000),
        _message(5, 702_000),
    ]
    timegap = TimeGapChunkPolicy().build(
        run_id="run1",
        chat_id="123",
        spec=ChunkPolicySpec(name="timegap", params={"gap_seconds": 60}),
        messages=messages,
    )
    hybrid = HybridChunkPolicy().build(
        run_id="run1",
        chat_id="123",
        spec=ChunkPolicySpec(
            name="hybrid",
            params={"gap_seconds": 60, "max_messages": 2, "overlap": 1},
        ),
        messages=messages,
    )

    assert timegap.chunk_set is not None
    assert hybrid.chunk_set is not None
    assert timegap.chunk_set.chunk_set_id != hybrid.chunk_set.chunk_set_id
    assert len(hybrid.chunks) >= len(timegap.chunks)
