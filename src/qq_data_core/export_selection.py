from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal

from .models import ExportBundleResult, NormalizedMessage, NormalizedSegment, NormalizedSnapshot
from .time_expr import format_export_datetime

ExportProfile = Literal["all", "only_text", "text_image", "text_image_emoji"]

CONTENT_SEGMENT_ORDER: tuple[str, ...] = (
    "text",
    "system",
    "share",
    "forward",
    "image",
    "video",
    "speech",
    "file",
    "emoji",
    "sticker",
    "reply",
    "unsupported",
)

ASSET_TYPE_ORDER: tuple[str, ...] = (
    "image",
    "video",
    "speech",
    "file",
    "sticker.static",
    "sticker.dynamic",
    "sticker",
)

PROFILE_SEGMENT_TYPES: dict[ExportProfile, set[str] | None] = {
    "all": None,
    "only_text": {"text", "system", "share", "forward"},
    "text_image": {"text", "system", "share", "forward", "image"},
    "text_image_emoji": {"text", "system", "share", "forward", "image", "emoji", "sticker"},
}

PROFILE_COMMANDS: dict[ExportProfile, str] = {
    "all": "/export",
    "only_text": "/export_onlyText",
    "text_image": "/export_TextImage",
    "text_image_emoji": "/export_TextImageEmoji",
}

MISSING_RETRY_CLUSTER_GAP = timedelta(minutes=10)
MISSING_RETRY_WINDOW_PADDING = timedelta(seconds=15)
MAX_MISSING_RETRY_HINTS = 6
BACKGROUND_MISSING_KINDS: set[str] = {
    "qq_not_downloaded_local_placeholder",
    "qq_expired_after_napcat",
}


