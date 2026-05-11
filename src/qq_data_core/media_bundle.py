from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from time import monotonic
from typing import Any, Callable, Iterable, Literal
from urllib.parse import urlparse

import orjson

from .export_forensics import (
    ExportForensicsCollector,
    ExportInvestigativeFailure,
    ForensicsRecordResult,
)
from .models import ExportBundleResult, MaterializedAsset, NormalizedMessage, NormalizedSegment, NormalizedSnapshot
from .paths import atomic_write_bytes, build_timestamp_token

MATERIALIZE_SLOW_STEP_WARN_S = 5.0
BUFFERED_COPY_CHUNK_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _AssetCandidate:
    asset_type: str
    asset_role: str | None
    file_name: str | None
    source_path: str | None
    md5: str | None
    timestamp_ms: int
    download_hint: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _MediaSearchContext:
    search_roots: list[Path]
    account_hints: set[str] = field(default_factory=set)
    legacy_md5_matches: dict[tuple[str, str], Path] = field(default_factory=dict)
    legacy_loose_bucket_results: dict[tuple[str, str], dict[str, Path | None]] = field(default_factory=dict)
    wanted_md5_by_bucket: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    month_hints: set[str] = field(default_factory=set)
    time_window_ms: tuple[int, int] | None = None
    media_cache_dir: Path | None = None


def write_export_bundle(
    snapshot: NormalizedSnapshot,
    data_path: Path,
    *,
    write_data: Callable[[NormalizedSnapshot, Path], Path],
    media_resolution_mode: Literal["napcat_only", "legacy_local_research"] = "legacy_local_research",
    media_search_roots: Iterable[Path] | None = None,
    media_cache_dir: Path | None = None,
    media_download_callback: Callable[[dict[str, Any]], str | Path | None] | None = None,
    media_download_manager: Any | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    forensics_collector: ExportForensicsCollector | None = None,
) -> ExportBundleResult:
    stage_token = build_timestamp_token(include_pid=True)
    staged_data_path = data_path.with_name(f".{data_path.name}.{stage_token}.tmp")
    if progress_callback is not None:
        progress_callback(
            {
                "phase": "write_data_file",
                "stage": "start",
                "record_count": len(snapshot.messages),
                "target_path": str(data_path),
            }
        )
    staged_assets_dir = data_path.parent / f".{data_path.stem}_assets.{stage_token}.tmp"
    manifest_path = data_path.with_suffix(".manifest.json")
    final_assets_dir = data_path.parent / f"{data_path.stem}_assets"
    try:
        write_started = monotonic()
        written_data_path = write_data(snapshot, staged_data_path)
        write_elapsed_s = monotonic() - write_started
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "write_data_file",
                    "stage": "done",
                    "elapsed_s": round(write_elapsed_s, 4),
                    "record_count": len(snapshot.messages),
                    "target_path": str(data_path),
                    "staged_path": str(written_data_path),
                    "bytes_written": _safe_file_size(written_data_path),
                }
            )
        materialize_started = monotonic()
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "pipeline_stage",
                    "stage": "bundle.materialize_snapshot_media",
                    "status": "start",
                    "candidate_hint": len(snapshot.messages),
                }
            )
        assets = materialize_snapshot_media(
            snapshot,
            staged_assets_dir,
            media_resolution_mode=media_resolution_mode,
            media_search_roots=media_search_roots,
            media_cache_dir=media_cache_dir,
            media_download_callback=media_download_callback,
            media_download_manager=media_download_manager,
            progress_callback=progress_callback,
            forensics_collector=forensics_collector,
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "pipeline_stage",
                    "stage": "bundle.materialize_snapshot_media",
                    "status": "done",
                    "elapsed_s": round(monotonic() - materialize_started, 4),
                    "materialized_asset_count": len(assets),
                }
            )
        summary = _summarize_assets(assets)
        finalize_started = monotonic()
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "pipeline_stage",
                    "stage": "bundle.finalize_output_files",
                    "status": "start",
                    "data_path": str(data_path),
                    "assets_dir": str(final_assets_dir),
                }
            )
        if data_path.exists():
            data_path.unlink()
        written_data_path.replace(data_path)
        if final_assets_dir.exists():
            shutil.rmtree(final_assets_dir)
        if staged_assets_dir.exists():
            shutil.move(str(staged_assets_dir), str(final_assets_dir))
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "pipeline_stage",
                    "stage": "bundle.finalize_output_files",
                    "status": "done",
                    "elapsed_s": round(monotonic() - finalize_started, 4),
                    "data_path": str(data_path),
                    "assets_dir": str(final_assets_dir),
                    "materialized_asset_count": len(assets),
                }
            )
        manifest_started = monotonic()
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "pipeline_stage",
                    "stage": "bundle.write_manifest",
                    "status": "start",
                    "manifest_path": str(manifest_path),
                }
            )
        _write_manifest_json(
            manifest_path,
            {
                "schema_version": 1,
                "chat_type": snapshot.chat_type,
                "chat_id": snapshot.chat_id,
                "chat_name": snapshot.chat_name,
                "exported_at": snapshot.exported_at.isoformat(),
                "record_count": len(snapshot.messages),
                "metadata": snapshot.metadata,
                "data_file": data_path.name,
                "assets_dir": final_assets_dir.name,
                "asset_summary": summary,
                "missing_breakdown": _summarize_missing_breakdown(assets),
            },
            assets=assets,
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "pipeline_stage",
                    "stage": "bundle.write_manifest",
                    "status": "done",
                    "elapsed_s": round(monotonic() - manifest_started, 4),
                    "manifest_path": str(manifest_path),
                    "materialized_asset_count": len(assets),
                }
            )
        return ExportBundleResult(
            data_path=data_path,
            manifest_path=manifest_path,
            assets_dir=final_assets_dir,
            record_count=len(snapshot.messages),
            copied_asset_count=summary["copied"],
            reused_asset_count=summary["reused"],
            missing_asset_count=summary["missing"],
            error_asset_count=summary["error"],
            forensic_run_dir=forensics_collector.run_dir if forensics_collector is not None else None,
            forensic_summary_path=forensics_collector.summary_path if forensics_collector is not None else None,
            forensic_incident_count=forensics_collector.incident_count if forensics_collector is not None else 0,
            assets=assets,
        )
    finally:
        with suppress(OSError):
            staged_data_path.unlink(missing_ok=True)
        if staged_assets_dir.exists():
            with suppress(OSError):
                shutil.rmtree(staged_assets_dir)


