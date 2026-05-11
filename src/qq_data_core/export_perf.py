from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from time import perf_counter

from .models import EXPORT_TIMEZONE
from .paths import build_timestamp_token

MATERIALIZE_SLOW_STEP_WARN_S = 5.0
TOP_PERF_EVENT_LIMIT = 20
SCAN_PHASE_KINDS = {
    "bounds_scan",
    "interval_scan",
    "interval_tail_scan",
    "tail_scan",
    "full_scan",
}


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(EXPORT_TIMEZONE).isoformat()
    return str(value)


def _compact_payload(payload: dict[str, Any], *, omit: set[str] | None = None) -> dict[str, Any]:
    omitted = omit or set()
    compact: dict[str, Any] = {}
    for key, value in payload.items():
        if key in omitted:
            continue
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, float):
            compact[key] = round(value, 4)
            continue
        compact[key] = value
    return compact


def _update_timing_bucket(
    bucket: dict[str, Any],
    *,
    elapsed_s: float,
    payload: dict[str, Any],
    max_payload_omit: set[str] | None = None,
) -> None:
    bucket["count"] = int(bucket.get("count") or 0) + 1
    bucket["total_s"] = float(bucket.get("total_s") or 0.0) + elapsed_s
    current_max = float(bucket.get("max_s") or 0.0)
    if elapsed_s >= current_max:
        bucket["max_s"] = elapsed_s
        bucket["max_payload"] = _compact_payload(payload, omit=max_payload_omit)


def _summarize_timing_bucket(name: str, bucket: dict[str, Any]) -> dict[str, Any]:
    count = int(bucket.get("count") or 0)
    total_s = float(bucket.get("total_s") or 0.0)
    max_s = float(bucket.get("max_s") or 0.0)
    average_s = total_s / count if count else 0.0
    result = {
        "name": name,
        "count": count,
        "total_s": round(total_s, 4),
        "average_s": round(average_s, 4),
        "max_s": round(max_s, 4),
    }
    if bucket.get("errors"):
        result["errors"] = int(bucket.get("errors") or 0)
    if "bytes_total" in bucket:
        result["bytes_total"] = int(bucket.get("bytes_total") or 0)
        if total_s > 0:
            result["throughput_mib_s"] = round(
                int(bucket.get("bytes_total") or 0) / total_s / (1024 * 1024),
                4,
            )
    if "same_volume_count" in bucket:
        result["same_volume_count"] = int(bucket.get("same_volume_count") or 0)
    if "cross_volume_count" in bucket:
        result["cross_volume_count"] = int(bucket.get("cross_volume_count") or 0)
    if bucket.get("max_payload"):
        result["max_payload"] = bucket["max_payload"]
    return result


def _append_top_event(
    entries: list[dict[str, Any]],
    *,
    elapsed_s: float,
    limit: int = TOP_PERF_EVENT_LIMIT,
    **payload: Any,
) -> None:
    event = {
        **payload,
        "elapsed_s": round(elapsed_s, 4),
        "elapsed_ms": int(round(elapsed_s * 1000)),
    }
    entries.append(event)
    entries.sort(key=lambda item: float(item.get("elapsed_s") or 0.0), reverse=True)
    if len(entries) > limit:
        del entries[limit:]


