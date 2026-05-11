from __future__ import annotations

import re
from os import fspath
from typing import TextIO

from rich.text import Text

from qq_data_cli.terminal_compat import probe_terminal_environment


_STATUS_FIELD_RE = re.compile(
    r"(?P<prefix>(?<![\w])(?:status|export_status)=)(?P<value>success|failed|in progress)\b",
    flags=re.IGNORECASE,
)

_ANSI_STATUS_COLORS = {
    "success": "\x1b[32m",
    "failed": "\x1b[31m",
    "in progress": "\x1b[33m",
}

_RICH_STATUS_STYLES = {
    "success": "green",
    "failed": "red",
    "in progress": "yellow",
}

_ANSI_RESET = "\x1b[0m"


def colorize_status_fields_for_ansi(
    text: str,
    *,
    stream: TextIO | None = None,
) -> str:
    if not text or not _supports_ansi_status_color(stream=stream):
        return text
    return _STATUS_FIELD_RE.sub(_ansi_status_replacement, text)


def build_rich_status_text(text: str) -> Text:
    result = Text()
    if not text:
        return result
    cursor = 0
    for match in _STATUS_FIELD_RE.finditer(text):
        value = match.group("value")
        style = _RICH_STATUS_STYLES.get(value.casefold())
        if cursor < match.start("value"):
            result.append(text[cursor : match.start("value")])
        result.append(value, style=style)
        cursor = match.end("value")
    if cursor < len(text):
        result.append(text[cursor:])
    if not result:
        result.append(text)
    return result


def format_export_result_lines(
    *,
    session_line: str | None,
    content_summary: dict[str, object],
    bundle,
    trace_summary: dict[str, object],
    trace_path,
) -> list[str]:
    lines: list[str] = ["export_result:"]
    export_status = _derive_export_status(content_summary)
    export_verdict = _derive_export_verdict(content_summary)
    lines.append(f"  export_status={export_status} export_verdict={export_verdict}")

    normalized_session = _normalize_export_session_line(session_line)
    if normalized_session:
        lines.append(f"  session={normalized_session}")

    lines.append("  files:")
    lines.append(f"    data={fspath(bundle.data_path)}")
    lines.append(f"    manifest={fspath(bundle.manifest_path)}")
    lines.append(f"    trace={fspath(trace_path)}")

    lines.append("  summary:")
    elapsed = trace_summary.get("elapsed_s")
    elapsed_text = f"{float(elapsed):.3f}s" if isinstance(elapsed, (int, float)) else "-"
    lines.append(
        "    "
        f"records={bundle.record_count} "
        f"elapsed={elapsed_text} "
        f"pages={int(trace_summary.get('pages_scanned') or 0)} "
        f"retries={int(trace_summary.get('retry_events') or 0)}"
    )
    oldest = str(content_summary.get("oldest_timestamp_iso") or "").strip()
    latest = str(content_summary.get("latest_timestamp_iso") or "").strip()
    if oldest and latest:
        lines.append(f"    time_range={oldest} -> {latest}")
    history_source = str(content_summary.get("history_source") or "-").strip() or "-"
    history_fallback = "partial" if bool(content_summary.get("bulk_partial_fallback")) else "-"
    lines.append(
        "    "
        f"history_source={history_source} "
        f"history_fallback={history_fallback} "
        f"forward_detail_count={int(content_summary.get('forward_detail_count') or 0)} "
        f"fwd_gap={int(content_summary.get('forward_structure_unavailable_count') or 0)}"
    )
    prefetch_chunk_count = int(trace_summary.get("prefetch_chunk_count") or 0)
    if prefetch_chunk_count > 0:
        lines.append(
            "    "
            f"prefetch_chunks={prefetch_chunk_count} "
            f"avg_prefetch_chunk={float(trace_summary.get('average_prefetch_chunk_s') or 0.0):.1f}s "
            f"slowest_prefetch_chunk={float(trace_summary.get('slowest_prefetch_chunk_s') or 0.0):.1f}s "
            f"prefetch_timeout_count={int(trace_summary.get('prefetch_timeout_count') or 0)} "
            f"prefetch_degraded={'yes' if bool(trace_summary.get('prefetch_degraded')) else 'no'}"
        )

    expected_assets = content_summary.get("expected_assets") or {}
    actual_assets = content_summary.get("actual_assets") or {}
    missing_assets = content_summary.get("missing_assets") or {}
    actual_total = sum(int(value) for value in actual_assets.values())
    expected_total = sum(int(value) for value in expected_assets.values())
    final_missing = sum(int(value) for value in missing_assets.values())
    lines.append("  assets:")
    lines.append(
        "    "
        f"final_assets={actual_total}/{expected_total} "
        f"copied={int(getattr(bundle, 'copied_asset_count', 0) or 0)} "
        f"reused={int(getattr(bundle, 'reused_asset_count', 0) or 0)} "
        f"missing={final_missing}"
    )
    actionable_missing_count = int(content_summary.get("actionable_missing_count") or 0)
    background_missing_count = int(content_summary.get("background_missing_count") or 0)
    lines.append(
        "    "
        f"actionable_missing={actionable_missing_count} "
        f"background_missing={background_missing_count}"
    )

    missing_breakdown = _format_counter_mapping(content_summary.get("missing_breakdown") or {})
    actionable_breakdown = _format_counter_mapping(
        content_summary.get("actionable_missing_breakdown") or {}
    )
    background_breakdown = _format_counter_mapping(
        content_summary.get("background_missing_breakdown") or {}
    )
    if missing_breakdown:
        lines.append(f"    final_missing_reason=[{missing_breakdown}]")
    if actionable_breakdown:
        lines.append(f"    actionable_missing_reason=[{actionable_breakdown}]")
    if background_breakdown:
        lines.append(f"    background_missing_reason=[{background_breakdown}]")

    if actionable_missing_count > 0:
        lines.append(
            "  note: 当前导出已完成，但仍有可行动 missing；请优先查看 "
            "actionable_missing_reason 和 trace。"
        )
    elif final_missing and background_missing_count > 0:
        lines.append(
            "  note: 当前剩余 missing 均为背景缺失（placeholder / expired 类），"
            "当前导出链本身未发现新的可行动缺口。"
        )
    return lines