def materialize_snapshot_media(
    snapshot: NormalizedSnapshot,
    assets_dir: Path,
    *,
    media_resolution_mode: Literal["napcat_only", "legacy_local_research"] = "legacy_local_research",
    media_search_roots: Iterable[Path] | None = None,
    media_cache_dir: Path | None = None,
    media_download_callback: Callable[[dict[str, Any]], str | Path | None] | None = None,
    media_download_manager: Any | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    forensics_collector: ExportForensicsCollector | None = None,
) -> list[MaterializedAsset]:
    assets_dir.mkdir(parents=True, exist_ok=True)
    candidate_entries: list[tuple[NormalizedMessage, _AssetCandidate]] = []
    for message in snapshot.messages:
        for candidate in _iter_asset_candidates(message):
            candidate_entries.append((message, candidate))
    download_requests = [
        {
            "asset_type": candidate.asset_type,
            "asset_role": candidate.asset_role,
            "file_name": candidate.file_name,
            "source_path": candidate.source_path,
            "md5": candidate.md5,
            "timestamp_ms": candidate.timestamp_ms,
            "download_hint": candidate.download_hint,
        }
        for _message, candidate in candidate_entries
    ]
    search_context: _MediaSearchContext | None = None
    if media_resolution_mode != "napcat_only":
        candidates = [candidate for _message, candidate in candidate_entries]
        roots = [root.resolve() for root in (media_search_roots or []) if root.exists()]
        search_context = _build_media_search_context(
            roots,
            candidates,
            snapshot=snapshot,
            media_cache_dir=media_cache_dir,
        )
    if media_resolution_mode == "napcat_only" and media_download_manager is not None:
        request_count = len(download_requests)
        if progress_callback is not None:
            _emit_download_queue_progress(
                progress_callback,
                stage="start",
                snapshot=media_download_manager.begin_export_download_tracking(download_requests),
            )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "prefetch_media",
                    "stage": "start",
                    "request_count": request_count,
                }
            )
        started_prefetch = monotonic()
        try:
            if progress_callback is None:
                media_download_manager.prepare_for_export(download_requests)
            else:
                media_download_manager.prepare_for_export(
                    download_requests,
                    progress_callback=progress_callback,
                )
        except Exception as exc:
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "prefetch_media",
                        "stage": "error",
                        "request_count": request_count,
                        "elapsed_s": round(monotonic() - started_prefetch, 4),
                        "error": str(exc),
                    }
                )
        else:
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "prefetch_media",
                        "stage": "done",
                        "request_count": request_count,
                        "elapsed_s": round(monotonic() - started_prefetch, 4),
                    }
                )
    copied_map: dict[str, str] = {}
    recent_identity_reuse_map: dict[tuple[Any, ...], tuple[str, str | None, str | None]] = {}
    future_local_identity_map = _build_future_local_identity_resolution_map(candidate_entries)
    occupied_export_paths: dict[str, str] = {}
    resolution_cache: dict[tuple[Any, ...], tuple[Path | None, str]] = {}
    created_export_dirs: set[str] = set()
    assets: list[MaterializedAsset] = []
    second_pass_candidates: list[tuple[MaterializedAsset, _AssetCandidate]] = []
    copied_count = 0
    reused_count = 0
    missing_count = 0
    error_count = 0
    total_candidates = len(candidate_entries)
    last_download_signature: tuple[Any, ...] | None = None

    for message, candidate in candidate_entries:
        current_index = len(assets) + 1
        step_started = monotonic()
        route_attempts: list[dict[str, Any]] = []
        pre_path_evidence: dict[str, Any] | None = None

        def _candidate_trace_callback(payload: dict[str, Any]) -> None:
            if str(payload.get("phase") or "") == "materialize_asset_substep":
                enriched_payload = dict(payload)
                enriched_payload.setdefault("current", current_index)
                enriched_payload.setdefault("total", total_candidates)
                route_attempts.append(enriched_payload)
                if progress_callback is not None:
                    progress_callback(enriched_payload)
                return
            if progress_callback is not None:
                progress_callback(payload)

        _emit_materialization_step_trace(
            progress_callback,
            stage="start",
            current=current_index,
            total=total_candidates,
            candidate=candidate,
        )
        identity_reuse = _lookup_recent_identity_reuse(
            candidate,
            recent_identity_reuse_map,
        )
        if identity_reuse is not None:
            exported_rel_path, resolved_source_path, reused_resolver = identity_reuse
            asset = MaterializedAsset(
                message_id=message.message_id,
                message_seq=message.message_seq,
                sender_id=message.sender_id,
                timestamp_iso=message.timestamp_iso,
                asset_type=str(candidate.asset_type),
                asset_role=candidate.asset_role,
                file_name=candidate.file_name,
                source_path=candidate.source_path,
                resolved_source_path=resolved_source_path,
                resolver=reused_resolver or "bundle_identity_reuse",
                extra={
                    "chat_id": message.chat_id,
                    "chat_type": message.chat_type,
                    "sender_name": message.sender_name,
                },
            )
            asset.status = "reused"
            asset.exported_rel_path = exported_rel_path
            assets.append(asset)
            reused_count += 1
            step_elapsed_s = round(monotonic() - step_started, 4)
            _emit_materialization_step_trace(
                progress_callback,
                stage="done",
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                status=asset.status,
                resolver=asset.resolver,
                resolved_source_path=asset.resolved_source_path,
                step_elapsed_s=step_elapsed_s,
            )
            _emit_materialization_progress(
                progress_callback,
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                copied=copied_count,
                reused=reused_count,
                missing=missing_count,
                error=error_count,
                status=asset.status,
                resolver=asset.resolver,
                step_elapsed_s=step_elapsed_s,
            )
            if media_download_manager is not None:
                snapshot = media_download_manager.export_download_progress_snapshot()
                signature = tuple(sorted(snapshot.items()))
                if signature != last_download_signature:
                    _emit_download_queue_progress(
                        progress_callback,
                        stage="progress",
                        snapshot=snapshot,
                    )
                    last_download_signature = signature
            continue
        cache_key = _asset_resolution_cache_key(candidate)
        cached_resolution = resolution_cache.get(cache_key)
        if cached_resolution is None:
            if (
                forensics_collector is not None
                and forensics_collector.enabled
                and candidate.asset_type in {"video", "file"}
                and _candidate_has_forward_parent_hint(candidate)
            ):
                pre_path_evidence = forensics_collector.collect_candidate_path_evidence(
                    candidate=_candidate_forensics_payload(candidate),
                    asset_type=candidate.asset_type,
                    file_name=candidate.file_name,
                    source_path=candidate.source_path,
                )
            future_local_identity = _lookup_future_local_identity_resolution(
                candidate,
                current_index=current_index,
                future_identity_map=future_local_identity_map,
            )
            if future_local_identity is not None:
                resolved_path, resolver = future_local_identity
            elif media_resolution_mode == "napcat_only":
                resolved_path, resolver = _resolve_candidate_path_napcat_only(
                    candidate,
                    media_download_manager=media_download_manager,
                    media_download_callback=media_download_callback,
                    progress_callback=_candidate_trace_callback,
                )
            else:
                assert search_context is not None
                resolved_path, resolver = _resolve_candidate_path(candidate, context=search_context)
                if resolved_path is None and media_download_callback is not None:
                    resolved_path = _resolve_via_download_callback(candidate, media_download_callback)
                    if resolved_path is not None:
                        resolver = (
                            "sticker_remote_download"
                            if candidate.asset_type == "sticker"
                            else "napcat_action_download"
                        )
            resolution_cache[cache_key] = (resolved_path, resolver)
        else:
            resolved_path, resolver = cached_resolution
        asset = MaterializedAsset(
            message_id=message.message_id,
            message_seq=message.message_seq,
            sender_id=message.sender_id,
            timestamp_iso=message.timestamp_iso,
            asset_type=str(candidate.asset_type),
            asset_role=candidate.asset_role,
            file_name=candidate.file_name,
            source_path=candidate.source_path,
            resolved_source_path=str(resolved_path) if resolved_path else None,
            resolver=resolver,
            extra={
                "chat_id": message.chat_id,
                "chat_type": message.chat_type,
                "sender_name": message.sender_name,
            },
        )
        if resolved_path is None:
            asset.status = "missing"
            asset.missing_kind = resolver or "missing"
            asset.note = _missing_asset_note(resolver)
            assets.append(asset)
            missing_count += 1
            forensic_result = _record_forensic_incident(
                forensics_collector=forensics_collector,
                message=message,
                candidate=candidate,
                asset=asset,
                route_attempts=route_attempts,
                pre_path_evidence=pre_path_evidence,
            )
            if (
                forensic_result is not None
                and forensic_result.is_new_incident
                and progress_callback is not None
            ):
                progress_callback(
                    {
                        "phase": "forensic_incident",
                        "stage": "recorded",
                        "incident_id": forensic_result.incident_id,
                        "reason_category": forensic_result.reason_category,
                        "file_name": asset.file_name,
                        "asset_type": asset.asset_type,
                        "occurrence_count": forensic_result.occurrence_count,
                        "is_new_incident": forensic_result.is_new_incident,
                        "incident_path": str(forensic_result.incident_path)
                        if forensic_result.incident_path is not None
                        else None,
                    }
                )
            if (
                media_resolution_mode == "napcat_only"
                and media_download_manager is not None
                and _candidate_has_second_pass_public_retry_evidence(candidate)
                and hasattr(media_download_manager, "resolve_via_public_token_route")
            ):
                second_pass_candidates.append((asset, candidate))
            step_elapsed_s = round(monotonic() - step_started, 4)
            _emit_materialization_step_trace(
                progress_callback,
                stage="done",
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                status=asset.status,
                resolver=asset.resolver,
                missing_kind=asset.missing_kind,
                note=asset.note,
                step_elapsed_s=step_elapsed_s,
            )
            _emit_materialization_progress(
                progress_callback,
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                copied=copied_count,
                reused=reused_count,
                missing=missing_count,
                error=error_count,
                status=asset.status,
                resolver=asset.resolver,
                step_elapsed_s=step_elapsed_s,
            )
            if media_download_manager is not None:
                snapshot = media_download_manager.export_download_progress_snapshot()
                signature = tuple(sorted(snapshot.items()))
                if signature != last_download_signature:
                    _emit_download_queue_progress(
                        progress_callback,
                        stage="progress",
                        snapshot=snapshot,
                    )
                    last_download_signature = signature
            continue

        dedupe_key = str(resolved_path).lower()
        if dedupe_key in copied_map:
            asset.status = "reused"
            asset.exported_rel_path = copied_map[dedupe_key]
            for identity_key in _asset_recent_identity_keys(candidate):
                recent_identity_reuse_map[identity_key] = (
                    asset.exported_rel_path,
                    asset.resolved_source_path,
                    asset.resolver,
                )
            assets.append(asset)
            reused_count += 1
            step_elapsed_s = round(monotonic() - step_started, 4)
            _emit_materialization_step_trace(
                progress_callback,
                stage="done",
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                status=asset.status,
                resolver=asset.resolver,
                resolved_source_path=asset.resolved_source_path,
                step_elapsed_s=step_elapsed_s,
            )
            _emit_materialization_progress(
                progress_callback,
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                copied=copied_count,
                reused=reused_count,
                missing=missing_count,
                error=error_count,
                status=asset.status,
                resolver=asset.resolver,
                step_elapsed_s=step_elapsed_s,
            )
            if media_download_manager is not None:
                snapshot = media_download_manager.export_download_progress_snapshot()
                signature = tuple(sorted(snapshot.items()))
                if signature != last_download_signature:
                    _emit_download_queue_progress(
                        progress_callback,
                        stage="progress",
                        snapshot=snapshot,
                    )
                    last_download_signature = signature
            continue

        allocate_started = monotonic()
        rel_path = _allocate_export_rel_path(
            candidate,
            resolved_path,
            dedupe_key=dedupe_key,
            occupied_export_paths=occupied_export_paths,
        )
        _emit_materialization_substep_trace(
            progress_callback,
            substep="allocate_export_path",
            candidate=candidate,
            elapsed_s=monotonic() - allocate_started,
            source_path=str(resolved_path),
            target_path=str(assets_dir / rel_path),
            source_size_bytes=_safe_file_size(resolved_path),
        )
        target_path = assets_dir / rel_path
        mkdir_started = monotonic()
        _ensure_export_parent(target_path.parent, created_export_dirs)
        _emit_materialization_substep_trace(
            progress_callback,
            substep="ensure_export_parent",
            candidate=candidate,
            elapsed_s=monotonic() - mkdir_started,
            source_path=str(resolved_path),
            target_path=str(target_path),
        )
        try:
            copy_started = monotonic()
            copy_stats = _copy_asset_file_fast(resolved_path, target_path)
            copy_elapsed_s = monotonic() - copy_started
            _emit_materialization_substep_trace(
                progress_callback,
                substep="copy_asset_file",
                candidate=candidate,
                elapsed_s=copy_elapsed_s,
                source_path=str(resolved_path),
                target_path=str(target_path),
                source_size_bytes=_safe_file_size(resolved_path),
                target_size_bytes=_safe_file_size(target_path),
                resolver=asset.resolver,
                copy_stats=copy_stats,
            )
        except Exception as exc:  # pragma: no cover - hard to force all OS copy failures
            _emit_materialization_substep_trace(
                progress_callback,
                substep="copy_asset_file",
                candidate=candidate,
                elapsed_s=monotonic() - copy_started,
                status="error",
                detail=str(exc),
                source_path=str(resolved_path),
                target_path=str(target_path),
                source_size_bytes=_safe_file_size(resolved_path),
                target_size_bytes=_safe_file_size(target_path),
                resolver=asset.resolver,
            )
            asset.status = "error"
            asset.note = str(exc)
            assets.append(asset)
            error_count += 1
            step_elapsed_s = round(monotonic() - step_started, 4)
            _emit_materialization_step_trace(
                progress_callback,
                stage="done",
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                status=asset.status,
                resolver=asset.resolver,
                note=asset.note,
                resolved_source_path=asset.resolved_source_path,
                step_elapsed_s=step_elapsed_s,
            )
            _emit_materialization_progress(
                progress_callback,
                current=len(assets),
                total=total_candidates,
                candidate=candidate,
                copied=copied_count,
                reused=reused_count,
                missing=missing_count,
                error=error_count,
                status=asset.status,
                resolver=asset.resolver,
                step_elapsed_s=step_elapsed_s,
            )
            if media_download_manager is not None:
                snapshot = media_download_manager.export_download_progress_snapshot()
                signature = tuple(sorted(snapshot.items()))
                if signature != last_download_signature:
                    _emit_download_queue_progress(
                        progress_callback,
                        stage="progress",
                        snapshot=snapshot,
                    )
                    last_download_signature = signature
            continue

        asset.status = "copied"
        asset.exported_rel_path = rel_path.as_posix()
        copied_map[dedupe_key] = asset.exported_rel_path
        occupied_export_paths[asset.exported_rel_path.casefold()] = dedupe_key
        for identity_key in _asset_recent_identity_keys(candidate):
            recent_identity_reuse_map[identity_key] = (
                asset.exported_rel_path,
                asset.resolved_source_path,
                asset.resolver,
            )
        assets.append(asset)
        copied_count += 1
        step_elapsed_s = round(monotonic() - step_started, 4)
        _emit_materialization_step_trace(
            progress_callback,
            stage="done",
            current=len(assets),
            total=total_candidates,
            candidate=candidate,
            status=asset.status,
            resolver=asset.resolver,
            resolved_source_path=asset.resolved_source_path,
            step_elapsed_s=step_elapsed_s,
        )
        _emit_materialization_progress(
            progress_callback,
            current=len(assets),
            total=total_candidates,
            candidate=candidate,
            copied=copied_count,
            reused=reused_count,
            missing=missing_count,
            error=error_count,
            status=asset.status,
            resolver=asset.resolver,
            step_elapsed_s=step_elapsed_s,
        )
        if media_download_manager is not None:
            snapshot = media_download_manager.export_download_progress_snapshot()
            signature = tuple(sorted(snapshot.items()))
            if signature != last_download_signature:
                _emit_download_queue_progress(
                    progress_callback,
                    stage="progress",
                    snapshot=snapshot,
                )
                last_download_signature = signature

    if second_pass_candidates:
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "retry_recent_missing_public_token",
                    "stage": "start",
                    "candidate_count": len(second_pass_candidates),
                }
            )
        recovered_count = 0
        for asset, candidate in second_pass_candidates:
            retry_started = monotonic()
            identity_reuse = _lookup_recent_identity_reuse(
                candidate,
                recent_identity_reuse_map,
            )
            if identity_reuse is not None:
                exported_rel_path, resolved_source_path, reused_resolver = identity_reuse
                asset.resolved_source_path = resolved_source_path
                asset.resolver = reused_resolver or "bundle_identity_reuse"
                asset.missing_kind = None
                asset.note = None
                asset.status = "reused"
                asset.exported_rel_path = exported_rel_path
                missing_count -= 1
                reused_count += 1
                recovered_count += 1
                _emit_materialization_substep_trace(
                    progress_callback,
                    substep="second_pass_identity_reuse",
                    candidate=candidate,
                    elapsed_s=monotonic() - retry_started,
                    source_path=resolved_source_path,
                    target_path=exported_rel_path,
                    resolver=asset.resolver,
                )
                continue
            if asset.status != "missing":
                continue
            if not _asset_missing_kind_allows_second_pass_public_retry(asset.missing_kind):
                continue
            if not _candidate_has_second_pass_public_retry_evidence(candidate):
                continue
            request_payload = {
                "asset_type": candidate.asset_type,
                "asset_role": candidate.asset_role,
                "file_name": candidate.file_name,
                "source_path": candidate.source_path,
                "md5": candidate.md5,
                "timestamp_ms": candidate.timestamp_ms,
                "download_hint": candidate.download_hint,
            }
            if (
                hasattr(media_download_manager, "should_attempt_second_pass_public_retry")
                and not media_download_manager.should_attempt_second_pass_public_retry(request_payload)
            ):
                _emit_materialization_substep_trace(
                    progress_callback,
                    substep="second_pass_public_retry",
                    candidate=candidate,
                    elapsed_s=0.0,
                    status="skip_no_new_evidence",
                    detail="skipped repeated public token retry without pending prefetch result",
                )
                continue
            with suppress(Exception):
                public_retry_started = monotonic()
                resolved_path, resolver = media_download_manager.resolve_via_public_token_route(
                    request_payload
                )
                _emit_materialization_substep_trace(
                    progress_callback,
                    substep="second_pass_public_retry",
                    candidate=candidate,
                    elapsed_s=monotonic() - public_retry_started,
                    status="ok" if resolved_path is not None else "miss",
                    source_path=str(resolved_path) if resolved_path is not None else None,
                    resolver=resolver,
                )
                if resolved_path is None:
                    continue
                dedupe_key = str(resolved_path).lower()
                asset.resolved_source_path = str(resolved_path)
                asset.resolver = resolver
                asset.missing_kind = None
                asset.note = None
                if dedupe_key in copied_map:
                    asset.status = "reused"
                    asset.exported_rel_path = copied_map[dedupe_key]
                    missing_count -= 1
                    reused_count += 1
                    recovered_count += 1
                    _emit_materialization_substep_trace(
                        progress_callback,
                        substep="second_pass_reuse_copied_asset",
                        candidate=candidate,
                        elapsed_s=monotonic() - retry_started,
                        source_path=str(resolved_path),
                        target_path=asset.exported_rel_path,
                        resolver=asset.resolver,
                    )
                    continue
                allocate_started = monotonic()
                rel_path = _allocate_export_rel_path(
                    candidate,
                    resolved_path,
                    dedupe_key=dedupe_key,
                    occupied_export_paths=occupied_export_paths,
                )
                _emit_materialization_substep_trace(
                    progress_callback,
                    substep="second_pass_allocate_export_path",
                    candidate=candidate,
                    elapsed_s=monotonic() - allocate_started,
                    source_path=str(resolved_path),
                    target_path=str(assets_dir / rel_path),
                    source_size_bytes=_safe_file_size(resolved_path),
                    resolver=asset.resolver,
                )
                target_path = assets_dir / rel_path
                mkdir_started = monotonic()
                _ensure_export_parent(target_path.parent, created_export_dirs)
                _emit_materialization_substep_trace(
                    progress_callback,
                    substep="second_pass_ensure_export_parent",
                    candidate=candidate,
                    elapsed_s=monotonic() - mkdir_started,
                    source_path=str(resolved_path),
                    target_path=str(target_path),
                    resolver=asset.resolver,
                )
                try:
                    copy_started = monotonic()
                    copy_stats = _copy_asset_file_fast(resolved_path, target_path)
                except Exception as exc:  # pragma: no cover - hard to force all OS copy failures
                    _emit_materialization_substep_trace(
                        progress_callback,
                        substep="second_pass_copy_asset_file",
                        candidate=candidate,
                        elapsed_s=monotonic() - copy_started,
                        status="error",
                        detail=str(exc),
                        source_path=str(resolved_path),
                        target_path=str(target_path),
                        source_size_bytes=_safe_file_size(resolved_path),
                        target_size_bytes=_safe_file_size(target_path),
                        resolver=asset.resolver,
                    )
                    asset.status = "error"
                    asset.note = str(exc)
                    missing_count -= 1
                    error_count += 1
                    continue
                _emit_materialization_substep_trace(
                    progress_callback,
                    substep="second_pass_copy_asset_file",
                    candidate=candidate,
                    elapsed_s=monotonic() - copy_started,
                    source_path=str(resolved_path),
                    target_path=str(target_path),
                    source_size_bytes=_safe_file_size(resolved_path),
                    target_size_bytes=_safe_file_size(target_path),
                    resolver=asset.resolver,
                    copy_stats=copy_stats,
                )
                asset.status = "copied"
                asset.exported_rel_path = rel_path.as_posix()
                copied_map[dedupe_key] = asset.exported_rel_path
                occupied_export_paths[asset.exported_rel_path.casefold()] = dedupe_key
                missing_count -= 1
                copied_count += 1
                recovered_count += 1
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "retry_recent_missing_public_token",
                    "stage": "done",
                    "candidate_count": len(second_pass_candidates),
                    "recovered_count": recovered_count,
                }
            )

    if media_download_manager is not None:
        _emit_download_queue_progress(
            progress_callback,
            stage="done",
            snapshot=media_download_manager.settle_export_download_progress(),
        )
    return assets