def _count_segments_in_messages(messages: list[NormalizedMessage]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for message in messages:
        for segment in message.segments:
            counts[segment.type] += 1
    return counts


def _coerce_segment_counts(value: object) -> Counter[str]:
    counter = Counter()
    if not isinstance(value, dict):
        return counter
    for key, raw in value.items():
        try:
            counter[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return counter


def apply_export_profile(snapshot: NormalizedSnapshot, profile: ExportProfile) -> NormalizedSnapshot:
    allowed = PROFILE_SEGMENT_TYPES[profile]
    if allowed is None:
        return snapshot

    source_message_count = len(snapshot.messages)
    source_segment_counts = _coerce_segment_counts(snapshot.metadata.get("source_segment_counts"))
    if not source_segment_counts:
        source_segment_counts = _count_segments_in_messages(snapshot.messages)
    filtered_messages: list[NormalizedMessage] = []
    dropped_messages = 0
    for message in snapshot.messages:
        segments = [segment.model_copy(deep=True) for segment in message.segments if segment.type in allowed]
        rebuilt = _rebuild_message(message, segments)
        if rebuilt is None:
            dropped_messages += 1
            continue
        filtered_messages.append(rebuilt)

    metadata = deepcopy(snapshot.metadata)
    metadata["export_profile"] = profile
    metadata["source_message_count"] = source_message_count
    metadata["dropped_message_count"] = dropped_messages
    metadata["kept_message_count"] = len(filtered_messages)
    metadata["source_segment_counts"] = dict(source_segment_counts)
    metadata["kept_segment_counts"] = dict(_count_segments_in_messages(filtered_messages))
    return snapshot.model_copy(update={"messages": filtered_messages, "metadata": metadata})


def trim_snapshot_to_last_messages(
    snapshot: NormalizedSnapshot,
    *,
    data_count: int | None,
):
    if data_count is None or data_count <= 0:
        return snapshot
    messages = list(snapshot.messages)
    trimmed_messages = messages[-data_count:]
    metadata = deepcopy(snapshot.metadata)
    source_segment_counts = _coerce_segment_counts(metadata.get("source_segment_counts"))
    if not source_segment_counts:
        source_segment_counts = _count_segments_in_messages(messages)
    metadata["requested_data_count"] = data_count
    metadata["source_message_count"] = len(messages)
    metadata["trimmed_message_count"] = len(trimmed_messages)
    metadata["source_segment_counts"] = dict(source_segment_counts)
    metadata["kept_segment_counts"] = dict(_count_segments_in_messages(trimmed_messages))
    return snapshot.model_copy(update={"messages": trimmed_messages, "metadata": metadata})


def build_export_content_summary(
    snapshot: NormalizedSnapshot,
    bundle: ExportBundleResult,
    *,
    profile: ExportProfile,
    fmt: str = "jsonl",
    strict_missing: str | None = None,
) -> dict[str, object]:
    segment_counts = Counter()
    source_segment_counts = _coerce_segment_counts(snapshot.metadata.get("source_segment_counts"))
    expected_assets = Counter()
    actual_assets = Counter()
    missing_assets = Counter()
    error_assets = Counter()
    missing_breakdown = Counter()
    actionable_missing_breakdown = Counter()
    background_missing_breakdown = Counter()

    for message in snapshot.messages:
        for segment in message.segments:
            segment_counts[segment.type] += 1
            for asset_key in _collect_segment_asset_keys(segment):
                expected_assets[asset_key] += 1

    if not source_segment_counts:
        source_segment_counts = Counter(segment_counts)

    for asset in bundle.assets:
        key = _asset_key(asset.asset_type, asset.asset_role)
        if asset.status in {"copied", "reused"}:
            actual_assets[key] += 1
        elif asset.status == "missing":
            missing_assets[key] += 1
            missing_kind = str(asset.missing_kind or asset.resolver or "missing")
            missing_breakdown[missing_kind] += 1
            if missing_kind in BACKGROUND_MISSING_KINDS:
                background_missing_breakdown[missing_kind] += 1
            else:
                actionable_missing_breakdown[missing_kind] += 1
        elif asset.status == "error":
            error_assets[key] += 1

    for segment_type in CONTENT_SEGMENT_ORDER:
        segment_counts.setdefault(segment_type, 0)
    for asset_key in ASSET_TYPE_ORDER:
        expected_assets.setdefault(asset_key, 0)
        actual_assets.setdefault(asset_key, 0)
        missing_assets.setdefault(asset_key, 0)
        error_assets.setdefault(asset_key, 0)

    oldest_timestamp_iso = snapshot.messages[0].timestamp_iso if snapshot.messages else None
    latest_timestamp_iso = snapshot.messages[-1].timestamp_iso if snapshot.messages else None

    return {
        "profile": profile,
        "chat_type": snapshot.chat_type,
        "chat_id": snapshot.chat_id,
        "message_count": len(snapshot.messages),
        "source_message_count": int(snapshot.metadata.get("source_message_count") or len(snapshot.messages)),
        "requested_data_count": snapshot.metadata.get("requested_data_count"),
        "oldest_timestamp_iso": oldest_timestamp_iso,
        "latest_timestamp_iso": latest_timestamp_iso,
        "format": fmt,
        "strict_missing": strict_missing,
        "history_source": str(snapshot.metadata.get("source") or "").strip() or None,
        "bulk_partial_fallback": bool(snapshot.metadata.get("bulk_partial_fallback")),
        "pages_scanned": int(snapshot.metadata.get("pages_scanned") or 0),
        "forward_detail_count": int(snapshot.metadata.get("forward_detail_count") or 0),
        "forward_structure_unavailable_count": int(
            snapshot.metadata.get("forward_structure_unavailable_count") or 0
        ),
        "segment_counts": {key: int(segment_counts[key]) for key in CONTENT_SEGMENT_ORDER},
        "source_segment_counts": {
            key: int(source_segment_counts[key]) for key in CONTENT_SEGMENT_ORDER
        },
        "expected_assets": {key: int(expected_assets[key]) for key in ASSET_TYPE_ORDER},
        "actual_assets": {key: int(actual_assets[key]) for key in ASSET_TYPE_ORDER},
        "missing_assets": {key: int(missing_assets[key]) for key in ASSET_TYPE_ORDER},
        "error_assets": {key: int(error_assets[key]) for key in ASSET_TYPE_ORDER},
        "missing_breakdown": dict(sorted((str(key), int(value)) for key, value in missing_breakdown.items())),
        "actionable_missing_breakdown": dict(
            sorted((str(key), int(value)) for key, value in actionable_missing_breakdown.items())
        ),
        "background_missing_breakdown": dict(
            sorted((str(key), int(value)) for key, value in background_missing_breakdown.items())
        ),
        "actionable_missing_count": int(sum(actionable_missing_breakdown.values())),
        "background_missing_count": int(sum(background_missing_breakdown.values())),
        "missing_retry_plan": _build_missing_retry_plan(
            snapshot=snapshot,
            bundle=bundle,
            profile=profile,
            fmt=fmt,
            strict_missing=str(strict_missing or "").strip() or None,
        ),
    }


def format_export_content_summary(summary: dict[str, object]) -> list[str]:
    content_counts = summary.get("segment_counts") or {}
    source_content_counts = summary.get("source_segment_counts") or {}
    expected_assets = summary.get("expected_assets") or {}
    actual_assets = summary.get("actual_assets") or {}
    missing_assets = summary.get("missing_assets") or {}
    error_assets = summary.get("error_assets") or {}
    missing_breakdown = summary.get("missing_breakdown") or {}
    actionable_missing_breakdown = summary.get("actionable_missing_breakdown") or {}
    background_missing_breakdown = summary.get("background_missing_breakdown") or {}

    lines = [
        "export_summary:",
        (
            f"  profile={summary['profile']} "
            f"msgs={summary['message_count']} "
            f"source_msgs={summary['source_message_count']} "
            f"requested_data_count={summary.get('requested_data_count') or '-'}"
        ),
    ]
    if summary.get("oldest_timestamp_iso") and summary.get("latest_timestamp_iso"):
        lines.append(
            f"  time_range={summary['oldest_timestamp_iso']} -> {summary['latest_timestamp_iso']}"
        )
    history_source = str(summary.get("history_source") or "").strip()
    if history_source:
        source_parts = [f"history_source={history_source}"]
        if bool(summary.get("bulk_partial_fallback")):
            source_parts.append("history_fallback=partial")
        pages_scanned = int(summary.get("pages_scanned") or 0)
        if pages_scanned > 0:
            source_parts.append(f"pages_scanned={pages_scanned}")
        lines.append("  " + " ".join(source_parts))
    forward_detail_count = int(summary.get("forward_detail_count") or 0)
    forward_structure_unavailable_count = int(
        summary.get("forward_structure_unavailable_count") or 0
    )
    if forward_detail_count > 0 or forward_structure_unavailable_count > 0:
        detail_parts = [f"forward_detail_count={forward_detail_count}"]
        if forward_structure_unavailable_count > 0:
            detail_parts.append(
                f"forward_structure_unavailable={forward_structure_unavailable_count}"
            )
        lines.append("  " + " ".join(detail_parts))
    lines.append(
        "  content_export=["
        + ", ".join(
            f"{key}:{int(content_counts.get(key, 0))}/{int(source_content_counts.get(key, content_counts.get(key, 0)))}"
            for key in CONTENT_SEGMENT_ORDER
        )
        + "]"
    )
    lines.append(
        "  final_asset_result=["
        + ", ".join(
            (
                f"{key}:{int(actual_assets.get(key, 0))}/{int(expected_assets.get(key, 0))}"
                f" miss={int(missing_assets.get(key, 0))}"
                f" err={int(error_assets.get(key, 0))}"
            )
            for key in ASSET_TYPE_ORDER
        )
        + "]"
    )
    if any(int(value) for value in expected_assets.values()):
        lines.append(
            "  note=若上方出现 remote_downloads(subqueue)，它只统计远程 URL 下载子队列；"
            "最终结果请以 final_asset_result / final_missing_reason 为准"
        )
    if missing_breakdown:
        lines.append(
            "  final_missing_reason=["
            + _format_counter_mapping(missing_breakdown)
            + "]"
        )
    if actionable_missing_breakdown:
        lines.append(
            "  actionable_missing_reason=["
            + _format_counter_mapping(actionable_missing_breakdown)
            + "]"
        )
    if background_missing_breakdown:
        lines.append(
            "  background_missing_reason=["
            + _format_counter_mapping(background_missing_breakdown)
            + "]"
        )
    actionable_missing_count = int(summary.get("actionable_missing_count") or 0)
    background_missing_count = int(summary.get("background_missing_count") or 0)
    if actionable_missing_count == 0 and background_missing_count > 0:
        lines.append(
            "  missing_note=当前剩余 missing 均为背景缺失（placeholder / expired 类），"
            "当前导出链本身未发现新的可行动缺口"
        )
    retry_plan = summary.get("missing_retry_plan") or {}
    clusters = retry_plan.get("clusters") if isinstance(retry_plan, dict) else None
    if isinstance(clusters, list) and clusters:
        lines.append("  missing_retry_hint=可只重试下列 missing 资产时间窗：")
        for index, cluster in enumerate(clusters, start=1):
            if not isinstance(cluster, dict):
                continue
            lines.append(
                "    "
                + f"[{index}] assets={int(cluster.get('asset_count') or 0)} "
                + f"messages={int(cluster.get('message_count') or 0)} "
                + f"window={cluster.get('start_token')} -> {cluster.get('end_token')}"
            )
            repl_command = str(cluster.get("repl_command") or "").strip()
            if repl_command:
                lines.append(f"        repl={repl_command}")
    return lines


def format_missing_breakdown_compact(summary: dict[str, object]) -> str:
    missing_breakdown = summary.get("missing_breakdown") or {}
    return _format_counter_mapping(missing_breakdown)


def format_actionable_missing_breakdown_compact(summary: dict[str, object]) -> str:
    actionable_missing_breakdown = summary.get("actionable_missing_breakdown") or {}
    return _format_counter_mapping(actionable_missing_breakdown)


def format_background_missing_breakdown_compact(summary: dict[str, object]) -> str:
    background_missing_breakdown = summary.get("background_missing_breakdown") or {}
    return _format_counter_mapping(background_missing_breakdown)


def format_missing_retry_hints_compact(summary: dict[str, object], *, shell: Literal["repl", "cli"]) -> list[str]:
    retry_plan = summary.get("missing_retry_plan") or {}
    if not isinstance(retry_plan, dict):
        return []
    clusters = retry_plan.get("clusters")
    if not isinstance(clusters, list):
        return []
    key = "repl_command" if shell == "repl" else "cli_command"
    lines: list[str] = []
    for index, cluster in enumerate(clusters, start=1):
        if not isinstance(cluster, dict):
            continue
        command = str(cluster.get(key) or "").strip()
        if not command:
            continue
        missing_kinds = _format_counter_mapping(cluster.get("missing_kinds") or {})
        lines.append(
            f"retry_hint[{index}] kinds=[{missing_kinds}] "
            f"assets={int(cluster.get('asset_count') or 0)} "
            f"messages={int(cluster.get('message_count') or 0)} cmd={command}"
        )
    return lines


def format_export_verdict_compact(summary: dict[str, object]) -> str:
    actionable_missing_count = int(summary.get("actionable_missing_count") or 0)
    background_missing_count = int(summary.get("background_missing_count") or 0)
    missing_assets = summary.get("missing_assets") or {}
    actual_assets = summary.get("actual_assets") or {}
    expected_assets = summary.get("expected_assets") or {}

    final_missing = sum(int(value) for value in missing_assets.values())
    actual_total = sum(int(value) for value in actual_assets.values())
    expected_total = sum(int(value) for value in expected_assets.values())

    if final_missing <= 0:
        verdict = "success"
    elif actionable_missing_count > 0:
        verdict = "success_with_actionable_missing"
    elif background_missing_count > 0:
        verdict = "success_with_background_missing"
    else:
        verdict = "success_with_unclassified_missing"

    return (
        f"export_verdict: {verdict} "
        f"final_assets={actual_total}/{expected_total} "
        f"final_missing={final_missing} "
        f"actionable_missing={actionable_missing_count} "
        f"background_missing={background_missing_count}"
    )


def format_export_content_summary_compact(summary: dict[str, object]) -> str:
    message_count = summary.get("message_count")
    source_message_count = summary.get("source_message_count")
    requested_data_count = summary.get("requested_data_count") or "-"
    segment_counts = summary.get("segment_counts") or {}
    expected_assets = summary.get("expected_assets") or {}
    actual_assets = summary.get("actual_assets") or {}
    missing_assets = summary.get("missing_assets") or {}
    error_assets = summary.get("error_assets") or {}
    missing_breakdown = summary.get("missing_breakdown") or {}
    actionable_missing_count = int(summary.get("actionable_missing_count") or 0)
    background_missing_count = int(summary.get("background_missing_count") or 0)
    history_source = str(summary.get("history_source") or "").strip() or "-"
    bulk_partial_fallback = bool(summary.get("bulk_partial_fallback"))
    forward_structure_unavailable_count = int(summary.get("forward_structure_unavailable_count") or 0)

    segment_type_count = len(segment_counts)
    expected_total = sum(int(value) for value in expected_assets.values())
    actual_total = sum(int(value) for value in actual_assets.values())
    missing_total = sum(int(value) for value in missing_assets.values())
    error_total = sum(int(value) for value in error_assets.values())
    expired_total = int(missing_breakdown.get("qq_expired_after_napcat") or 0)

    return (
        "summary "
        f"profile={summary['profile']} "
        f"msgs={message_count}/{source_message_count} "
        f"req={requested_data_count} "
        f"src={history_source} "
        f"history_fallback={'partial' if bulk_partial_fallback else '-'} "
        f"segment_types={segment_type_count} "
        f"final_assets={actual_total}/{expected_total} "
        f"final_missing={missing_total} "
        f"actionable_missing={actionable_missing_count} "
        f"background_missing={background_missing_count} "
        f"fwd_gap={forward_structure_unavailable_count} "
        f"expired_occurrences={expired_total} "
        f"errors={error_total}"
    )


def format_watch_export_result_summary(summary: dict[str, object]) -> str:
    message_count = summary.get("message_count")
    source_message_count = summary.get("source_message_count")
    requested_data_count = summary.get("requested_data_count") or "-"
    expected_assets = summary.get("expected_assets") or {}
    actual_assets = summary.get("actual_assets") or {}
    missing_assets = summary.get("missing_assets") or {}
    missing_breakdown = summary.get("missing_breakdown") or {}
    actionable_missing_count = int(summary.get("actionable_missing_count") or 0)
    background_missing_count = int(summary.get("background_missing_count") or 0)
    history_source = str(summary.get("history_source") or "").strip() or "-"
    bulk_partial_fallback = bool(summary.get("bulk_partial_fallback"))
    forward_structure_unavailable_count = int(summary.get("forward_structure_unavailable_count") or 0)

    expected_total = sum(int(value) for value in expected_assets.values())
    actual_total = sum(int(value) for value in actual_assets.values())
    missing_total = sum(int(value) for value in missing_assets.values())
    expired_total = int(missing_breakdown.get("qq_expired_after_napcat") or 0)

    return (
        f"m={message_count}/{source_message_count} "
        f"req={requested_data_count} "
        f"src={history_source} "
        f"history_fallback={'partial' if bulk_partial_fallback else '-'} "
        f"final_a={actual_total}/{expected_total} "
        f"final_miss={missing_total} "
        f"actionable_miss={actionable_missing_count} "
        f"background_miss={background_missing_count} "
        f"fwd_gap={forward_structure_unavailable_count} "
        f"expired_occurrences={expired_total}"
    )


def _build_missing_retry_plan(
    *,
    snapshot: NormalizedSnapshot,
    bundle: ExportBundleResult,
    profile: ExportProfile,
    fmt: str,
    strict_missing: str | None,
) -> dict[str, object] | None:
    missing_assets = [
        asset
        for asset in bundle.assets
        if asset.status == "missing"
        and asset.timestamp_iso
        and str(asset.missing_kind or asset.resolver or "missing") not in BACKGROUND_MISSING_KINDS
    ]
    if not missing_assets:
        return None

    clustered_assets = sorted(
        missing_assets,
        key=lambda asset: (asset.timestamp_iso, asset.message_id or "", asset.file_name or ""),
    )
    clusters: list[list] = []
    current_cluster: list = []
    previous_dt: datetime | None = None
    for asset in clustered_assets:
        try:
            current_dt = datetime.fromisoformat(asset.timestamp_iso)
        except ValueError:
            continue
        if not current_cluster or previous_dt is None or current_dt - previous_dt <= MISSING_RETRY_CLUSTER_GAP:
            current_cluster.append((asset, current_dt))
        else:
            clusters.append(current_cluster)
            current_cluster = [(asset, current_dt)]
        previous_dt = current_dt
    if current_cluster:
        clusters.append(current_cluster)

    if not clusters:
        return None

    chat_type_token = "group" if snapshot.chat_type == "group" else "friend"
    profile_command = PROFILE_COMMANDS.get(profile, "/export")
    fmt_marker = " asTXT" if str(fmt).strip().lower() == "txt" else ""
    strict_suffix = f" --strict-missing {strict_missing}" if strict_missing else ""

    result_clusters: list[dict[str, object]] = []
    for cluster in clusters[:MAX_MISSING_RETRY_HINTS]:
        assets = [asset for asset, _dt in cluster]
        datetimes = [dt for _asset, dt in cluster]
        start_dt = min(datetimes) - MISSING_RETRY_WINDOW_PADDING
        end_dt = max(datetimes) + MISSING_RETRY_WINDOW_PADDING
        start_token = format_export_datetime(start_dt)
        end_token = format_export_datetime(end_dt)
        message_ids = {asset.message_id for asset in assets if asset.message_id}
        asset_types = Counter(asset.asset_type for asset in assets if asset.asset_type)
        missing_kinds = Counter(asset.missing_kind or asset.resolver or "missing" for asset in assets)
        repl_command = (
            f"{profile_command} {chat_type_token} {snapshot.chat_id} "
            f"{start_token} {end_token}{fmt_marker}{strict_suffix}"
        ).strip()
        cli_command = f"run_targeted_missing_retest.bat --only-cluster {len(result_clusters) + 1}".strip()
        result_clusters.append(
            {
                "start_token": start_token,
                "end_token": end_token,
                "asset_count": len(assets),
                "message_count": len(message_ids),
                "asset_types": dict(sorted((str(key), int(value)) for key, value in asset_types.items())),
                "missing_kinds": dict(sorted((str(key), int(value)) for key, value in missing_kinds.items())),
                "repl_command": repl_command,
                "cli_command": cli_command,
            }
        )

    return {
        "cluster_gap_seconds": int(MISSING_RETRY_CLUSTER_GAP.total_seconds()),
        "padding_seconds": int(MISSING_RETRY_WINDOW_PADDING.total_seconds()),
        "cluster_count": len(result_clusters),
        "clusters": result_clusters,
    }


def _rebuild_message(message: NormalizedMessage, segments: list[NormalizedSegment]) -> NormalizedMessage | None:
    if not segments:
        return None

    content_parts: list[str] = []
    text_parts: list[str] = []
    image_file_names: list[str] = []
    uploaded_file_names: list[str] = []
    emoji_tokens: list[str] = []

    for segment in segments:
        token = _segment_content_token(segment)
        if token:
            content_parts.append(token)
        text_value = _segment_text_value(segment)
        if text_value:
            text_parts.append(text_value)
        if segment.type == "image" and segment.file_name:
            image_file_names.append(segment.file_name)
        if segment.type == "file" and segment.file_name:
            uploaded_file_names.append(segment.file_name)
        if segment.type in {"emoji", "sticker"} and (segment.token or token):
            emoji_tokens.append(segment.token or token)

    content = " ".join(part for part in content_parts if part).strip()
    if not content:
        return None
    text_content = " ".join(part for part in text_parts if part).strip()
    return message.model_copy(
        deep=True,
        update={
            "segments": segments,
            "content": content,
            "text_content": text_content,
            "image_file_names": image_file_names,
            "uploaded_file_names": uploaded_file_names,
            "emoji_tokens": emoji_tokens,
        }
    )


def _segment_content_token(segment: NormalizedSegment) -> str:
    if segment.type == "text":
        return segment.text or ""
    if segment.type == "system":
        return segment.text or segment.summary or segment.token or "[system message]"
    if segment.token:
        return segment.token
    if segment.type == "image":
        return f"[image:{segment.file_name or 'image.jpg'}]"
    if segment.type == "file":
        return f"[uploaded_file_name:{segment.file_name or 'uploaded_file'}]"
    if segment.type == "speech":
        return "[speech audio]"
    if segment.type == "video":
        return f"[video:{segment.file_name or 'video'}]"
    if segment.type == "emoji":
        return f"[emoji:id={segment.emoji_id or '?'}]"
    if segment.type == "sticker":
        return segment.summary or "[sticker]"
    if segment.type == "forward":
        preview_text = _segment_text_value(segment)
        return " ".join(part for part in [FORWARD_FALLBACK_TOKEN, preview_text] if part).strip()
    if segment.type == "share":
        summary = _segment_text_value(segment)
        token = f"[share:{segment.summary or '分享卡片'}]"
        return " ".join(part for part in [token, summary] if part).strip()
    if segment.type == "unsupported":
        return segment.token or "[unsupported]"
    return ""


FORWARD_FALLBACK_TOKEN = "[forward message]"


def _segment_text_value(segment: NormalizedSegment) -> str:
    if segment.type in {"text", "system"}:
        return (segment.text or "").strip()
    if segment.type == "forward":
        return str(
            segment.extra.get("detailed_text")
            or segment.extra.get("preview_text")
            or segment.summary
            or ""
        ).strip()
    if segment.type == "share":
        parts = [
            segment.summary,
            str(segment.extra.get("desc") or "").strip() or None,
            str(segment.extra.get("tag") or "").strip() or None,
        ]
        return " ".join(part for part in parts if part).strip()
    if segment.type == "reply":
        reply_text = (
            segment.text
            or str(segment.extra.get("reply_text") or "").strip()
            or ""
        )
        return reply_text.strip()
    return ""


def _segment_asset_keys(segment: NormalizedSegment) -> list[str]:
    if segment.type == "image":
        return ["image"]
    if segment.type == "file":
        return ["file"]
    if segment.type == "speech":
        return ["speech"]
    if segment.type == "video":
        return ["video"]
    if segment.type == "sticker":
        keys: list[str] = []
        static_path = str(segment.extra.get("static_path") or "").strip()
        dynamic_path = str(segment.extra.get("dynamic_path") or "").strip()
        if static_path:
            keys.append("sticker.static")
        if dynamic_path:
            keys.append("sticker.dynamic")
        if segment.path:
            keys.append("sticker")
        return keys
    return []


def _collect_segment_asset_keys(segment: NormalizedSegment) -> list[str]:
    keys = list(_segment_asset_keys(segment))
    if segment.type != "forward":
        return keys

    forward_messages = segment.extra.get("forward_messages")
    if not isinstance(forward_messages, list):
        return keys

    for forwarded in forward_messages:
        if not isinstance(forwarded, dict):
            continue
        for child in forwarded.get("segments") or []:
            try:
                normalized_child = (
                    child if isinstance(child, NormalizedSegment) else NormalizedSegment.model_validate(child)
                )
            except Exception:
                continue
            keys.extend(_collect_segment_asset_keys(normalized_child))
    return keys


def _asset_key(asset_type: str, asset_role: str | None) -> str:
    if asset_role:
        return f"{asset_type}.{asset_role}"
    return asset_type


def _format_counter(value: object) -> str:
    counter = value if isinstance(value, dict) else {}
    if not counter:
        return "-"
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter))


def _format_counter_mapping(value: object) -> str:
    counter = value if isinstance(value, dict) else {}
    if not counter:
        return "-"
    return ", ".join(f"{key}:{int(counter[key])}" for key in counter)