class _TimedStage:
    def __init__(
        self,
        writer: "ExportPerfTraceWriter",
        stage: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self._writer = writer
        self._stage = stage
        self._payload = dict(payload or {})
        self._started = 0.0
        self._extra: dict[str, Any] = {}

    def add(self, **payload: Any) -> None:
        self._extra.update(payload)

    def __enter__(self) -> "_TimedStage":
        self._started = perf_counter()
        self._writer.write_event(
            "pipeline_stage",
            {
                "stage": self._stage,
                "status": "start",
                **self._payload,
            },
        )
        return self

    def __exit__(self, exc_type, exc, _tb) -> bool:
        elapsed_s = perf_counter() - self._started
        payload = {
            "stage": self._stage,
            "elapsed_s": round(elapsed_s, 4),
            **self._payload,
            **self._extra,
        }
        if exc is not None:
            payload["status"] = "error"
            payload["error"] = str(exc)
        else:
            payload["status"] = "done"
        self._writer.write_event("pipeline_stage", payload)
        return False


class ExportPerfTraceWriter:
    def __init__(
        self,
        state_dir: Path,
        *,
        chat_type: str,
        chat_id: str,
        mode: str,
    ) -> None:
        self._lock = Lock()
        export_perf_dir = state_dir / "export_perf"
        export_perf_dir.mkdir(parents=True, exist_ok=True)
        stamp = build_timestamp_token(include_pid=True)
        self.path = export_perf_dir / f"{mode}_{chat_type}_{chat_id}_{stamp}.jsonl"
        self._temp_path = self.path.with_name(f"{self.path.name}.tmp")
        self._handle = self._temp_path.open("a", encoding="utf-8", newline="\n")
        self._started_at = datetime.now(EXPORT_TIMEZONE)
        self._pages_scanned = 0
        self._retry_events = 0
        self._page_time_sum = 0.0
        self._slowest_page_s = 0.0
        self._last_record_count = 0
        self._materialize_step_count = 0
        self._materialize_step_time_sum = 0.0
        self._slowest_materialize_step_s = 0.0
        self._slowest_materialize_step: dict[str, Any] | None = None
        self._prefetch_chunk_count = 0
        self._prefetch_chunk_time_sum = 0.0
        self._slowest_prefetch_chunk_s = 0.0
        self._prefetch_timeout_count = 0
        self._prefetch_degraded = False
        self._stage_buckets: dict[str, dict[str, Any]] = {}
        self._history_page_buckets: dict[str, dict[str, Any]] = {}
        self._scan_phase_buckets: dict[str, dict[str, Any]] = {}
        self._scan_summaries: list[dict[str, Any]] = []
        self._page_size_adapt_events: list[dict[str, Any]] = []
        self._forward_expand_runs: list[dict[str, Any]] = []
        self._tail_bulk_chunk_breakdown: list[dict[str, Any]] = []
        self._tail_forward_hydrate_windows: list[dict[str, Any]] = []
        self._top_materialize_steps: list[dict[str, Any]] = []
        self._top_materialize_substeps: list[dict[str, Any]] = []
        self._substep_buckets: dict[str, dict[str, Any]] = {}
        self._materialize_asset_buckets: dict[str, dict[str, Any]] = {}
        self._materialize_stage_buckets: dict[str, dict[str, Any]] = {}
        self._copy_io_buckets: dict[str, dict[str, Any]] = {}
        self._closed = False
        self.report_path = self.path.with_suffix(".report.json")

    def timed_stage(
        self,
        stage: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> _TimedStage:
        return _TimedStage(self, stage, payload)

    def write_event(self, kind: str, payload: dict[str, Any]) -> None:
        event = {
            "timestamp": datetime.now(EXPORT_TIMEZONE),
            "kind": kind,
            **payload,
        }
        with self._lock:
            if self._closed:
                return
            self._observe_event(kind, payload)
            if not self._should_persist_event(kind, payload):
                return
            self._handle.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    default=_json_default,
                )
                + "\n"
            )
            if self._should_flush_event(kind, payload):
                self._handle.flush()

    def _observe_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "write_data_file":
            stage = "bundle.write_data_file"
            status = str(payload.get("stage") or "").strip() or "done"
            bucket = self._stage_buckets.setdefault(
                stage,
                {"count": 0, "total_s": 0.0, "max_s": 0.0, "errors": 0},
            )
            if status == "error":
                bucket["errors"] = int(bucket.get("errors") or 0) + 1
            if status in {"done", "error"}:
                _update_timing_bucket(
                    bucket,
                    elapsed_s=float(payload.get("elapsed_s") or 0.0),
                    payload=payload,
                    max_payload_omit={"stage", "elapsed_s"},
                )
            return
        if kind == "pipeline_stage":
            stage = str(payload.get("stage") or "").strip() or "unknown"
            status = str(payload.get("status") or "").strip() or "done"
            bucket = self._stage_buckets.setdefault(
                stage,
                {"count": 0, "total_s": 0.0, "max_s": 0.0, "errors": 0},
            )
            if status == "error":
                bucket["errors"] = int(bucket.get("errors") or 0) + 1
            if status in {"done", "error"}:
                _update_timing_bucket(
                    bucket,
                    elapsed_s=float(payload.get("elapsed_s") or 0.0),
                    payload=payload,
                    max_payload_omit={"status", "elapsed_s", "stage"},
                )
            return
        if kind in SCAN_PHASE_KINDS:
            self._pages_scanned = max(self._pages_scanned, int(payload.get("pages_scanned") or 0))
            page_duration_s = float(payload.get("page_duration_s") or 0.0)
            self._page_time_sum += page_duration_s
            self._slowest_page_s = max(self._slowest_page_s, page_duration_s)
            self._last_record_count = max(
                self._last_record_count,
                int(payload.get("collected_messages") or payload.get("matched_messages") or 0),
            )
            return
        if kind == "page_retry":
            self._retry_events += 1
            return
        if kind == "history_page_done":
            mode = str(payload.get("mode") or "").strip() or "unknown"
            history_source = str(payload.get("history_source") or "").strip() or "unknown"
            self._pages_scanned = max(self._pages_scanned, int(payload.get("pages_scanned") or 0))
            bucket_name = f"{mode}:{history_source}"
            bucket = self._history_page_buckets.setdefault(
                bucket_name,
                {"count": 0, "total_s": 0.0, "max_s": 0.0, "page_messages": 0},
            )
            elapsed_s = float(payload.get("page_duration_s") or 0.0)
            _update_timing_bucket(
                bucket,
                elapsed_s=elapsed_s,
                payload=payload,
                max_payload_omit={"page_duration_s"},
            )
            bucket["page_messages"] = int(bucket.get("page_messages") or 0) + int(
                payload.get("page_message_count") or 0
            )
            return
        if kind == "scan_summary":
            scan_phase = str(payload.get("scan_phase") or "").strip() or "unknown"
            bucket = self._scan_phase_buckets.setdefault(
                scan_phase,
                {"count": 0, "total_s": 0.0, "max_s": 0.0, "errors": 0},
            )
            _update_timing_bucket(
                bucket,
                elapsed_s=float(payload.get("elapsed_s") or 0.0),
                payload=payload,
                max_payload_omit={"elapsed_s", "elapsed_ms", "scan_phase"},
            )
            self._scan_summaries.append(_compact_payload(payload))
            return
        if kind == "page_size_adapt":
            self._page_size_adapt_events.append(_compact_payload(payload))
            if len(self._page_size_adapt_events) > TOP_PERF_EVENT_LIMIT:
                del self._page_size_adapt_events[:-TOP_PERF_EVENT_LIMIT]
            return
        if kind == "forward_expand_summary":
            self._forward_expand_runs.append(_compact_payload(payload))
            if len(self._forward_expand_runs) > TOP_PERF_EVENT_LIMIT:
                del self._forward_expand_runs[:-TOP_PERF_EVENT_LIMIT]
            return
        if kind == "tail_bulk_chunk" and str(payload.get("status") or "") == "done":
            self._tail_bulk_chunk_breakdown.append(_compact_payload(payload))
            if len(self._tail_bulk_chunk_breakdown) > TOP_PERF_EVENT_LIMIT:
                del self._tail_bulk_chunk_breakdown[:-TOP_PERF_EVENT_LIMIT]
            return
        if kind == "tail_forward_hydrate_window" and str(payload.get("status") or "") == "done":
            self._tail_forward_hydrate_windows.append(_compact_payload(payload))
            if len(self._tail_forward_hydrate_windows) > 100:
                del self._tail_forward_hydrate_windows[:-100]
            return
        if kind == "prefetch_media_chunk":
            stage = str(payload.get("stage") or "")
            if stage in {"done", "error"}:
                chunk_elapsed_s = float(payload.get("elapsed_s") or 0.0)
                self._prefetch_chunk_count += 1
                self._prefetch_chunk_time_sum += chunk_elapsed_s
                self._slowest_prefetch_chunk_s = max(self._slowest_prefetch_chunk_s, chunk_elapsed_s)
                if str(payload.get("reason") or "") == "chunk_timeout":
                    self._prefetch_timeout_count += 1
            return
        if kind == "prefetch_media" and str(payload.get("stage") or "") == "error":
            self._prefetch_degraded = True
            return
        if kind == "materialize_asset_step" and str(payload.get("stage") or "") == "done":
            step_elapsed_s = float(payload.get("step_elapsed_s") or 0.0)
            self._materialize_step_count += 1
            self._materialize_step_time_sum += step_elapsed_s
            asset_type = str(payload.get("asset_type") or "").strip() or "unknown"
            asset_role = str(payload.get("asset_role") or "").strip() or "unknown"
            status = str(payload.get("status") or "").strip() or "unknown"
            resolver = str(payload.get("resolver") or "").strip() or "-"
            missing_kind = str(payload.get("missing_kind") or "").strip() or "-"
            bucket_name = (
                f"{asset_type}:{asset_role}:status={status}:resolver={resolver}:missing={missing_kind}"
            )
            asset_bucket = self._materialize_asset_buckets.setdefault(
                bucket_name,
                {
                    "count": 0,
                    "total_s": 0.0,
                    "max_s": 0.0,
                    "errors": 0,
                    "asset_type": asset_type,
                    "asset_role": asset_role,
                    "status": status,
                    "resolver": resolver,
                    "missing_kind": missing_kind,
                },
            )
            if status == "error":
                asset_bucket["errors"] = int(asset_bucket.get("errors") or 0) + 1
            _update_timing_bucket(
                asset_bucket,
                elapsed_s=step_elapsed_s,
                payload=payload,
                max_payload_omit={"step_elapsed_s", "step_elapsed_ms"},
            )
            _append_top_event(
                self._top_materialize_steps,
                elapsed_s=step_elapsed_s,
                current=int(payload.get("current") or 0),
                total=int(payload.get("total") or 0),
                asset_type=payload.get("asset_type"),
                asset_role=payload.get("asset_role"),
                file_name=payload.get("file_name"),
                timestamp_iso=payload.get("timestamp_iso"),
                message_id_raw=payload.get("message_id_raw"),
                element_id=payload.get("element_id"),
                status=payload.get("status"),
                resolver=payload.get("resolver"),
                missing_kind=payload.get("missing_kind"),
                md5=payload.get("md5"),
                source_path=payload.get("source_path"),
                source_path_kind=payload.get("source_path_kind"),
                hint_url=payload.get("hint_url"),
                hint_url_kind=payload.get("hint_url_kind"),
                resolved_source_path=payload.get("resolved_source_path"),
            )
            if step_elapsed_s >= self._slowest_materialize_step_s:
                self._slowest_materialize_step_s = step_elapsed_s
                self._slowest_materialize_step = {
                    "current": int(payload.get("current") or 0),
                    "asset_type": payload.get("asset_type"),
                    "asset_role": payload.get("asset_role"),
                    "file_name": payload.get("file_name"),
                    "timestamp_iso": payload.get("timestamp_iso"),
                    "message_id_raw": payload.get("message_id_raw"),
                    "element_id": payload.get("element_id"),
                    "status": payload.get("status"),
                    "resolver": payload.get("resolver"),
                    "missing_kind": payload.get("missing_kind"),
                    "md5": payload.get("md5"),
                    "source_path": payload.get("source_path"),
                    "source_path_kind": payload.get("source_path_kind"),
                    "hint_url": payload.get("hint_url"),
                    "hint_url_kind": payload.get("hint_url_kind"),
                    "resolved_source_path": payload.get("resolved_source_path"),
                }
            return
        if kind == "materialize_asset_substep":
            substep = str(payload.get("substep") or "").strip() or "unknown"
            elapsed_s = float(payload.get("elapsed_s") or 0.0)
            if elapsed_s <= 0:
                return
            bucket = self._substep_buckets.setdefault(
                substep,
                {"count": 0, "total_s": 0.0, "max_s": 0.0, "errors": 0},
            )
            status = str(payload.get("status") or "").strip()
            if status in {"error", "timeout"}:
                bucket["errors"] = int(bucket.get("errors") or 0) + 1
            _update_timing_bucket(
                bucket,
                elapsed_s=elapsed_s,
                payload=payload,
                max_payload_omit={"elapsed_s", "elapsed_ms"},
            )
            asset_type = str(payload.get("asset_type") or "").strip() or "unknown"
            stage_bucket_name = f"{asset_type}:{substep}:status={status or 'unknown'}"
            stage_bucket = self._materialize_stage_buckets.setdefault(
                stage_bucket_name,
                {
                    "count": 0,
                    "total_s": 0.0,
                    "max_s": 0.0,
                    "errors": 0,
                    "asset_type": asset_type,
                    "substep": substep,
                    "status": status or "unknown",
                },
            )
            if status in {"error", "timeout", "cached_error"}:
                stage_bucket["errors"] = int(stage_bucket.get("errors") or 0) + 1
            _update_timing_bucket(
                stage_bucket,
                elapsed_s=elapsed_s,
                payload=payload,
                max_payload_omit={"elapsed_s", "elapsed_ms"},
            )
            if substep in {"copy_asset_file", "second_pass_copy_asset_file"}:
                same_volume = bool(payload.get("same_volume"))
                source_drive = str(payload.get("source_drive") or "").strip() or "-"
                target_drive = str(payload.get("target_drive") or "").strip() or "-"
                resolver = str(payload.get("resolver") or "").strip() or "-"
                source_size_bytes = int(payload.get("source_size_bytes") or 0)
                size_bucket = _size_bucket_label(source_size_bytes)
                copy_bucket_name = (
                    f"{asset_type}:{substep}:resolver={resolver}:same_volume={same_volume}:"
                    f"size_bucket={size_bucket}:{source_drive}->{target_drive}"
                )
                copy_bucket = self._copy_io_buckets.setdefault(
                    copy_bucket_name,
                    {
                        "count": 0,
                        "total_s": 0.0,
                        "max_s": 0.0,
                        "errors": 0,
                        "asset_type": asset_type,
                        "substep": substep,
                        "resolver": resolver,
                        "same_volume": same_volume,
                        "source_drive": source_drive,
                        "target_drive": target_drive,
                        "size_bucket": size_bucket,
                        "bytes_total": 0,
                        "same_volume_count": 0,
                        "cross_volume_count": 0,
                    },
                )
                if status in {"error", "timeout", "cached_error"}:
                    copy_bucket["errors"] = int(copy_bucket.get("errors") or 0) + 1
                copy_bucket["bytes_total"] = int(copy_bucket.get("bytes_total") or 0) + source_size_bytes
                if same_volume:
                    copy_bucket["same_volume_count"] = int(copy_bucket.get("same_volume_count") or 0) + 1
                else:
                    copy_bucket["cross_volume_count"] = int(copy_bucket.get("cross_volume_count") or 0) + 1
                _update_timing_bucket(
                    copy_bucket,
                    elapsed_s=elapsed_s,
                    payload=payload,
                    max_payload_omit={"elapsed_s", "elapsed_ms"},
                )
            _append_top_event(
                self._top_materialize_substeps,
                elapsed_s=elapsed_s,
                substep=substep,
                status=status,
                asset_type=payload.get("asset_type"),
                asset_role=payload.get("asset_role"),
                file_name=payload.get("file_name"),
                message_id_raw=payload.get("message_id_raw"),
                element_id=payload.get("element_id"),
                forward_parent_message_id_raw=payload.get("forward_parent_message_id_raw"),
                detail=payload.get("detail"),
                hint_file_id=payload.get("hint_file_id"),
                hint_url=payload.get("hint_url"),
                timestamp_iso=payload.get("timestamp_iso"),
                source_path_kind=payload.get("source_path_kind"),
                hint_url_kind=payload.get("hint_url_kind"),
            )

    def _should_persist_event(self, kind: str, payload: dict[str, Any]) -> bool:
        if kind != "materialize_asset_step":
            return True
        return str(payload.get("stage") or "") == "done"

    def _should_flush_event(self, kind: str, payload: dict[str, Any]) -> bool:
        if kind != "materialize_asset_step":
            return True
        step_elapsed_s = float(payload.get("step_elapsed_s") or 0.0)
        return step_elapsed_s >= MATERIALIZE_SLOW_STEP_WARN_S

    def _build_summary_unlocked(self, *, record_count: int | None = None) -> dict[str, Any]:
        elapsed_s = (datetime.now(EXPORT_TIMEZONE) - self._started_at).total_seconds()
        average_page_s = self._page_time_sum / self._pages_scanned if self._pages_scanned else 0.0
        average_materialize_step_s = (
            self._materialize_step_time_sum / self._materialize_step_count
            if self._materialize_step_count
            else 0.0
        )
        average_prefetch_chunk_s = (
            self._prefetch_chunk_time_sum / self._prefetch_chunk_count
            if self._prefetch_chunk_count
            else 0.0
        )
        return {
            "started_at": self._started_at.isoformat(),
            "elapsed_s": round(elapsed_s, 3),
            "total_elapsed_s": round(elapsed_s, 3),
            "pages_scanned": self._pages_scanned,
            "retry_events": self._retry_events,
            "average_page_s": round(average_page_s, 4),
            "slowest_page_s": round(self._slowest_page_s, 4),
            "prefetch_chunk_count": self._prefetch_chunk_count,
            "average_prefetch_chunk_s": round(average_prefetch_chunk_s, 4),
            "slowest_prefetch_chunk_s": round(self._slowest_prefetch_chunk_s, 4),
            "prefetch_timeout_count": self._prefetch_timeout_count,
            "prefetch_degraded": self._prefetch_degraded,
            "materialize_step_count": self._materialize_step_count,
            "average_materialize_step_s": round(average_materialize_step_s, 4),
            "slowest_materialize_step_s": round(self._slowest_materialize_step_s, 4),
            "slowest_materialize_step": self._slowest_materialize_step,
            "record_count": self._last_record_count if record_count is None else record_count,
            "stage_breakdown": [
                _summarize_timing_bucket(name, bucket)
                for name, bucket in sorted(
                    self._stage_buckets.items(),
                    key=lambda item: float(item[1].get("total_s") or 0.0),
                    reverse=True,
                )[:10]
            ],
        }

    def build_summary(self, *, record_count: int | None = None) -> dict[str, Any]:
        with self._lock:
            return self._build_summary_unlocked(record_count=record_count)

    def build_report(self, *, record_count: int | None = None) -> dict[str, Any]:
        with self._lock:
            summary = self._build_summary_unlocked(record_count=record_count)
            stage_breakdown = [
                _summarize_timing_bucket(name, bucket)
                for name, bucket in sorted(
                    self._stage_buckets.items(),
                    key=lambda item: float(item[1].get("total_s") or 0.0),
                    reverse=True,
                )
            ]
            fetch_stage_breakdown = [
                row
                for row in stage_breakdown
                if row["name"].startswith("app.fetch_")
                or row["name"].startswith("provider.fetch_")
                or row["name"].startswith("provider.fast_")
                or row["name"].startswith("provider.finalize_")
            ]
            history_page_breakdown = []
            for name, bucket in sorted(
                self._history_page_buckets.items(),
                key=lambda item: float(item[1].get("total_s") or 0.0),
                reverse=True,
            ):
                row = _summarize_timing_bucket(name, bucket)
                row["page_messages"] = int(bucket.get("page_messages") or 0)
                history_page_breakdown.append(row)
            scan_phase_breakdown = [
                _summarize_timing_bucket(name, bucket)
                for name, bucket in sorted(
                    self._scan_phase_buckets.items(),
                    key=lambda item: float(item[1].get("total_s") or 0.0),
                    reverse=True,
                )
            ]
            substep_breakdown = [
                _summarize_timing_bucket(name, bucket)
                for name, bucket in sorted(
                    self._substep_buckets.items(),
                    key=lambda item: float(item[1].get("total_s") or 0.0),
                    reverse=True,
                )
            ]
            materialize_asset_breakdown = []
            for name, bucket in sorted(
                self._materialize_asset_buckets.items(),
                key=lambda item: float(item[1].get("total_s") or 0.0),
                reverse=True,
            ):
                row = _summarize_timing_bucket(name, bucket)
                row["asset_type"] = bucket.get("asset_type")
                row["asset_role"] = bucket.get("asset_role")
                row["status"] = bucket.get("status")
                row["resolver"] = bucket.get("resolver")
                row["missing_kind"] = bucket.get("missing_kind")
                materialize_asset_breakdown.append(row)
            materialize_stage_breakdown = []
            for name, bucket in sorted(
                self._materialize_stage_buckets.items(),
                key=lambda item: float(item[1].get("total_s") or 0.0),
                reverse=True,
            ):
                row = _summarize_timing_bucket(name, bucket)
                row["asset_type"] = bucket.get("asset_type")
                row["substep"] = bucket.get("substep")
                row["status"] = bucket.get("status")
                materialize_stage_breakdown.append(row)
            copy_io_breakdown = []
            for name, bucket in sorted(
                self._copy_io_buckets.items(),
                key=lambda item: float(item[1].get("total_s") or 0.0),
                reverse=True,
            ):
                row = _summarize_timing_bucket(name, bucket)
                row["asset_type"] = bucket.get("asset_type")
                row["substep"] = bucket.get("substep")
                row["resolver"] = bucket.get("resolver")
                row["same_volume"] = bool(bucket.get("same_volume"))
                row["source_drive"] = bucket.get("source_drive")
                row["target_drive"] = bucket.get("target_drive")
                row["size_bucket"] = bucket.get("size_bucket")
                copy_io_breakdown.append(row)
            return {
                **summary,
                "total_elapsed_s": summary["elapsed_s"],
                "stage_breakdown": stage_breakdown,
                "fetch_stage_breakdown": fetch_stage_breakdown,
                "trace_path": str(self.path),
                "report_path": str(self.report_path),
                "history_page_breakdown": history_page_breakdown,
                "scan_phase_breakdown": scan_phase_breakdown,
                "materialize_stage_breakdown": materialize_stage_breakdown,
                "materialize_asset_breakdown": materialize_asset_breakdown,
                "copy_io_breakdown": copy_io_breakdown,
                "scan_summaries": list(self._scan_summaries),
                "page_size_adapt_events": list(self._page_size_adapt_events),
                "forward_expand_runs": list(self._forward_expand_runs),
                "tail_bulk_chunk_breakdown": list(self._tail_bulk_chunk_breakdown),
                "tail_forward_hydrate_windows": list(self._tail_forward_hydrate_windows),
                "substep_breakdown": substep_breakdown,
                "top_materialize_steps": list(self._top_materialize_steps),
                "top_materialize_substeps": list(self._top_materialize_substeps),
            }

    def persist_report(self, *, record_count: int | None = None) -> Path:
        report = self.build_report(record_count=record_count)
        temp_path = self.report_path.with_name(f"{self.report_path.name}.tmp")
        temp_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        os.replace(temp_path, self.report_path)
        return self.report_path

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handle.flush()
            self._handle.close()
            os.replace(self._temp_path, self.path)


def _size_bucket_label(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0"
    if size_bytes < 64 * 1024:
        return "<64KiB"
    if size_bytes < 256 * 1024:
        return "64-256KiB"
    if size_bytes < 1024 * 1024:
        return "256KiB-1MiB"
    if size_bytes < 8 * 1024 * 1024:
        return "1-8MiB"
    if size_bytes < 32 * 1024 * 1024:
        return "8-32MiB"
    return ">=32MiB"
