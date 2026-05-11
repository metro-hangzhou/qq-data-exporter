from __future__ import annotations

from .models import (
    CanonicalMessageRecord,
    ChunkBuildResult,
    ChunkMembershipRecord,
    ChunkPolicySpec,
    ChunkRecord,
    ChunkSetRecord,
)
from .utils import preview_text, stable_digest


def _make_chunk_set(
    *,
    run_id: str,
    chat_id: str,
    spec: ChunkPolicySpec,
    chunk_kind: str,
) -> ChunkSetRecord:
    chunk_set_id = (
        f"chunkset_{stable_digest(run_id, chat_id, spec.name, spec.version, spec.params)}"
    )
    return ChunkSetRecord(
        chunk_set_id=chunk_set_id,
        run_id=run_id,
        chat_id=chat_id,
        policy_name=spec.name,
        policy_version=spec.version,
        policy_params=dict(spec.params),
        chunk_kind=chunk_kind,
    )


def _build_chunk_records(
    *,
    chunk_set: ChunkSetRecord,
    groups: list[list[CanonicalMessageRecord]],
) -> ChunkBuildResult:
    chunks: list[ChunkRecord] = []
    memberships: list[ChunkMembershipRecord] = []
    for chunk_index, group in enumerate(groups):
        if not group:
            continue
        chunk_id = f"chunk_{stable_digest(chunk_set.chunk_set_id, chunk_index)}"
        preview = preview_text(" ".join(item.content for item in group if item.content))
        chunks.append(
            ChunkRecord(
                chunk_id=chunk_id,
                chunk_set_id=chunk_set.chunk_set_id,
                chat_id=chunk_set.chat_id,
                chunk_kind=chunk_set.chunk_kind,
                ordinal=chunk_index,
                start_message_uid=group[0].message_uid,
                end_message_uid=group[-1].message_uid,
                start_timestamp_ms=group[0].timestamp_ms,
                end_timestamp_ms=group[-1].timestamp_ms,
                message_count=len(group),
                content_preview=preview,
            )
        )
        for message_index, message in enumerate(group):
            memberships.append(
                ChunkMembershipRecord(
                    chunk_id=chunk_id,
                    message_uid=message.message_uid,
                    ordinal=message_index,
                )
            )
    return ChunkBuildResult(chunk_set=chunk_set, chunks=chunks, memberships=memberships)


class NoChunkPolicy:
    name = "none"
    version = "v1"

    def build(
        self,
        *,
        run_id: str,
        chat_id: str,
        spec: ChunkPolicySpec,
        messages: list[CanonicalMessageRecord],
    ) -> ChunkBuildResult:
        return ChunkBuildResult()


class WindowChunkPolicy:
    name = "window"
    version = "v1"

    def build(
        self,
        *,
        run_id: str,
        chat_id: str,
        spec: ChunkPolicySpec,
        messages: list[CanonicalMessageRecord],
    ) -> ChunkBuildResult:
        window_size = max(1, int(spec.params.get("window_size", 256)))
        overlap = max(0, int(spec.params.get("overlap", 32)))
        step = max(1, window_size - overlap)
        groups: list[list[CanonicalMessageRecord]] = []
        for start in range(0, len(messages), step):
            group = messages[start : start + window_size]
            if group:
                groups.append(group)
            if start + window_size >= len(messages):
                break
        chunk_set = _make_chunk_set(
            run_id=run_id,
            chat_id=chat_id,
            spec=spec,
            chunk_kind="window",
        )
        return _build_chunk_records(chunk_set=chunk_set, groups=groups)


class TimeGapChunkPolicy:
    name = "timegap"
    version = "v1"

    def build(
        self,
        *,
        run_id: str,
        chat_id: str,
        spec: ChunkPolicySpec,
        messages: list[CanonicalMessageRecord],
    ) -> ChunkBuildResult:
        if not messages:
            return ChunkBuildResult()
        gap_seconds = max(1, int(spec.params.get("gap_seconds", 600)))
        max_messages = max(1, int(spec.params.get("max_messages", 256)))
        groups: list[list[CanonicalMessageRecord]] = []
        current: list[CanonicalMessageRecord] = [messages[0]]
        for previous, current_message in zip(messages, messages[1:]):
            gap = (current_message.timestamp_ms - previous.timestamp_ms) / 1000.0
            if gap > gap_seconds or len(current) >= max_messages:
                groups.append(current)
                current = [current_message]
                continue
            current.append(current_message)
        if current:
            groups.append(current)
        chunk_set = _make_chunk_set(
            run_id=run_id,
            chat_id=chat_id,
            spec=spec,
            chunk_kind="timegap",
        )
        return _build_chunk_records(chunk_set=chunk_set, groups=groups)


class HybridChunkPolicy:
    name = "hybrid"
    version = "v1"

    def build(
        self,
        *,
        run_id: str,
        chat_id: str,
        spec: ChunkPolicySpec,
        messages: list[CanonicalMessageRecord],
    ) -> ChunkBuildResult:
        if not messages:
            return ChunkBuildResult()
        gap_seconds = max(1, int(spec.params.get("gap_seconds", 600)))
        max_messages = max(1, int(spec.params.get("max_messages", 256)))
        overlap = max(0, int(spec.params.get("overlap", 32)))
        time_groups: list[list[CanonicalMessageRecord]] = []
        current: list[CanonicalMessageRecord] = [messages[0]]
        for previous, current_message in zip(messages, messages[1:]):
            gap = (current_message.timestamp_ms - previous.timestamp_ms) / 1000.0
            if gap > gap_seconds:
                time_groups.append(current)
                current = [current_message]
                continue
            current.append(current_message)
        if current:
            time_groups.append(current)

        groups: list[list[CanonicalMessageRecord]] = []
        step = max(1, max_messages - overlap)
        for time_group in time_groups:
            if len(time_group) <= max_messages:
                groups.append(time_group)
                continue
            for start in range(0, len(time_group), step):
                chunk = time_group[start : start + max_messages]
                if chunk:
                    groups.append(chunk)
                if start + max_messages >= len(time_group):
                    break
        chunk_set = _make_chunk_set(
            run_id=run_id,
            chat_id=chat_id,
            spec=spec,
            chunk_kind="hybrid",
        )
        return _build_chunk_records(chunk_set=chunk_set, groups=groups)