def format_prefetch_media_progress_line(
    update: dict[str, object],
    *,
    prefix: str = "",
) -> str | None:
    phase = str(update.get("phase") or "")
    elapsed_s = float(update.get("elapsed_s") or 0.0)

    if phase == "prefetch_media":
        stage = str(update.get("stage") or "start")
        request_count = int(update.get("request_count") or 0)
        if stage == "done":
            rate = f" rate={request_count / elapsed_s:.1f}/s" if elapsed_s > 0 and request_count > 0 else ""
            return (
                f"status=success {prefix}export_progress: prefetched media context "
                f"requests={request_count} elapsed={elapsed_s:.1f}s{rate}"
            ).strip()
        if stage == "error":
            reason = str(update.get("reason") or "").strip()
            error = str(update.get("error") or "").strip()
            timeout_s = float(update.get("timeout_s") or 0.0)
            budget_s = float(update.get("budget_s") or 0.0)
            suffix_parts: list[str] = []
            if reason:
                suffix_parts.append(f"reason={reason}")
            if timeout_s > 0:
                suffix_parts.append(f"timeout={timeout_s:.1f}s")
            if budget_s > 0:
                suffix_parts.append(f"budget={budget_s:.1f}s")
            if error and not reason:
                suffix_parts.append(f"detail={error}")
            suffix = f" {' '.join(suffix_parts)}" if suffix_parts else ""
            return (
                f"status=failed {prefix}export_progress: media prefetch degraded "
                f"requests={request_count} elapsed={elapsed_s:.1f}s{suffix}"
            ).strip()
        return (
            f"status=in progress {prefix}export_progress: prefetching media context "
            f"requests={request_count}"
        ).strip()

    if phase == "prefetch_media_prepare":
        stage = str(update.get("stage") or "start")
        overall_request_count = int(update.get("overall_request_count") or 0)
        scanned_request_count = int(update.get("scanned_request_count") or 0)
        context_request_count = int(update.get("context_request_count") or 0)
        prefetched_local_count = int(update.get("prefetched_local_count") or 0)
        skipped_old_bucket_count = int(update.get("skipped_old_bucket_count") or 0)
        prepare_elapsed_s = float(update.get("elapsed_s") or 0.0)
        scanned_label = (
            f"{scanned_request_count}/{overall_request_count}"
            if overall_request_count > 0
            else str(scanned_request_count)
        )
        detail = (
            f"context={context_request_count} local={prefetched_local_count} "
            f"skip_old={skipped_old_bucket_count}"
        )
        rate = (
            f" rate={scanned_request_count / prepare_elapsed_s:.1f}/s elapsed={prepare_elapsed_s:.1f}s"
            if prepare_elapsed_s > 0 and scanned_request_count > 0
            else ""
        )
        if stage == "done":
            return (
                f"status=success {prefix}export_progress: planned media prefetch "
                f"scanned={scanned_label} {detail}{rate}"
            ).strip()
        if stage == "progress":
            return (
                f"status=in progress {prefix}export_progress: planning media prefetch "
                f"scanned={scanned_label} {detail}{rate}"
            ).strip()
        return (
            f"status=in progress {prefix}export_progress: planning media prefetch "
            f"scanned={scanned_label} {detail}{rate}"
        ).strip()

    if phase != "prefetch_media_chunk":
        return None

    stage = str(update.get("stage") or "start")
    chunk_index = int(update.get("chunk_index") or 0)
    chunk_count = int(update.get("chunk_count") or 0)
    chunk_size = int(update.get("request_count") or 0)
    total_request_count = int(update.get("total_request_count") or 0)
    overall_request_count = int(update.get("overall_request_count") or 0)
    processed_request_count = int(update.get("processed_request_count") or 0)
    hydrated_count = int(update.get("hydrated_count") or 0)
    reason = str(update.get("reason") or "").strip()

    if total_request_count <= 0:
        total_request_count = max(processed_request_count, chunk_size)
    progress_label = f"{processed_request_count}/{total_request_count}" if total_request_count > 0 else str(processed_request_count)
    chunk_label = f"{chunk_index}/{chunk_count}" if chunk_count > 0 else str(chunk_index)
    chunk_elapsed = elapsed_s if elapsed_s > 0 else 0.0
    rate = (
        f" rate={chunk_size / chunk_elapsed:.1f}/s"
        if chunk_elapsed > 0 and chunk_size > 0 and stage in {"done", "error"}
        else ""
    )

    if stage == "done":
        total_suffix = f" total={overall_request_count}" if overall_request_count > total_request_count > 0 else ""
        return (
            f"status=in progress {prefix}export_progress: prefetch chunk={chunk_label} "
            f"context={progress_label}{total_suffix} batch={chunk_size} hydrated={hydrated_count} "
            f"chunk_elapsed={chunk_elapsed:.1f}s{rate}"
        ).strip()
    if stage == "error":
        reason_suffix = f" reason={reason}" if reason else ""
        timeout_s = float(update.get("timeout_s") or 0.0)
        timeout_suffix = f" timeout={timeout_s:.1f}s" if timeout_s > 0 else ""
        total_suffix = f" total={overall_request_count}" if overall_request_count > total_request_count > 0 else ""
        return (
            f"status=failed {prefix}export_progress: prefetch chunk={chunk_label} "
            f"context={progress_label}{total_suffix} batch={chunk_size} "
            f"chunk_elapsed={chunk_elapsed:.1f}s{reason_suffix}{timeout_suffix}"
        ).strip()
    total_suffix = f" total={overall_request_count}" if overall_request_count > total_request_count > 0 else ""
    return (
        f"status=in progress {prefix}export_progress: prefetch chunk={chunk_label} "
        f"context={progress_label}{total_suffix} batch={chunk_size}"
    ).strip()