def _asset_resolution_cache_key(candidate: _AssetCandidate) -> tuple[Any, ...]:
    hint = candidate.download_hint
    return (
        candidate.asset_type,
        candidate.asset_role,
        _normalize_identity_string(candidate.file_name),
        _normalize_identity_string(candidate.source_path),
        _normalize_identity_string(candidate.md5),
        _normalize_identity_string(hint.get("file_id")),
        _normalize_identity_string(hint.get("message_id_raw")),
        _normalize_identity_string(hint.get("element_id")),
        _normalize_identity_string(hint.get("peer_uid")),
        _normalize_identity_string(hint.get("chat_type_raw")),
        _normalize_identity_string(hint.get("remote_url")),
        _normalize_identity_string(hint.get("url")),
        _normalize_identity_string(hint.get("emoji_id")),
        _normalize_identity_string(hint.get("emoji_package_id")),
    )


def _asset_recent_identity_keys(candidate: _AssetCandidate) -> tuple[tuple[Any, ...], ...]:
    hint = candidate.download_hint if isinstance(candidate.download_hint, dict) else {}
    asset_type = _normalize_identity_string(candidate.asset_type)
    file_name = _normalize_identity_string(candidate.file_name)
    md5 = _normalize_identity_string(candidate.md5)
    source_leaf = ""
    if candidate.source_path:
        source_leaf = _normalize_identity_string(PureWindowsPath(candidate.source_path).name)
    file_id = _normalize_identity_string(hint.get("file_id"))
    public_token = _normalize_identity_string(hint.get("public_file_token"))
    public_action = _normalize_identity_string(hint.get("public_action"))
    remote_url = _normalize_identity_string(
        _normalized_match_url(hint.get("remote_url") or hint.get("url"))
    )
    preferred_names = tuple(
        name
        for name in (file_name, source_leaf)
        if name
    )
    keys: list[tuple[Any, ...]] = []
    if public_token and public_action:
        keys.append(("public_token", asset_type, public_action, public_token))
    if file_id:
        keys.append(("file_id", asset_type, file_id))
    if remote_url:
        keys.append(("remote_url", asset_type, remote_url))
    if md5:
        for preferred_name in preferred_names:
            keys.append(("md5_named", asset_type, preferred_name, md5))
        if asset_type == "image":
            keys.append(("image_md5_only", asset_type, md5))
    deduped_keys: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped_keys.append(key)
    return tuple(deduped_keys)


def _lookup_recent_identity_reuse(
    candidate: _AssetCandidate,
    reuse_map: dict[tuple[Any, ...], tuple[str, str | None, str | None]],
) -> tuple[str, str | None, str | None] | None:
    for identity_key in _asset_recent_identity_keys(candidate):
        reused = reuse_map.get(identity_key)
        if reused is not None:
            return reused
    return None