def _ansi_status_replacement(match: re.Match[str]) -> str:
    value = match.group("value")
    color = _ANSI_STATUS_COLORS.get(value.casefold())
    if not color:
        return match.group(0)
    return f"{match.group('prefix')}{color}{value}{_ANSI_RESET}"


def _derive_export_verdict(summary: dict[str, object]) -> str:
    actionable_missing_count = int(summary.get("actionable_missing_count") or 0)
    background_missing_count = int(summary.get("background_missing_count") or 0)
    missing_assets = summary.get("missing_assets") or {}
    final_missing = sum(int(value) for value in missing_assets.values())

    if final_missing <= 0:
        return "success"
    if actionable_missing_count > 0:
        return "success_with_actionable_missing"
    if background_missing_count > 0:
        return "success_with_background_missing"
    return "success_with_unclassified_missing"


def _derive_export_status(summary: dict[str, object]) -> str:
    _ = summary
    return "success"


def _format_counter_mapping(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    parts: list[str] = []
    for key, raw in value.items():
        try:
            count = int(raw)
        except (TypeError, ValueError):
            continue
        parts.append(f"{key}:{count}")
    return ", ".join(parts)


def _normalize_export_session_line(session_line: str | None) -> str | None:
    text = str(session_line or "").strip()
    if not text:
        return None
    prefix = "export_session:"
    if text.startswith(prefix):
        return text[len(prefix) :].strip()
    return text


def _supports_ansi_status_color(*, stream: TextIO | None = None) -> bool:
    target_stream = stream
    if target_stream is not None and not bool(getattr(target_stream, "isatty", lambda: False)()):
        return False
    probe = probe_terminal_environment(stdout=target_stream)
    if not probe.stdout_tty:
        return False
    if probe.platform_system != "Windows":
        return True
    if probe.virtual_terminal_enabled:
        return True
    return probe.wt_session or probe.vscode_terminal or probe.ansicon_present