def _build_future_local_identity_resolution_map(
    candidate_entries: list[tuple[NormalizedMessage, _AssetCandidate]],
) -> dict[tuple[Any, ...], list[tuple[int, Path, str]]]:
    future_identity_map: dict[tuple[Any, ...], list[tuple[int, Path, str]]] = {}
    for index, (_message, candidate) in enumerate(candidate_entries, start=1):
        if candidate.asset_type != "image":
            continue
        resolved_path = _existing_path(candidate.source_path)
        if resolved_path is None:
            continue
        for identity_key in _asset_recent_identity_keys(candidate):
            future_identity_map.setdefault(identity_key, []).append(
                (index, resolved_path, "bundle_future_local_identity_evidence")
            )
    return future_identity_map


def _ensure_export_parent(
    parent: Path,
    created_export_dirs: set[str],
) -> None:
    cache_key = str(parent).casefold()
    if cache_key in created_export_dirs:
        return
    parent.mkdir(parents=True, exist_ok=True)
    created_export_dirs.add(cache_key)


def _copy_asset_file_fast(source_path: Path, target_path: Path) -> dict[str, Any]:
    source_anchor = str(source_path.anchor or "").strip().casefold()
    target_anchor = str(target_path.anchor or "").strip().casefold()
    source_size_bytes = _safe_file_size(source_path)
    if source_anchor and target_anchor and source_anchor != target_anchor:
        chunk_count = _copy_asset_file_buffered(source_path, target_path)
        return {
            "copy_mode": "buffered_cross_volume",
            "copy_chunk_count": chunk_count,
            "copy_buffer_bytes": BUFFERED_COPY_CHUNK_BYTES,
            "copy_bytes_total": source_size_bytes,
        }
    shutil.copyfile(source_path, target_path)
    return {
        "copy_mode": "copyfile_same_volume",
        "copy_chunk_count": 1,
        "copy_buffer_bytes": 0,
        "copy_bytes_total": source_size_bytes,
    }


def _copy_asset_file_buffered(
    source_path: Path,
    target_path: Path,
    *,
    chunk_bytes: int = BUFFERED_COPY_CHUNK_BYTES,
) -> int:
    buffer = bytearray(max(64 * 1024, int(chunk_bytes)))
    view = memoryview(buffer)
    chunk_count = 0
    with source_path.open("rb", buffering=0) as source_handle, target_path.open("wb", buffering=0) as target_handle:
        while True:
            bytes_read = source_handle.readinto(buffer)
            if not bytes_read:
                break
            target_handle.write(view[:bytes_read])
            chunk_count += 1
    return chunk_count


def _lookup_future_local_identity_resolution(
    candidate: _AssetCandidate,
    *,
    current_index: int,
    future_identity_map: dict[tuple[Any, ...], list[tuple[int, Path, str]]],
) -> tuple[Path, str] | None:
    if candidate.asset_type != "image":
        return None
    for identity_key in _asset_recent_identity_keys(candidate):
        candidates = future_identity_map.get(identity_key)
        if not candidates:
            continue
        for evidence_index, resolved_path, resolver in candidates:
            if evidence_index > current_index:
                return resolved_path, resolver
    return None


def _candidate_has_second_pass_public_retry_evidence(candidate: _AssetCandidate) -> bool:
    if candidate.asset_type not in {"image", "video", "file", "speech"}:
        return False
    hint = candidate.download_hint if isinstance(candidate.download_hint, dict) else {}
    forward_parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
    has_context_hint = any(
        _normalize_identity_string(hint.get(key))
        for key in ("message_id_raw", "element_id", "peer_uid", "chat_type_raw")
    ) or any(
        _normalize_identity_string(forward_parent.get(key))
        for key in ("message_id_raw", "element_id", "peer_uid", "chat_type_raw")
    )
    if not has_context_hint:
        return False
    has_locator_evidence = any(
        [
            _normalize_identity_string(candidate.file_name),
            _normalize_identity_string(candidate.md5),
            _normalize_identity_string(candidate.source_path),
            _normalize_identity_string(hint.get("file_id")),
            _normalize_identity_string(hint.get("public_file_token")),
            _normalize_identity_string(hint.get("public_action")),
            _normalize_identity_string(_normalized_match_url(hint.get("remote_url") or hint.get("url"))),
        ]
    )
    return has_locator_evidence


def _asset_missing_kind_allows_second_pass_public_retry(missing_kind: str | None) -> bool:
    normalized = str(missing_kind or "").strip().lower()
    if not normalized:
        return True
    return normalized in {"missing", "missing_after_napcat"}


def _record_forensic_incident(
    *,
    forensics_collector: ExportForensicsCollector | None,
    message: NormalizedMessage,
    candidate: _AssetCandidate,
    asset: MaterializedAsset,
    route_attempts: list[dict[str, Any]],
    pre_path_evidence: dict[str, Any] | None = None,
) -> ForensicsRecordResult | None:
    if forensics_collector is None or not forensics_collector.enabled:
        return None
    result = forensics_collector.record_investigative_missing(
        message=message,
        candidate=_candidate_forensics_payload(candidate),
        asset=asset,
        route_attempts=route_attempts,
        pre_path_evidence=pre_path_evidence,
    )
    if result is None:
        return None
    asset.extra["forensic_incident_id"] = result.incident_id
    asset.extra["forensic_reason_category"] = result.reason_category
    if result.should_abort:
        raise ExportInvestigativeFailure(
            incident_id=result.incident_id,
            forensic_summary_path=forensics_collector.summary_path,
            incident_path=result.incident_path,
            reason_category=result.reason_category,
        )
    return result


def _candidate_forensics_payload(candidate: _AssetCandidate) -> dict[str, Any]:
    return {
        "asset_type": candidate.asset_type,
        "asset_role": candidate.asset_role,
        "file_name": candidate.file_name,
        "source_path": candidate.source_path,
        "md5": candidate.md5,
        "timestamp_ms": candidate.timestamp_ms,
        "download_hint": dict(candidate.download_hint),
    }


def _candidate_has_forward_parent_hint(candidate: _AssetCandidate) -> bool:
    hint = candidate.download_hint if isinstance(candidate.download_hint, dict) else {}
    parent = hint.get("_forward_parent")
    return isinstance(parent, dict) and bool(str(parent.get("message_id_raw") or "").strip())


def _write_manifest_json(
    manifest_path: Path,
    header: dict[str, Any],
    *,
    assets: list[MaterializedAsset],
) -> None:
    temp_path = manifest_path.with_suffix(f"{manifest_path.suffix}.tmp")
    header_json = orjson.dumps(header, option=orjson.OPT_INDENT_2).decode("utf-8")
    body_prefix = header_json[:-1] + ',\n  "assets": [\n'
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(body_prefix)
            for index, asset in enumerate(assets):
                if index > 0:
                    handle.write(",\n")
                asset_json = json.dumps(
                    asset.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write(_indent_json_block(asset_json, 4))
            if assets:
                handle.write("\n")
            handle.write("  ]\n}\n")
        temp_path.replace(manifest_path)
    finally:
        with suppress(OSError):
            temp_path.unlink(missing_ok=True)


def _indent_json_block(value: str, indent_spaces: int) -> str:
    indent = " " * indent_spaces
    return "\n".join(f"{indent}{line}" if line else line for line in value.splitlines())


def _normalize_identity_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _normalized_match_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        return parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
        ).geturl()
    return text.lower()


def _emit_materialization_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    current: int,
    total: int,
    candidate: _AssetCandidate,
    copied: int,
    reused: int,
    missing: int,
    error: int,
    status: str | None = None,
    resolver: str | None = None,
    step_elapsed_s: float | None = None,
) -> None:
    if progress_callback is None:
        return
    payload = {
        "phase": "materialize_assets",
        "current": current,
        "total": total,
        "asset_type": candidate.asset_type,
        "asset_role": candidate.asset_role,
        "file_name": candidate.file_name,
        "copied_assets": copied,
        "reused_assets": reused,
        "missing_assets": missing,
        "error_assets": error,
    }
    if status:
        payload["status"] = status
    if resolver:
        payload["resolver"] = resolver
    if step_elapsed_s is not None:
        payload["step_elapsed_s"] = step_elapsed_s
        payload["step_elapsed_ms"] = int(round(step_elapsed_s * 1000))
        if step_elapsed_s >= MATERIALIZE_SLOW_STEP_WARN_S:
            payload["slow_step"] = True
    progress_callback(payload)


def _emit_download_queue_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    snapshot: dict[str, Any],
) -> None:
    if progress_callback is None:
        return
    payload = {
        "phase": "download_assets",
        "stage": stage,
        "candidate_total": int(snapshot.get("candidate_total") or 0),
        "eager_remote_candidates": int(snapshot.get("eager_remote_candidates") or 0),
        "public_token_candidates": int(snapshot.get("public_token_candidates") or 0),
        "context_candidates": int(snapshot.get("context_candidates") or 0),
        "queued": int(snapshot.get("queued") or 0),
        "active": int(snapshot.get("active") or 0),
        "completed": int(snapshot.get("completed") or 0),
        "failed": int(snapshot.get("failed") or 0),
        "cached": int(snapshot.get("cached") or 0),
        "timeout_count": int(snapshot.get("timeout_count") or 0),
        "forward_context_timeout_count": int(snapshot.get("forward_context_timeout_count") or 0),
        "forward_context_empty_count": int(snapshot.get("forward_context_empty_count") or 0),
        "forward_context_error_count": int(snapshot.get("forward_context_error_count") or 0),
        "last_asset_type": snapshot.get("last_asset_type"),
        "last_file_name": snapshot.get("last_file_name"),
        "last_status": snapshot.get("last_status"),
    }
    progress_callback(payload)


def _emit_materialization_step_trace(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    stage: str,
    current: int,
    total: int,
    candidate: _AssetCandidate,
    status: str | None = None,
    resolver: str | None = None,
    missing_kind: str | None = None,
    note: str | None = None,
    resolved_source_path: str | None = None,
    step_elapsed_s: float | None = None,
) -> None:
    if progress_callback is None:
        return
    hint = candidate.download_hint or {}
    forward_parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
    payload: dict[str, Any] = {
        "phase": "materialize_asset_step",
        "stage": stage,
        "current": current,
        "total": total,
        "asset_type": candidate.asset_type,
        "asset_role": candidate.asset_role,
        "file_name": candidate.file_name,
        "source_path": candidate.source_path,
        "md5": candidate.md5,
        "message_id_raw": hint.get("message_id_raw"),
        "element_id": hint.get("element_id"),
        "hint_file_id": hint.get("file_id"),
        "hint_url": hint.get("url"),
        "forward_parent_message_id_raw": forward_parent.get("message_id_raw"),
        "forward_parent_element_id": forward_parent.get("element_id"),
        "timestamp_ms": candidate.timestamp_ms,
        "timestamp_iso": _timestamp_iso_from_ms(candidate.timestamp_ms),
        "source_path_kind": _bundle_asset_location_kind(candidate.source_path),
        "hint_url_kind": _bundle_asset_location_kind(hint.get("remote_url") or hint.get("url")),
    }
    if status:
        payload["status"] = status
    if resolver:
        payload["resolver"] = resolver
    if missing_kind:
        payload["missing_kind"] = missing_kind
    if note:
        payload["note"] = note
    if resolved_source_path:
        payload["resolved_source_path"] = resolved_source_path
    if step_elapsed_s is not None:
        payload["step_elapsed_s"] = step_elapsed_s
        payload["step_elapsed_ms"] = int(round(step_elapsed_s * 1000))
        if step_elapsed_s >= MATERIALIZE_SLOW_STEP_WARN_S:
            payload["slow_step"] = True
    progress_callback(payload)


def _emit_materialization_substep_trace(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    substep: str,
    candidate: _AssetCandidate,
    elapsed_s: float,
    status: str = "done",
    detail: str | None = None,
    source_path: str | None = None,
    target_path: str | None = None,
    source_size_bytes: int | None = None,
    target_size_bytes: int | None = None,
    resolver: str | None = None,
    copy_stats: dict[str, Any] | None = None,
) -> None:
    if progress_callback is None:
        return
    hint = candidate.download_hint or {}
    forward_parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
    source_text = str(source_path or candidate.source_path or "").strip() or None
    target_text = str(target_path or "").strip() or None
    source_drive = None
    target_drive = None
    if source_text:
        with suppress(Exception):
            source_drive = str(Path(source_text).anchor or "").strip() or None
    if target_text:
        with suppress(Exception):
            target_drive = str(Path(target_text).anchor or "").strip() or None
    payload: dict[str, Any] = {
        "phase": "materialize_asset_substep",
        "stage": "done",
        "substep": substep,
        "status": status,
        "elapsed_s": round(elapsed_s, 4),
        "elapsed_ms": int(round(elapsed_s * 1000)),
        "asset_type": candidate.asset_type,
        "asset_role": candidate.asset_role,
        "file_name": candidate.file_name,
        "message_id_raw": hint.get("message_id_raw"),
        "element_id": hint.get("element_id"),
        "forward_parent_message_id_raw": forward_parent.get("message_id_raw"),
        "timestamp_ms": candidate.timestamp_ms,
        "timestamp_iso": _timestamp_iso_from_ms(candidate.timestamp_ms),
        "md5": candidate.md5,
        "hint_file_id": hint.get("file_id"),
        "hint_url": hint.get("remote_url") or hint.get("url"),
        "source_path": source_text,
        "source_path_kind": _bundle_asset_location_kind(source_text),
        "target_path": target_text,
        "target_path_kind": _bundle_asset_location_kind(target_text),
        "source_size_bytes": source_size_bytes,
        "target_size_bytes": target_size_bytes,
        "source_drive": source_drive,
        "target_drive": target_drive,
        "same_volume": bool(source_drive and target_drive and source_drive.casefold() == target_drive.casefold()),
    }
    if detail:
        payload["detail"] = detail
    if resolver:
        payload["resolver"] = resolver
    if isinstance(copy_stats, dict):
        for key, value in copy_stats.items():
            if value is None or value == "":
                continue
            payload[key] = value
    progress_callback(payload)


def _timestamp_iso_from_ms(timestamp_ms: int) -> str | None:
    if timestamp_ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _bundle_asset_location_kind(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower()
        if host in {"127.0.0.1:3000", "localhost:3000", "127.0.0.1:6099", "localhost:6099"}:
            return "napcat_local_download"
        if host.endswith("multimedia.nt.qq.com.cn"):
            return "qq_multimedia"
        return f"{parsed.scheme.lower()}_url"
    path = Path(text)
    if path.exists() and path.is_file():
        try:
            if path.stat().st_size > 0:
                return "local_file"
        except OSError:
            return "local_path"
        return "zero_byte_local"
    return "missing_local"


def _iter_asset_candidates(message: NormalizedMessage) -> Iterable[_AssetCandidate]:
    for segment in message.segments:
        yield from _iter_asset_candidates_from_segment(
            segment,
            timestamp_ms=message.timestamp_ms,
        )


def _iter_asset_candidates_from_segment(
    segment: NormalizedSegment | dict[str, Any],
    *,
    timestamp_ms: int,
    parent_download_hint: dict[str, Any] | None = None,
) -> Iterable[_AssetCandidate]:
    if isinstance(segment, NormalizedSegment):
        segment_type = segment.type
        file_name = segment.file_name
        path = segment.path
        md5 = segment.md5
        extra = dict(segment.extra or {})
    else:
        segment_type = str(segment.get("type") or "").strip()
        file_name = _string_or_none(segment.get("file_name"))
        path = _string_or_none(segment.get("path"))
        md5 = _string_or_none(segment.get("md5"))
        raw_extra = segment.get("extra") or {}
        extra = dict(raw_extra) if isinstance(raw_extra, dict) else {}
    if not path:
        path = _local_path_from_download_hint(extra)
    if parent_download_hint:
        merged_forward_hint = {
            key: value
            for key, value in parent_download_hint.items()
            if value is not None and value != "" and value != []
        }
        if merged_forward_hint:
            existing = dict(extra)
            existing["_forward_parent"] = merged_forward_hint
            for key, value in merged_forward_hint.items():
                existing.setdefault(f"_forward_parent_{key}", value)
            extra = existing

    if segment_type == "image":
        yield _AssetCandidate(
            "image",
            None,
            file_name,
            path,
            md5,
            timestamp_ms,
            download_hint=extra,
        )
        return
    if segment_type == "file":
        yield _AssetCandidate(
            "file",
            None,
            file_name,
            path,
            md5,
            timestamp_ms,
            download_hint=extra,
        )
        return
    if segment_type == "speech":
        yield _AssetCandidate(
            "speech",
            None,
            file_name,
            path,
            md5,
            timestamp_ms,
            download_hint=extra,
        )
        return
    if segment_type == "video":
        yield _AssetCandidate(
            "video",
            None,
            file_name,
            path,
            md5,
            timestamp_ms,
            download_hint=extra,
        )
        return
    if segment_type == "sticker":
        static_path = _string_or_none(extra.get("static_path"))
        dynamic_path = _string_or_none(extra.get("dynamic_path"))
        remote_url = _string_or_none(extra.get("remote_url"))
        remote_file_name = _string_or_none(extra.get("remote_file_name"))
        if static_path:
            yield _AssetCandidate(
                "sticker",
                "static",
                Path(PureWindowsPath(static_path)).name or file_name,
                static_path,
                md5,
                timestamp_ms,
                download_hint=extra,
            )
        if dynamic_path:
            yield _AssetCandidate(
                "sticker",
                "dynamic",
                Path(PureWindowsPath(dynamic_path)).name or file_name,
                dynamic_path,
                md5,
                timestamp_ms,
                download_hint=extra,
            )
        if not static_path and not dynamic_path and path:
            yield _AssetCandidate(
                "sticker",
                None,
                file_name,
                path,
                md5,
                timestamp_ms,
                download_hint=extra,
            )
        if not static_path and not dynamic_path and not path and remote_url:
            yield _AssetCandidate(
                "sticker",
                None,
                remote_file_name or file_name,
                None,
                md5,
                timestamp_ms,
                download_hint=extra,
            )
        return
    if segment_type == "forward":
        forward_parent_hint = {
            "message_id_raw": _string_or_none(extra.get("message_id_raw"))
            or _string_or_none(extra.get("_forward_parent_message_id_raw")),
            "element_id": _string_or_none(extra.get("element_id"))
            or _string_or_none(extra.get("_forward_parent_element_id")),
            "peer_uid": _string_or_none(extra.get("peer_uid"))
            or _string_or_none(extra.get("_forward_parent_peer_uid")),
            "chat_type_raw": extra.get("chat_type_raw")
            if extra.get("chat_type_raw") is not None
            else extra.get("_forward_parent_chat_type_raw"),
        }
        for node in extra.get("forward_messages") or []:
            if not isinstance(node, dict):
                continue
            for child in node.get("segments") or []:
                if isinstance(child, dict) or isinstance(child, NormalizedSegment):
                    yield from _iter_asset_candidates_from_segment(
                        child,
                        timestamp_ms=timestamp_ms,
                        parent_download_hint=forward_parent_hint,
                    )
        return


def _resolve_candidate_path_napcat_only(
    candidate: _AssetCandidate,
    *,
    media_download_manager: Any | None,
    media_download_callback: Callable[[dict[str, Any]], str | Path | None] | None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[Path | None, str]:
    raw_path = _existing_path(candidate.source_path)
    if raw_path is not None:
        return raw_path, "direct_local_path"
    request_payload = {
        "asset_type": candidate.asset_type,
        "asset_role": candidate.asset_role,
        "file_name": candidate.file_name,
        "source_path": candidate.source_path,
        "md5": candidate.md5,
        "timestamp_ms": candidate.timestamp_ms,
        "download_hint": candidate.download_hint,
    }
    if media_download_manager is not None and hasattr(media_download_manager, "resolve_for_export"):
        with suppress(Exception):
            resolve_for_export = media_download_manager.resolve_for_export
            parameters = inspect.signature(resolve_for_export).parameters
            if "trace_callback" in parameters:
                resolved_path, resolver = resolve_for_export(
                    request_payload,
                    trace_callback=progress_callback,
                )
            else:
                resolved_path, resolver = resolve_for_export(request_payload)
            if resolved_path is not None:
                return resolved_path, resolver or "napcat_context_hydrated"
            if resolver:
                return None, resolver
    # In strict NapCat-only mode, do not fall back to generic callback download,
    # local root scans, or MD5 cache matching. If context hydration cannot recover
    # a file, formal export must record a true NapCat-side miss.
    return None, "missing_after_napcat"


def _missing_asset_note(resolver: str | None) -> str:
    if resolver == "qq_expired_after_napcat":
        return "asset appears expired in QQ/NapCat; no local file and remote URL unavailable"
    if resolver == "qq_not_downloaded_local_placeholder":
        return "QQ only left zero-byte local placeholders (for example OriTemp/*.tmp); the original media was not materialized locally"
    return "source file not found"


def _resolve_via_download_callback(
    candidate: _AssetCandidate,
    media_download_callback: Callable[[dict[str, Any]], str | Path | None],
) -> Path | None:
    if candidate.asset_type not in {"image", "file", "speech", "video", "sticker"}:
        return None
    file_id = _string_or_none(candidate.download_hint.get("file_id"))
    file_name = _string_or_none(candidate.file_name)
    has_context_hint = any(
        _string_or_none(candidate.download_hint.get(key))
        for key in ("message_id_raw", "element_id", "peer_uid", "chat_type_raw")
    )
    if not file_id and not file_name and not has_context_hint:
        return None
    try:
        result = media_download_callback(
            {
                "asset_type": candidate.asset_type,
                "asset_role": candidate.asset_role,
                "file_name": candidate.file_name,
                "source_path": candidate.source_path,
                "md5": candidate.md5,
                "timestamp_ms": candidate.timestamp_ms,
                "download_hint": candidate.download_hint,
            }
        )
    except Exception:
        return None
    if result is None:
        return None
    candidate_path = Path(result)
    if not candidate_path.exists() or not candidate_path.is_file():
        return None
    return candidate_path.resolve()


def _resolve_candidate_path(
    candidate: _AssetCandidate,
    *,
    context: _MediaSearchContext,
) -> tuple[Path | None, str]:
    raw_path = _existing_path(candidate.source_path)
    if raw_path is not None:
        upgraded = _prefer_original_media_path(candidate, raw_path, context=context)
        if upgraded is not None and upgraded != raw_path:
            return upgraded, "segment_path_upgraded"
        return raw_path, "segment_path"
    if candidate.source_path:
        ntqq_original = _resolve_via_ntqq_originals(
            candidate,
            search_roots=context.search_roots,
            account_hints=context.account_hints,
        )
        if ntqq_original is not None:
            return ntqq_original, "qq_media_root_original_scan"
    else:
        ntqq_hinted = _resolve_via_ntqq_month_hints(candidate, context=context)
        if ntqq_hinted is not None:
            return ntqq_hinted, "qq_media_root_original_scan"
    legacy = _resolve_via_legacy_md5(candidate, context=context)
    if legacy is not None:
        return legacy, "legacy_md5_index"
    fallback = _resolve_via_roots(
        candidate,
        search_roots=context.search_roots,
        account_hints=context.account_hints,
    )
    if fallback is not None:
        return fallback, "qq_media_root_scan"
    return None, "unresolved"


def _resolve_via_roots(
    candidate: _AssetCandidate,
    *,
    search_roots: list[Path],
    account_hints: set[str],
) -> Path | None:
    if not search_roots:
        return None
    source = candidate.source_path or ""
    source_parts = list(PureWindowsPath(source).parts)
    suffixes = _candidate_suffixes(source_parts)
    for root in search_roots:
        for suffix in suffixes:
            trial = root.joinpath(*suffix)
            if trial.exists() and trial.is_file():
                upgraded = _prefer_original_media_path(candidate, trial.resolve(), context=None)
                return upgraded or trial.resolve()
    if candidate.asset_type in {"image", "sticker", "video", "speech"} and _normalized_md5(candidate.md5):
        return None
    names_to_try = _candidate_names(candidate)
    if not names_to_try:
        return None
    for directory in _iter_targeted_name_search_directories(
        search_roots,
        account_hints=account_hints,
        asset_type=candidate.asset_type,
        source_path=candidate.source_path,
    ):
        for name in names_to_try:
            try:
                match = next(directory.rglob(name))
            except StopIteration:
                continue
            if match.exists() and match.is_file():
                return match.resolve()
    return None


def _safe_file_size(path: str | Path | None) -> int | None:
    if not path:
        return None
    with suppress(OSError, ValueError):
        return int(Path(path).stat().st_size)
    return None


def _prefer_original_media_path(
    candidate: _AssetCandidate,
    resolved_path: Path,
    *,
    context: _MediaSearchContext | None,
) -> Path | None:
    if candidate.asset_type != "image":
        return None
    if _looks_like_thumbnail_path(resolved_path):
        upgraded = _find_ntqq_original_siblings(resolved_path)
        if upgraded is not None:
            return upgraded
        if context is not None:
            legacy = _resolve_via_legacy_md5(candidate, context=context)
            if legacy is not None and legacy != resolved_path:
                return legacy
    return None


def _resolve_via_ntqq_originals(
    candidate: _AssetCandidate,
    *,
    search_roots: list[Path],
    account_hints: set[str],
) -> Path | None:
    source_path = _string_or_none(candidate.source_path)
    if not source_path:
        return None
    parts = list(PureWindowsPath(source_path).parts)
    lowered = [part.lower() for part in parts]
    if "nt_qq" not in lowered or "nt_data" not in lowered:
        return None
    leaf = parts[-1]
    if not leaf:
        return None

    base_suffixes: list[list[str]] = []
    if "pic" in lowered:
        pic_index = lowered.index("pic")
        if pic_index + 1 < len(parts):
            month = parts[pic_index + 1]
            base_suffixes.extend(
                [
                    ["nt_qq", "nt_data", "Pic", month, "Ori"],
                    ["nt_qq", "nt_data", "Pic", month, "OriTemp"],
                    ["nt_qq", "nt_data", "Pic", month, "Thumb"],
                ]
            )
    if "emoji" in lowered and "emoji-recv" in lowered:
        emoji_index = lowered.index("emoji")
        if emoji_index + 2 < len(parts):
            recv_dir = parts[emoji_index + 1]
            month = parts[emoji_index + 2]
            base_suffixes.extend(
                [
                    ["nt_qq", "nt_data", "Emoji", recv_dir, month, "Ori"],
                    ["nt_qq", "nt_data", "Emoji", recv_dir, month, "Thumb"],
                    ["nt_qq", "nt_data", "Emoji", recv_dir, month, "OriTemp"],
                    ["nt_qq", "nt_data", "Pic", month, "Ori"],
                    ["nt_qq", "nt_data", "Pic", month, "OriTemp"],
                    ["nt_qq", "nt_data", "Pic", month, "Thumb"],
                ]
            )
    if not base_suffixes:
        return None

    stem = _preferred_media_stem(candidate, leaf)
    for root in search_roots:
        for parent in _ntqq_parent_candidates(root, account_hints=account_hints):
            for suffix in base_suffixes:
                directory = parent.joinpath(*suffix)
                if not directory.exists() or not directory.is_dir():
                    continue
                match = _find_candidate_in_directory(directory, stem=stem, asset_type=candidate.asset_type)
                if match is not None:
                    return match
    return None


def _resolve_via_ntqq_month_hints(
    candidate: _AssetCandidate,
    *,
    context: _MediaSearchContext,
) -> Path | None:
    if candidate.asset_type != "image" or not context.search_roots or not context.month_hints:
        return None
    stem = _preferred_media_stem(candidate, candidate.file_name or candidate.md5 or "")
    if not stem:
        return None
    hinted_months = sorted(context.month_hints)
    for root in context.search_roots:
        for parent in _ntqq_parent_candidates(root, account_hints=context.account_hints):
            month_candidates = list(hinted_months)
            pic_root = parent / "nt_qq" / "nt_data" / "Pic"
            if pic_root.exists() and pic_root.is_dir():
                with suppress(Exception):
                    for child in sorted(pic_root.iterdir(), key=lambda item: item.name):
                        if child.is_dir() and re.fullmatch(r"\d{4}-\d{2}", child.name):
                            if child.name not in month_candidates:
                                month_candidates.append(child.name)
            for month in month_candidates:
                for directory in (
                    parent / "nt_qq" / "nt_data" / "Pic" / month / "Thumb",
                    parent / "nt_qq" / "nt_data" / "Pic" / month / "Ori",
                    parent / "nt_qq" / "nt_data" / "Pic" / month / "OriTemp",
                ):
                    if not directory.exists() or not directory.is_dir():
                        continue
                    match = _find_candidate_in_directory(directory, stem=stem, asset_type="image")
                    if match is not None:
                        return match
    return None


def _candidate_suffixes(parts: list[str]) -> list[list[str]]:
    suffixes: list[list[str]] = []
    lowered = [part.lower() for part in parts]
    markers = {
        "qq": 1,
        "tencent files": 1,
        "nt_qq": 0,
        "pic": 0,
        "ptt": 0,
        "filerecv": 0,
        "emoji": 0,
        "video": 0,
    }
    for index, lowered_part in enumerate(lowered):
        if lowered_part in markers:
            skip = markers[lowered_part]
            start = max(0, index + skip)
            suffix = [part for part in parts[start:] if part not in {"\\", "/"}]
            if suffix:
                suffixes.append(suffix)
    if parts:
        suffixes.append([part for part in parts[-6:] if part not in {"\\", "/"}])
    return suffixes


def _candidate_names(candidate: _AssetCandidate) -> list[str]:
    names: list[str] = []
    for item in [candidate.file_name, candidate.md5]:
        value = _string_or_none(item)
        if value and value not in names:
            names.append(value)
    return names


def _build_media_search_context(
    search_roots: list[Path],
    candidates: list[_AssetCandidate],
    *,
    snapshot: NormalizedSnapshot,
    media_cache_dir: Path | None = None,
) -> _MediaSearchContext:
    wanted_md5_by_type: dict[str, set[str]] = {}
    wanted_md5_by_bucket: dict[tuple[str, str], set[str]] = {}
    account_hints: set[str] = set()
    for candidate in candidates:
        account_hints.update(_extract_account_hints(candidate.source_path))
        md5 = _normalized_md5(candidate.md5)
        if not md5:
            continue
        if candidate.asset_type not in {"image", "video", "file", "speech", "sticker"}:
            continue
        wanted_md5_by_type.setdefault(candidate.asset_type, set()).add(md5)
        bucket = _candidate_month_bucket(candidate)
        if bucket is not None:
            wanted_md5_by_bucket.setdefault((candidate.asset_type, bucket), set()).add(md5)

    time_window_ms = _derive_media_time_window(snapshot, candidates)
    month_hints = _derive_month_hints(candidates, time_window_ms=time_window_ms)
    legacy_matches: dict[tuple[str, str], Path] = {}
    if search_roots and wanted_md5_by_type:
        legacy_matches = _build_legacy_md5_matches(
            search_roots,
            wanted_md5_by_type,
            account_hints,
            month_hints=month_hints,
            time_window_ms=time_window_ms,
            media_cache_dir=media_cache_dir,
        )
    return _MediaSearchContext(
        search_roots=search_roots,
        account_hints=account_hints,
        legacy_md5_matches=legacy_matches,
        wanted_md5_by_bucket=wanted_md5_by_bucket,
        month_hints=month_hints,
        time_window_ms=time_window_ms,
        media_cache_dir=media_cache_dir,
    )


def _resolve_via_legacy_md5(
    candidate: _AssetCandidate,
    *,
    context: _MediaSearchContext,
) -> Path | None:
    md5 = _normalized_md5(candidate.md5)
    if not md5:
        return None
    match = context.legacy_md5_matches.get((candidate.asset_type, md5))
    if match is not None:
        return match
    return _resolve_via_legacy_md5_loose(candidate, context=context)


def _resolve_via_legacy_md5_loose(
    candidate: _AssetCandidate,
    *,
    context: _MediaSearchContext,
) -> Path | None:
    md5 = _normalized_md5(candidate.md5)
    if not md5 or not context.search_roots:
        return None
    bucket = _candidate_month_bucket(candidate)
    if bucket is not None:
        bucket_key = (candidate.asset_type, bucket)
        cached = context.legacy_loose_bucket_results.get(bucket_key)
        if cached is None:
            wanted = context.wanted_md5_by_bucket.get(bucket_key, {md5})
            matches = _build_legacy_md5_matches(
                context.search_roots,
                {candidate.asset_type: set(wanted)},
                context.account_hints,
                month_hints=set(),
                time_window_ms=None,
                media_cache_dir=context.media_cache_dir,
            )
            cached = {
                wanted_md5: matches.get((candidate.asset_type, wanted_md5))
                for wanted_md5 in wanted
            }
            context.legacy_loose_bucket_results[bucket_key] = cached
        return cached.get(md5)
    matches = _build_legacy_md5_matches(
        context.search_roots,
        {candidate.asset_type: {md5}},
        context.account_hints,
        month_hints=set(),
        time_window_ms=None,
        media_cache_dir=context.media_cache_dir,
    )
    return matches.get((candidate.asset_type, md5))


def _candidate_month_bucket(candidate: _AssetCandidate) -> str | None:
    with suppress(Exception):
        return datetime.fromtimestamp(candidate.timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m")
    return None


def _build_legacy_md5_matches(
    search_roots: list[Path],
    wanted_md5_by_type: dict[str, set[str]],
    account_hints: set[str],
    *,
    month_hints: set[str],
    time_window_ms: tuple[int, int] | None,
    media_cache_dir: Path | None = None,
) -> dict[tuple[str, str], Path]:
    matches: dict[tuple[str, str], Path] = {}
    pending: dict[str, set[str]] = {
        asset_type: set(md5s)
        for asset_type, md5s in wanted_md5_by_type.items()
        if md5s
    }
    if not pending:
        return matches

    for asset_type, directory in _iter_legacy_media_directories(search_roots, account_hints=account_hints):
        wanted = pending.get(asset_type)
        if not wanted:
            continue
        for path, digest in _iter_legacy_md5_rows(
            directory,
            asset_type=asset_type,
            cache_dir=media_cache_dir,
            month_hints=month_hints,
            time_window_ms=time_window_ms,
        ):
            if not path.is_file():
                continue
            if not digest or digest not in wanted:
                continue
            key = (asset_type, digest)
            if key not in matches:
                matches[key] = path.resolve()
                wanted.remove(digest)
            if not wanted:
                break
        if all(not remaining for remaining in pending.values()):
            break
    return matches


def _iter_legacy_media_directories(
    search_roots: list[Path],
    *,
    account_hints: set[str],
) -> Iterable[tuple[str, Path]]:
    seen: set[Path] = set()
    relative_candidates = [
        ("image", Path("Image") / "Group2"),
        ("image", Path("Image") / "C2C"),
        ("image", Path("Image") / "PicFileThumbnails"),
        ("video", Path("Video")),
        ("speech", Path("Audio")),
        ("file", Path("FileRecv")),
    ]
    for root in search_roots:
        parent_candidates = _legacy_parent_candidates(root, account_hints=account_hints)
        for parent in parent_candidates:
            for asset_type, suffix in relative_candidates:
                directory = (parent / suffix).resolve()
                if directory.exists() and directory.is_dir() and directory not in seen:
                    seen.add(directory)
                    yield asset_type, directory


def _iter_targeted_name_search_directories(
    search_roots: list[Path],
    *,
    account_hints: set[str],
    asset_type: str,
    source_path: str | None,
) -> Iterable[Path]:
    seen: set[Path] = set()

    def emit(path: Path) -> Iterable[Path]:
        resolved = path.resolve()
        if resolved.exists() and resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            yield resolved

    ntqq_suffixes_by_type: dict[str, list[list[str]]] = {
        "file": [
            ["nt_qq", "nt_data", "File"],
            ["nt_qq", "nt_data", "FileRecv"],
            ["ScreenRecorder"],
            ["Video"],
            ["FileRecv"],
        ],
        "video": [
            ["nt_qq", "nt_data", "Video"],
            ["Video"],
            ["ScreenRecorder"],
            ["FileRecv"],
        ],
        "speech": [
            ["nt_qq", "nt_data", "Ptt"],
            ["Audio"],
        ],
        "image": [
            ["nt_qq", "nt_data", "Pic"],
            ["nt_qq", "nt_data", "Emoji"],
            ["Image"],
        ],
        "sticker": [
            ["nt_qq", "nt_data", "Emoji"],
            ["ExpressionRecommend"],
            ["Image"],
        ],
    }

    source = _string_or_none(source_path)
    if source:
        source_parts = list(PureWindowsPath(source).parts)
        for root in search_roots:
            for suffix in _candidate_suffixes(source_parts):
                trial = root.joinpath(*suffix)
                yield from emit(trial.parent if trial.suffix else trial)

    for root in search_roots:
        for parent in _legacy_parent_candidates(root, account_hints=account_hints):
            for suffix in ntqq_suffixes_by_type.get(asset_type, []):
                yield from emit(parent.joinpath(*suffix))


def _legacy_parent_candidates(root: Path, *, account_hints: set[str]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved.exists() and resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    add(root)
    root_name = root.name.strip()
    if root_name.isdigit():
        return candidates

    if account_hints:
        for hint in account_hints:
            add(root / hint)
        return candidates

    with suppress(Exception):
        for child in root.iterdir():
            if child.is_dir():
                add(child)
    return candidates


def _ntqq_parent_candidates(root: Path, *, account_hints: set[str]) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        resolved = path.resolve()
        if resolved.exists() and resolved.is_dir() and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    add(root)
    if root.name.strip().isdigit():
        return candidates
    for hint in account_hints:
        add(root / hint)
    if account_hints:
        return candidates
    with suppress(Exception):
        for child in root.iterdir():
            if child.is_dir():
                add(child)
    return candidates


def _preferred_media_stem(candidate: _AssetCandidate, leaf_name: str) -> str:
    for value in [candidate.md5, candidate.file_name, leaf_name]:
        text = _string_or_none(value)
        if not text:
            continue
        return _strip_thumb_suffix(Path(text).stem if Path(text).suffix else text)
    return _short_hash(leaf_name)


def _strip_thumb_suffix(value: str) -> str:
    lowered = value.casefold()
    if lowered.endswith("_0"):
        return value[:-2]
    return value


def _looks_like_thumbnail_path(path: Path) -> bool:
    lowered_parts = {part.casefold() for part in path.parts}
    return "thumb" in lowered_parts or "picfilethumbnails" in lowered_parts


def _find_ntqq_original_siblings(path: Path) -> Path | None:
    parent_name = path.parent.name.casefold()
    if parent_name not in {"thumb", "picfilethumbnails"}:
        return None
    stem = _strip_thumb_suffix(path.stem)
    base = path.parent.parent
    for directory in [base / "Ori", base / "OriTemp"]:
        if not directory.exists() or not directory.is_dir():
            continue
        match = _find_candidate_in_directory(directory, stem=stem, asset_type="image")
        if match is not None:
            return match
    return None


def _find_candidate_in_directory(directory: Path, *, stem: str, asset_type: str) -> Path | None:
    candidates: list[Path] = []
    direct = directory / stem
    if direct.exists() and direct.is_file():
        candidates.append(direct.resolve())
    with suppress(Exception):
        candidates.extend(
            sorted(
                candidate.resolve()
                for candidate in directory.glob(f"{stem}.*")
                if candidate.is_file() and _legacy_extension_allowed(asset_type, candidate)
            )
        )
        candidates.extend(
            sorted(
                candidate.resolve()
                for candidate in directory.glob(f"{stem}_*.*")
                if candidate.is_file() and _legacy_extension_allowed(asset_type, candidate)
            )
        )
        candidates.extend(
            sorted(
                candidate.resolve()
                for candidate in directory.glob(f"{stem}_*")
                if candidate.is_file() and _legacy_extension_allowed(asset_type, candidate)
            )
        )
    if not candidates:
        return None
    unique_candidates = {candidate.resolve(): None for candidate in candidates}
    return sorted(unique_candidates, key=_original_candidate_priority)[0]


def _original_candidate_priority(path: Path) -> tuple[int, str]:
    suffix = path.suffix.casefold()
    order = {
        ".gif": 0,
        ".webp": 1,
        ".png": 2,
        ".jpg": 3,
        ".jpeg": 4,
        ".bmp": 5,
        "": 6,
    }
    name_stem = path.stem if path.suffix else path.name
    match = re.search(r"_(\d+)$", name_stem)
    has_variant = 1 if match else 0
    variant_rank = -int(match.group(1)) if match else 0
    return (order.get(suffix, 99), has_variant, variant_rank, str(path))


def _legacy_extension_allowed(asset_type: str, path: Path) -> bool:
    suffix = path.suffix.lower()
    if asset_type == "image":
        return suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} or suffix == ""
    if asset_type == "video":
        return suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm"} or suffix == ""
    if asset_type == "speech":
        return suffix in {".amr", ".silk", ".ogg", ".wav", ".mp3"} or suffix == ""
    if asset_type == "file":
        return True
    return False


def _iter_legacy_md5_rows(
    directory: Path,
    *,
    asset_type: str,
    cache_dir: Path | None,
    month_hints: set[str] | None = None,
    time_window_ms: tuple[int, int] | None = None,
) -> list[tuple[Path, str | None]]:
    files_with_stat: list[tuple[Path, object]] = []
    for path in directory.rglob("*"):
        if not path.is_file() or not _legacy_extension_allowed(asset_type, path):
            continue
        stat = path.stat()
        if not _legacy_file_in_scope(
            path,
            stat,
            month_hints=month_hints or set(),
            time_window_ms=time_window_ms,
        ):
            continue
        files_with_stat.append((path, stat))
    files_with_stat.sort(key=lambda item: str(item[0]).lower())
    rows_with_digest: list[tuple[Path, str | None]] = []
    if cache_dir is None:
        for path, _stat in files_with_stat:
            rows_with_digest.append((path, _file_md5(path)))
        return rows_with_digest

    cache = _load_legacy_md5_cache(directory, cache_dir=cache_dir)
    refreshed_map = {
        str(item["path"]).lower(): item
        for item in cache.get("files", [])
        if _string_or_none(item.get("path")) is not None
    }
    cached_rows = list(cache.get("files", []))
    cache_map = {str(item["path"]).lower(): item for item in cache.get("files", [])}
    for path, stat in files_with_stat:
        key = str(path.resolve()).lower()
        cached = cache_map.get(key)
        digest: str | None
        if (
            cached is not None
            and int(cached.get("size", -1)) == stat.st_size
            and int(cached.get("mtime_ns", -1)) == stat.st_mtime_ns
        ):
            digest = str(cached.get("md5") or "") or None
        else:
            digest = _file_md5(path)
        refreshed_map[key] = {
            "path": str(path.resolve()),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "md5": digest,
        }
        rows_with_digest.append((path, digest))
    refreshed = sorted(refreshed_map.values(), key=lambda item: str(item["path"]).lower())
    if refreshed != cached_rows:
        _write_legacy_md5_cache(directory, cache_dir=cache_dir, rows=refreshed)
    return rows_with_digest


def _legacy_file_in_scope(
    path: Path,
    stat: object,
    *,
    month_hints: set[str],
    time_window_ms: tuple[int, int] | None,
) -> bool:
    file_months = _months_from_stat(stat)
    if month_hints and file_months and file_months.isdisjoint(month_hints):
        return False
    if time_window_ms is None:
        return True
    start_ms, end_ms = time_window_ms
    span_ms = max(0, end_ms - start_ms)
    if span_ms > 14 * 24 * 60 * 60 * 1000:
        return True
    slack_ms = 7 * 24 * 60 * 60 * 1000
    lower = start_ms - slack_ms
    upper = end_ms + slack_ms
    timestamps = _timestamps_ms_from_stat(stat)
    if not timestamps:
        return True
    return any(lower <= value <= upper for value in timestamps)


def _file_md5(path: Path) -> str | None:
    try:
        digest = hashlib.md5()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()
    except OSError:
        return None


def _normalized_md5(value: str | None) -> str | None:
    text = _string_or_none(value)
    return text.lower() if text else None


def _derive_media_time_window(
    snapshot: NormalizedSnapshot,
    candidates: list[_AssetCandidate],
) -> tuple[int, int] | None:
    explicit_bounds: list[int] = []
    for key in ("resolved_since", "resolved_until"):
        parsed = _metadata_datetime_ms(snapshot.metadata.get(key))
        if parsed is not None:
            explicit_bounds.append(parsed)
    if len(explicit_bounds) == 2:
        return min(explicit_bounds), max(explicit_bounds)

    timestamps = [candidate.timestamp_ms for candidate in candidates if candidate.timestamp_ms > 0]
    if not timestamps:
        timestamps = [message.timestamp_ms for message in snapshot.messages if message.timestamp_ms > 0]
    if not timestamps:
        return None
    return min(timestamps), max(timestamps)


def _derive_month_hints(
    candidates: list[_AssetCandidate],
    *,
    time_window_ms: tuple[int, int] | None,
) -> set[str]:
    hints: set[str] = set()
    for candidate in candidates:
        if candidate.source_path:
            hints.update(_extract_month_tokens(candidate.source_path))
    if time_window_ms is not None:
        hints.update(_month_tokens_between(*time_window_ms))
    return hints


def _metadata_datetime_ms(value: object) -> int | None:
    text = _string_or_none(value)
    if not text:
        return None
    with suppress(ValueError):
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    return None


def _extract_month_tokens(value: str) -> set[str]:
    return set(re.findall(r"\b\d{4}-\d{2}\b", value))


def _month_tokens_between(start_ms: int, end_ms: int) -> set[str]:
    start_dt = datetime.fromtimestamp(min(start_ms, end_ms) / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(max(start_ms, end_ms) / 1000, tz=timezone.utc)
    cursor = datetime(start_dt.year, start_dt.month, 1, tzinfo=timezone.utc)
    end_cursor = datetime(end_dt.year, end_dt.month, 1, tzinfo=timezone.utc)
    months: set[str] = set()
    while cursor <= end_cursor:
        months.add(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = datetime(cursor.year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            cursor = datetime(cursor.year, cursor.month + 1, 1, tzinfo=timezone.utc)
    return months


def _months_from_stat(stat: object) -> set[str]:
    months: set[str] = set()
    for value in _timestamps_ms_from_stat(stat):
        months.add(datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m"))
    return months


def _timestamps_ms_from_stat(stat: object) -> list[int]:
    values: list[int] = []
    for attr in ("st_mtime_ns", "st_ctime_ns"):
        raw = getattr(stat, attr, None)
        if raw is None:
            continue
        values.append(int(raw // 1_000_000))
    return values


def _extract_account_hints(source_path: str | None) -> set[str]:
    if not source_path:
        return set()
    hints = set()
    for part in PureWindowsPath(source_path).parts:
        text = str(part).strip()
        if text.isdigit() and len(text) >= 5:
            hints.add(text)
    return hints


def _load_legacy_md5_cache(directory: Path, *, cache_dir: Path) -> dict[str, object]:
    cache_path = _legacy_md5_cache_path(directory, cache_dir=cache_dir)
    if not cache_path.exists():
        return {"files": []}
    try:
        return orjson.loads(cache_path.read_bytes())
    except Exception:
        return {"files": []}


def _write_legacy_md5_cache(
    directory: Path,
    *,
    cache_dir: Path,
    rows: list[dict[str, object]],
) -> None:
    cache_path = _legacy_md5_cache_path(directory, cache_dir=cache_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "directory": str(directory.resolve()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": rows,
    }
    atomic_write_bytes(cache_path, orjson.dumps(payload))


def _legacy_md5_cache_path(directory: Path, *, cache_dir: Path) -> Path:
    digest = hashlib.sha1(str(directory.resolve()).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"legacy_md5_{digest}.json"


def _build_export_rel_path(candidate: _AssetCandidate, resolved_path: Path) -> Path:
    folder = {
        "image": "images",
        "video": "videos",
        "speech": "audio",
        "file": "files",
        "sticker": "stickers",
    }[candidate.asset_type]
    preferred_name = candidate.file_name or resolved_path.name or f"{candidate.asset_type}_{_short_hash(str(resolved_path))}"
    file_name = _normalize_file_name(preferred_name, resolved_path=resolved_path, asset_type=candidate.asset_type)
    if candidate.asset_role:
        return Path(folder) / candidate.asset_role / file_name
    return Path(folder) / file_name


def _allocate_export_rel_path(
    candidate: _AssetCandidate,
    resolved_path: Path,
    *,
    dedupe_key: str,
    occupied_export_paths: dict[str, str],
) -> Path:
    preferred = _build_export_rel_path(candidate, resolved_path)
    preferred_key = preferred.as_posix().casefold()
    existing_owner = occupied_export_paths.get(preferred_key)
    if existing_owner in {None, dedupe_key}:
        return preferred

    stem = preferred.stem
    suffix = preferred.suffix
    parent = preferred.parent
    collision_suffix = _short_hash(str(resolved_path.resolve()))
    candidate_path = parent / f"{stem}_{collision_suffix}{suffix}"
    candidate_key = candidate_path.as_posix().casefold()
    if occupied_export_paths.get(candidate_key) in {None, dedupe_key}:
        return candidate_path

    for index in range(2, 1000):
        numbered = parent / f"{stem}_{collision_suffix}_{index}{suffix}"
        numbered_key = numbered.as_posix().casefold()
        if occupied_export_paths.get(numbered_key) in {None, dedupe_key}:
            return numbered
    return candidate_path


def _normalize_file_name(name: str, *, resolved_path: Path, asset_type: str) -> str:
    clean = "".join(char if char not in '<>:"/\\|?*' else "_" for char in name).strip() or f"{asset_type}_{_short_hash(str(resolved_path))}"
    suffix = Path(clean).suffix.lower()
    if suffix and _has_trusted_media_suffix(asset_type=asset_type, suffix=suffix):
        return clean
    guessed = _guess_extension(resolved_path)
    if suffix:
        if _should_replace_suffix(asset_type=asset_type, current_suffix=suffix, guessed_suffix=guessed):
            return Path(clean).with_suffix(guessed).name
        return clean
    return f"{clean}{guessed}" if guessed else clean


def _should_replace_suffix(*, asset_type: str, current_suffix: str, guessed_suffix: str) -> bool:
    if not guessed_suffix:
        return False
    if asset_type not in {"image", "sticker", "video", "speech"}:
        return False
    equivalent_groups = [
        {".jpg", ".jpeg"},
    ]
    if current_suffix == guessed_suffix:
        return False
    if any({current_suffix, guessed_suffix}.issubset(group) for group in equivalent_groups):
        return False
    return True


def _has_trusted_media_suffix(*, asset_type: str, suffix: str) -> bool:
    normalized = suffix.lower()
    trusted_suffixes = {
        "image": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
        "sticker": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"},
        "video": {".mp4", ".mov", ".avi", ".mkv", ".webm"},
        "speech": {".amr", ".silk", ".ogg", ".wav", ".mp3", ".m4a"},
    }.get(asset_type)
    if not trusted_suffixes:
        return False
    return normalized in trusted_suffixes


def _guess_extension(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except Exception:
        return path.suffix
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    if header.startswith(b"BM"):
        return ".bmp"
    if header.startswith(b"#!AMR"):
        return ".amr"
    if header.startswith(b"#!SILK_V3"):
        return ".silk"
    if header.startswith(b"OggS"):
        return ".ogg"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return ".wav"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return ".mp4"
    return path.suffix


def _existing_path(value: str | None) -> Path | None:
    text = _string_or_none(value)
    if not text:
        return None
    candidate = Path(PureWindowsPath(text))
    if candidate.exists() and candidate.is_file():
        return candidate.resolve()
    return None


def _string_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _local_path_from_download_hint(hint: dict[str, Any] | None) -> str | None:
    if not isinstance(hint, dict):
        return None
    for key in ("path", "file", "url"):
        text = _string_or_none(hint.get(key))
        if text and _looks_like_local_path(text):
            return text
    return None


def _looks_like_local_path(value: str) -> bool:
    text = _string_or_none(value)
    if not text:
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith("\\\\"))


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _summarize_assets(assets: list[MaterializedAsset]) -> dict[str, int]:
    return {
        "copied": sum(1 for item in assets if item.status == "copied"),
        "reused": sum(1 for item in assets if item.status == "reused"),
        "missing": sum(1 for item in assets if item.status == "missing"),
        "error": sum(1 for item in assets if item.status == "error"),
        "total": len(assets),
    }


def _summarize_missing_breakdown(assets: list[MaterializedAsset]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in assets:
        if item.status != "missing":
            continue
        key = str(item.resolver or "missing").strip() or "missing"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
