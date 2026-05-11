from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Mapping, Sequence

import orjson
from pydantic import BaseModel, ConfigDict, Field

try:
    from qq_data_core.paths import atomic_write_bytes, build_timestamp_token
except ImportError:
    def atomic_write_bytes(path: Path, payload: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_bytes(payload)
        temp_path.replace(path)
        return path

    def build_timestamp_token(*, include_pid: bool = False) -> str:
        token = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
        if include_pid:
            from os import getpid

            token = f"{token}_{getpid()}"
        return token

import qq_data_process.models as _process_models

if not hasattr(_process_models, "PROCESS_TIMEZONE"):
    _process_models.PROCESS_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
if not hasattr(_process_models, "ResourceState"):
    _process_models.ResourceState = Literal[
        "available",
        "missing",
        "expired",
        "placeholder",
        "unsupported",
    ]
if not hasattr(_process_models, "CorpusLineage"):
    class _CompatCorpusLineage(BaseModel):
        model_config = ConfigDict(extra="allow")

        source_export_id: str | None = None
        source_message_id: str | None = None
        source_asset_key: str | None = None
        source_chat_id: str | None = None
        extra: dict[str, Any] = Field(default_factory=dict)

    _process_models.CorpusLineage = _CompatCorpusLineage
if not hasattr(_process_models, "CorpusProvenance"):
    class _CompatCorpusProvenance(BaseModel):
        model_config = ConfigDict(extra="allow")

        build_profile: str = "preprocess"
        created_at: datetime = Field(
            default_factory=lambda: datetime.now(_process_models.PROCESS_TIMEZONE)
        )
        source_type: str | None = None
        source_path: str | None = None
        extra: dict[str, Any] = Field(default_factory=dict)

    _process_models.CorpusProvenance = _CompatCorpusProvenance

class AnalysisResult(BaseModel):
    status: str = "ok"
    summary: str = ""
    confidence: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)


class DeterministicResult(AnalysisResult):
    plugin_id: str = "compat"
    plugin_version: str = "0.0.0"
    modality_targets: list[str] = Field(default_factory=list)
    evidence_refs: list[Any] = Field(default_factory=list)
    verdict: str | None = None
    notes: list[str] = Field(default_factory=list)

AnalysisContext = Any
CorpusLineage = _process_models.CorpusLineage
CorpusProvenance = _process_models.CorpusProvenance

from qq_data_process.preprocess_models import (
    PreprocessAnnotation,
    PreprocessBuildReport,
    PreprocessDirective,
    PreprocessPluginProvenance,
    PreprocessViewContext,
    PreprocessViewManifest,
    ProcessedAssetView,
    ProcessedMessageView,
    ProcessedThreadView,
)
from qq_data_process.preprocess_types import DeliveryProfile

from .preprocess_registry import PreprocessorPlugin, PreprocessorRegistry, PreprocessorSpec, register_preprocessor

__all__ = [
    "PreprocessRunResult",
    "build_preprocess_view",
    "run_preprocess",
    "load_preprocess_view",
]


@dataclass(frozen=True)
class PreprocessRunPaths:
    run_id: str
    root_dir: Path
    run_dir: Path
    manifest_path: Path
    registry_path: Path
    messages_path: Path
    threads_path: Path
    assets_path: Path
    build_report_path: Path


@dataclass(frozen=True)
class PreprocessExecutionRecord:
    preprocessor_id: str
    scope_level: str
    status: str
    started_at: str
    finished_at: str
    duration_s: float
    requires: tuple[str, ...] = field(default_factory=tuple)
    produces: tuple[str, ...] = field(default_factory=tuple)
    result_count: int = 0
    error: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PreprocessRunResult:
    run_id: str
    view_id: str
    run_dir: Path
    manifest_path: Path
    messages_path: Path
    threads_path: Path
    assets_path: Path
    build_report_path: Path
    records: tuple[PreprocessExecutionRecord, ...]
    result_count: int
    summary: Mapping[str, Any]


@dataclass(frozen=True)
class PreprocessRunRequest:
    context: AnalysisContext
    preprocessor_ids: Sequence[str] = field(default_factory=tuple)
    view_id: str | None = None
    output_root: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    directive: PreprocessDirective | None = None
    fail_fast: bool = False


@dataclass(frozen=True)
class _PreprocessorContextAdapter:
    corpus: AnalysisContext
    run_id: str
    run_dir: Path
    options: Mapping[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.corpus, name)


def build_preprocess_view(
    context: AnalysisContext,
    *,
    preprocessors: Sequence[PreprocessorPlugin],
    preprocessor_ids: Sequence[str] | None = None,
    view_id: str | None = None,
    output_root: Path | str | None = None,
    metadata: Mapping[str, Any] | None = None,
    directive: PreprocessDirective | Mapping[str, Any] | None = None,
    fail_fast: bool = False,
) -> PreprocessRunResult:
    return run_preprocess(
        context,
        preprocessors=preprocessors,
        preprocessor_ids=preprocessor_ids,
        view_id=view_id,
        output_root=output_root,
        metadata=metadata,
        directive=directive,
        fail_fast=fail_fast,
    )


def run_preprocess(
    context: AnalysisContext,
    *,
    preprocessors: Sequence[PreprocessorPlugin],
    preprocessor_ids: Sequence[str] | None = None,
    view_id: str | None = None,
    output_root: Path | str | None = None,
    metadata: Mapping[str, Any] | None = None,
    directive: PreprocessDirective | Mapping[str, Any] | None = None,
    fail_fast: bool = False,
) -> PreprocessRunResult:
    registry = PreprocessorRegistry()
    for plugin in preprocessors:
        register_preprocessor(registry, plugin)

    request = PreprocessRunRequest(
        context=context,
        preprocessor_ids=tuple(preprocessor_ids or ()),
        view_id=view_id,
        output_root=Path(output_root) if output_root is not None else None,
        metadata=dict(metadata or {}),
        directive=_directive_from_value(directive or (metadata or {}).get("directive")),
        fail_fast=fail_fast,
    )
    return _execute_preprocess_run(request, registry)


def load_preprocess_view(view_path: str | Path) -> PreprocessViewContext:
    root = Path(view_path).expanduser().resolve()
    manifest_path = root if root.is_file() else root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Preprocess view manifest does not exist: {manifest_path}")

    manifest = PreprocessViewManifest.model_validate(_read_json(manifest_path))
    base_dir = manifest_path.parent
    outputs = manifest.outputs or {}
    messages_path = _resolve_output_path(base_dir, outputs.get("messages"), default_name="messages.jsonl")
    threads_path = _resolve_output_path(base_dir, outputs.get("threads"), default_name="threads.jsonl")
    assets_path = _resolve_output_path(base_dir, outputs.get("assets"), default_name="assets.jsonl")

    message_views = _load_typed_jsonl(messages_path, ProcessedMessageView)
    thread_views = _load_typed_jsonl(threads_path, ProcessedThreadView)
    asset_views = _load_typed_jsonl(assets_path, ProcessedAssetView)

    annotations, artifacts = _collect_nested_preprocess_objects(
        message_views=message_views,
        thread_views=thread_views,
        asset_views=asset_views,
    )
    context_id = manifest.view_id
    lineage = manifest.lineage
    provenance = manifest.provenance
    return PreprocessViewContext(
        context_id=context_id,
        manifest=manifest,
        message_views=message_views,
        thread_views=thread_views,
        asset_views=asset_views,
        annotations=annotations,
        artifacts=artifacts,
        indexes=_build_preprocess_indexes(
            message_views=message_views,
            thread_views=thread_views,
            asset_views=asset_views,
            annotations=annotations,
        ),
        metadata={
            "view_dir": str(base_dir),
            "manifest_path": str(manifest_path),
            "delivery_profile": manifest.delivery_profile,
        },
        directive=manifest.directive,
        lineage=lineage,
        provenance=provenance,
    )


def _execute_preprocess_run(
    request: PreprocessRunRequest,
    registry: PreprocessorRegistry,
) -> PreprocessRunResult:
    context = request.context
    paths = _prepare_preprocess_paths(context, output_root=request.output_root, view_id=request.view_id)
    specs = registry.resolve_execution_order(request.preprocessor_ids)
    execution_order = tuple(spec.preprocessor_id for spec in specs)
    started_at = datetime.now().astimezone()
    delivery_profile = _delivery_profile(context, request.metadata)
    directive = request.directive
    runtime_context = _PreprocessorContextAdapter(
        corpus=context,
        run_id=paths.run_id,
        run_dir=paths.run_dir,
        options={
            "delivery_profile": delivery_profile,
            "directive": directive.model_dump(mode="json") if directive is not None else None,
            **dict(request.metadata),
        },
    )

    write_json(paths.registry_path, registry.to_payload())

    records: list[PreprocessExecutionRecord] = []
    results_by_preprocessor: dict[str, list[Any]] = {}
    failed_ids: set[str] = set()
    message_views: list[ProcessedMessageView] = []
    thread_views: list[ProcessedThreadView] = []
    asset_views: list[ProcessedAssetView] = []
    annotations: list[PreprocessAnnotation] = []
    total_results = 0

    for spec in specs:
        blocked_dependencies = [dependency for dependency in spec.requires if dependency in failed_ids]
        if blocked_dependencies:
            records.append(_blocked_record(spec, blocked_dependencies))
            failed_ids.add(spec.preprocessor_id)
            continue

        _bind_preprocessor_inputs(spec.plugin, spec.requires, results_by_preprocessor)
        started = datetime.now().astimezone()
        started_perf = perf_counter()
        try:
            results = list(spec.plugin.run(runtime_context) or [])
            total_results += len(results)
            results_by_preprocessor[spec.preprocessor_id] = results
            derived = _normalize_plugin_outputs(
                context=context,
                spec=spec,
                results=results,
                view_id=paths.run_id,
                delivery_profile=delivery_profile,
            )
            message_views.extend(derived["message_views"])
            thread_views.extend(derived["thread_views"])
            asset_views.extend(derived["asset_views"])
            annotations.extend(derived["annotations"])
            records.append(
                PreprocessExecutionRecord(
                    preprocessor_id=spec.preprocessor_id,
                    scope_level=spec.scope_level,
                    status="ok",
                    started_at=started.isoformat(),
                    finished_at=datetime.now().astimezone().isoformat(),
                    duration_s=round(perf_counter() - started_perf, 6),
                    requires=spec.requires,
                    produces=spec.produces,
                    result_count=len(results),
                )
            )
        except Exception as exc:
            records.append(
                PreprocessExecutionRecord(
                    preprocessor_id=spec.preprocessor_id,
                    scope_level=spec.scope_level,
                    status="error",
                    started_at=started.isoformat(),
                    finished_at=datetime.now().astimezone().isoformat(),
                    duration_s=round(perf_counter() - started_perf, 6),
                    requires=spec.requires,
                    produces=spec.produces,
                    error={
                        "type": exc.__class__.__name__,
                        "message": str(exc),
                    },
                )
            )
            failed_ids.add(spec.preprocessor_id)
            if request.fail_fast:
                break

    write_jsonl(paths.messages_path, [item.model_dump(mode="json") for item in message_views])
    write_jsonl(paths.threads_path, [item.model_dump(mode="json") for item in thread_views])
    write_jsonl(paths.assets_path, [item.model_dump(mode="json") for item in asset_views])

    runtime_provenance = PreprocessPluginProvenance(
        plugin_id="preprocess_runtime",
        plugin_version="0.1.0",
        build_profile="preprocess_runtime",
    )
    manifest_provenance = context.provenance or context.manifest.provenance or CorpusProvenance(build_profile="preprocess")
    lineage = context.lineage or context.manifest.lineage
    build_report = PreprocessBuildReport(
        run_id=paths.run_id,
        view_id=paths.run_id,
        started_at=started_at,
        finished_at=datetime.now().astimezone(),
        processed_message_count=len(message_views),
        processed_thread_count=len(thread_views),
        processed_asset_count=len(asset_views),
        annotation_count=len(annotations),
        warnings=[],
        errors=[record.error["message"] for record in records if record.error],
        stats={
            "execution_order": list(execution_order),
            "result_count": total_results,
            "source_context_id": context.context_id,
            "delivery_profile": delivery_profile,
            "record_count": len(records),
            "directive_present": directive is not None,
        },
        lineage=lineage,
        provenance=runtime_provenance,
    )
    build_report.elapsed_s = max((build_report.finished_at - build_report.started_at).total_seconds(), 0.0)

    manifest = PreprocessViewManifest(
        view_id=paths.run_id,
        corpus_id=context.context_id,
        chat_type=context.manifest.chat_type,
        chat_id=context.manifest.chat_id,
        chat_name=context.manifest.chat_name,
        view_kind=_view_kind_from_delivery(delivery_profile),
        delivery_profile=delivery_profile,
        processed_message_count=len(message_views),
        processed_thread_count=len(thread_views),
        processed_asset_count=len(asset_views),
        annotation_count=len(annotations),
        source_exports=list(context.manifest.source_exports),
        outputs={
            "manifest": str(paths.manifest_path),
            "messages": str(paths.messages_path),
            "threads": str(paths.threads_path),
            "assets": str(paths.assets_path),
            "build_report": str(paths.build_report_path),
        },
        metadata={
            **dict(request.metadata),
            "execution_order": list(execution_order),
            "annotation_count": len(annotations),
        },
        directive=directive,
        lineage=lineage,
        provenance=manifest_provenance,
        build_report=build_report,
    )
    write_json(paths.manifest_path, manifest.model_dump(mode="json"))
    write_json(paths.build_report_path, build_report.model_dump(mode="json"))

    summary = {
        "run_id": paths.run_id,
        "view_id": paths.run_id,
        "execution_order": list(execution_order),
        "success_count": sum(1 for record in records if record.status == "ok"),
        "error_count": sum(1 for record in records if record.status == "error"),
        "blocked_count": sum(1 for record in records if record.status == "blocked"),
        "result_count": total_results,
        "message_count": len(message_views),
        "thread_count": len(thread_views),
        "asset_count": len(asset_views),
        "annotation_count": len(annotations),
        "delivery_profile": delivery_profile,
        "directive_present": directive is not None,
    }

    return PreprocessRunResult(
        run_id=paths.run_id,
        view_id=paths.run_id,
        run_dir=paths.run_dir,
        manifest_path=paths.manifest_path,
        messages_path=paths.messages_path,
        threads_path=paths.threads_path,
        assets_path=paths.assets_path,
        build_report_path=paths.build_report_path,
        records=tuple(records),
        result_count=total_results,
        summary=summary,
    )


def _normalize_plugin_outputs(
    *,
    context: AnalysisContext,
    spec: PreprocessorSpec,
    results: Sequence[Any],
    view_id: str,
    delivery_profile: DeliveryProfile,
) -> dict[str, list[Any]]:
    message_views: list[ProcessedMessageView] = []
    thread_views: list[ProcessedThreadView] = []
    asset_views: list[ProcessedAssetView] = []
    annotations: list[PreprocessAnnotation] = []

    for item in results:
        if hasattr(item, "asset_view") and hasattr(item, "annotation"):
            asset_view = getattr(item, "asset_view")
            annotation = getattr(item, "annotation")
            if isinstance(asset_view, ProcessedAssetView):
                asset_views.append(asset_view)
            if isinstance(annotation, PreprocessAnnotation):
                annotations.append(annotation)
            continue

        if _is_analysis_result_like(item):
            annotation = _annotation_from_analysis_result(
                result=item,
                spec=spec,
                context=context,
                view_id=view_id,
            )
            annotations.append(annotation)
            scope = annotation.scope_level
            if scope in {"thread", "topic"}:
                thread_views.append(
                    _thread_view_from_annotation(
                        annotation=annotation,
                        result=item,
                        context=context,
                        view_id=view_id,
                        delivery_profile=delivery_profile,
                    )
                )
            elif scope == "asset":
                asset_views.append(
                    _asset_view_from_annotation(
                        annotation=annotation,
                        result=item,
                        context=context,
                        view_id=view_id,
                        delivery_profile=delivery_profile,
                    )
                )
            else:
                message_views.append(
                    _message_view_from_annotation(
                        annotation=annotation,
                        result=item,
                        context=context,
                        view_id=view_id,
                        delivery_profile=delivery_profile,
                    )
                )

    return {
        "message_views": message_views,
        "thread_views": thread_views,
        "asset_views": asset_views,
        "annotations": annotations,
    }


def _is_analysis_result_like(item: Any) -> bool:
    if isinstance(item, (AnalysisResult, DeterministicResult)):
        return True
    if item is None:
        return False
    return all(
        hasattr(item, name)
        for name in ("status", "summary", "confidence", "details", "model_dump")
    )


def _annotation_from_analysis_result(
    *,
    result: AnalysisResult,
    spec: PreprocessorSpec,
    context: AnalysisContext,
    view_id: str,
) -> PreprocessAnnotation:
    details = result.details or {}
    derived = details.get("derived_annotation") if isinstance(details.get("derived_annotation"), Mapping) else {}
    source_message_ids = _string_list(details.get("source_message_ids")) or _string_list(derived.get("source_message_ids"))
    source_asset_ids = _string_list(details.get("asset_ids")) or _string_list(derived.get("asset_ids"))
    source_thread_ids = _string_list(details.get("thread_ids"))
    operation_type = str(details.get("operation_type") or _default_operation_for_scope(spec.scope_level))
    scope_level = str(details.get("scope_level") or spec.scope_level)
    lineage = _lineage_from_context(
        context,
        source_message_id=source_message_ids[0] if source_message_ids else None,
        source_asset_key=source_asset_ids[0] if source_asset_ids else None,
    )
    provenance = PreprocessPluginProvenance(
        plugin_id=spec.preprocessor_id,
        plugin_version=spec.plugin_version,
        build_profile="preprocess",
    )
    return PreprocessAnnotation(
        annotation_id=_make_id("annotation", spec.preprocessor_id, view_id, result.summary, *source_message_ids, *source_asset_ids),
        operation_type=operation_type,
        scope_level=scope_level,
        label=spec.preprocessor_id,
        summary=result.summary,
        decision_summary=str(details.get("decision_summary") or derived.get("reason") or result.summary),
        confidence=float(result.confidence),
        source_message_ids=source_message_ids,
        source_asset_ids=source_asset_ids,
        source_thread_ids=source_thread_ids,
        target_ids=[],
        tags=_string_list(getattr(result, "tags", [])),
        metadata={
            "result_status": result.status,
            "details": result.model_dump(mode="json").get("details", {}),
        },
        lineage=lineage,
        provenance=provenance,
    )


def _message_view_from_annotation(
    *,
    annotation: PreprocessAnnotation,
    result: AnalysisResult,
    context: AnalysisContext,
    view_id: str,
    delivery_profile: DeliveryProfile,
) -> ProcessedMessageView:
    source_message_id = annotation.source_message_ids[0] if annotation.source_message_ids else None
    source_message = next((item for item in context.messages if item.message_id == source_message_id), None)
    return ProcessedMessageView(
        processed_message_id=_make_id("processed_message", annotation.annotation_id, view_id),
        view_id=view_id,
        message_id=source_message_id,
        chat_type=context.manifest.chat_type,
        chat_id=context.manifest.chat_id,
        source_message_ids=list(annotation.source_message_ids),
        source_asset_ids=list(annotation.source_asset_ids),
        operation_type=annotation.operation_type,
        delivery_profile=delivery_profile,
        raw_text=source_message.text_content if source_message is not None else None,
        processed_text=result.summary,
        summary=annotation.summary,
        decision_summary=annotation.decision_summary,
        confidence=annotation.confidence,
        suppressed=annotation.operation_type == "suppress",
        labels=list(annotation.tags),
        annotations=[annotation],
        metadata=dict(annotation.metadata),
        lineage=annotation.lineage,
        provenance=annotation.provenance,
    )


def _thread_view_from_annotation(
    *,
    annotation: PreprocessAnnotation,
    result: AnalysisResult,
    context: AnalysisContext,
    view_id: str,
    delivery_profile: DeliveryProfile,
) -> ProcessedThreadView:
    details = result.details or {}
    derived = details.get("derived_annotation") if isinstance(details.get("derived_annotation"), Mapping) else {}
    thread_id = _string_or_none(details.get("thread_id")) or _string_or_none(derived.get("thread_id"))
    return ProcessedThreadView(
        processed_thread_id=_make_id("processed_thread", annotation.annotation_id, view_id),
        view_id=view_id,
        thread_id=thread_id,
        chat_type=context.manifest.chat_type,
        chat_id=context.manifest.chat_id,
        source_message_ids=list(annotation.source_message_ids),
        source_asset_ids=list(annotation.source_asset_ids),
        operation_type=annotation.operation_type,
        delivery_profile=delivery_profile,
        title=_string_or_none(derived.get("window_id")) or _string_or_none(derived.get("thread_id")),
        summary=annotation.summary,
        decision_summary=annotation.decision_summary,
        confidence=annotation.confidence,
        labels=list(annotation.tags),
        annotations=[annotation],
        metadata=dict(annotation.metadata),
        lineage=annotation.lineage,
        provenance=annotation.provenance,
    )


def _asset_view_from_annotation(
    *,
    annotation: PreprocessAnnotation,
    result: AnalysisResult,
    context: AnalysisContext,
    view_id: str,
    delivery_profile: DeliveryProfile,
) -> ProcessedAssetView:
    details = result.details or {}
    source_asset_id = annotation.source_asset_ids[0] if annotation.source_asset_ids else None
    source_asset = next((item for item in context.assets if item.asset_id == source_asset_id), None)
    return ProcessedAssetView(
        processed_asset_id=_make_id("processed_asset", annotation.annotation_id, view_id),
        view_id=view_id,
        asset_id=source_asset_id,
        message_id=annotation.source_message_ids[0] if annotation.source_message_ids else None,
        asset_type=(
            _string_or_none(details.get("asset_type"))
            or (source_asset.asset_type if source_asset is not None else "file")
        ),
        resource_state=(
            _string_or_none(details.get("resource_state"))
            or (source_asset.resource_state if source_asset is not None else "unsupported")
        ),
        file_name=_string_or_none(details.get("file_name")) or (source_asset.file_name if source_asset is not None else None),
        source_message_ids=list(annotation.source_message_ids),
        source_asset_ids=list(annotation.source_asset_ids),
        operation_type=annotation.operation_type,
        delivery_profile=delivery_profile,
        caption=None,
        summary=annotation.summary,
        decision_summary=annotation.decision_summary,
        confidence=annotation.confidence,
        labels=list(annotation.tags),
        annotations=[annotation],
        metadata=dict(annotation.metadata),
        lineage=annotation.lineage,
        provenance=annotation.provenance,
    )


def _prepare_preprocess_paths(
    context: AnalysisContext,
    *,
    output_root: Path | None,
    view_id: str | None,
) -> PreprocessRunPaths:
    run_id = view_id or build_timestamp_token(include_pid=True)
    root_dir = output_root or _default_preprocess_root(context)
    root_dir = root_dir.expanduser().resolve()
    run_dir = root_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return PreprocessRunPaths(
        run_id=run_id,
        root_dir=root_dir,
        run_dir=run_dir,
        manifest_path=run_dir / "manifest.json",
        registry_path=run_dir / "registry.json",
        messages_path=run_dir / "messages.jsonl",
        threads_path=run_dir / "threads.jsonl",
        assets_path=run_dir / "assets.jsonl",
        build_report_path=run_dir / "build_report.json",
    )


def _resolve_output_path(base_dir: Path, configured: str | None, *, default_name: str) -> Path:
    if configured:
        configured_path = Path(configured).expanduser()
        if not configured_path.is_absolute():
            configured_path = (base_dir / configured_path).resolve()
        else:
            configured_path = configured_path.resolve()
        return configured_path
    return (base_dir / default_name).resolve()


def _load_typed_jsonl(path: Path, model_type: Any) -> list[Any]:
    if not path.exists():
        return []
    rows = _read_jsonl(path)
    return [model_type.model_validate(row) for row in rows]


def _collect_nested_preprocess_objects(
    *,
    message_views: Sequence[ProcessedMessageView],
    thread_views: Sequence[ProcessedThreadView],
    asset_views: Sequence[ProcessedAssetView],
) -> tuple[list[PreprocessAnnotation], list[Any]]:
    annotations_by_id: dict[str, PreprocessAnnotation] = {}
    artifacts_by_id: dict[str, Any] = {}
    for container in [*message_views, *thread_views, *asset_views]:
        for annotation in container.annotations:
            key = annotation.annotation_id or _make_id(
                "annotation",
                container.view_id,
                getattr(container, "message_id", None),
                getattr(container, "thread_id", None),
                getattr(container, "asset_id", None),
                annotation.summary,
            )
            annotations_by_id[key] = annotation
            for artifact in annotation.artifacts:
                artifact_key = artifact.artifact_id or _make_id(
                    "artifact",
                    key,
                    artifact.artifact_type,
                    artifact.path,
                )
                artifacts_by_id[artifact_key] = artifact
    return list(annotations_by_id.values()), list(artifacts_by_id.values())


def _build_preprocess_indexes(
    *,
    message_views: Sequence[ProcessedMessageView],
    thread_views: Sequence[ProcessedThreadView],
    asset_views: Sequence[ProcessedAssetView],
    annotations: Sequence[PreprocessAnnotation],
) -> dict[str, Any]:
    return {
        "message_views_by_message_id": {
            item.message_id: [view.processed_message_id for view in message_views if view.message_id == item.message_id]
            for item in message_views
            if item.message_id
        },
        "thread_views_by_thread_id": {
            item.thread_id: [view.processed_thread_id for view in thread_views if view.thread_id == item.thread_id]
            for item in thread_views
            if item.thread_id
        },
        "asset_views_by_asset_id": {
            item.asset_id: [view.processed_asset_id for view in asset_views if view.asset_id == item.asset_id]
            for item in asset_views
            if item.asset_id
        },
        "annotation_ids": [item.annotation_id for item in annotations if item.annotation_id],
    }


def _default_preprocess_root(context: AnalysisContext) -> Path:
    corpus_dir = context.metadata.get("corpus_dir")
    if corpus_dir:
        return Path(str(corpus_dir)) / "preprocess"
    return Path("state") / "preprocess_views"


def _delivery_profile(context: AnalysisContext, metadata: Mapping[str, Any]) -> DeliveryProfile:
    value = metadata.get("delivery_profile") or context.metadata.get("delivery_profile")
    text = str(value or "").strip()
    if text in {"raw_only", "processed_only", "raw_plus_processed"}:
        return text  # type: ignore[return-value]
    return "raw_plus_processed"


def _directive_from_value(value: PreprocessDirective | Mapping[str, Any] | None) -> PreprocessDirective | None:
    if value is None:
        return None
    if isinstance(value, PreprocessDirective):
        return value
    if isinstance(value, Mapping):
        return PreprocessDirective.model_validate(dict(value))
    raise TypeError("directive must be a PreprocessDirective, mapping, or None")


def _view_kind_from_delivery(delivery_profile: DeliveryProfile) -> str:
    if delivery_profile == "raw_only":
        return "raw_view"
    if delivery_profile == "processed_only":
        return "processed_view"
    return "processed_view"


def _bind_preprocessor_inputs(
    plugin: PreprocessorPlugin,
    requires: Sequence[str],
    results: Mapping[str, list[Any]],
) -> None:
    binder = getattr(plugin, "bind_inputs", None)
    if not callable(binder):
        return
    bound = {dependency: results[dependency] for dependency in requires if dependency in results}
    binder(bound)


def _blocked_record(
    spec: PreprocessorSpec,
    blocked_dependencies: Sequence[str],
) -> PreprocessExecutionRecord:
    now = datetime.now().astimezone().isoformat()
    return PreprocessExecutionRecord(
        preprocessor_id=spec.preprocessor_id,
        scope_level=spec.scope_level,
        status="blocked",
        started_at=now,
        finished_at=now,
        duration_s=0.0,
        requires=spec.requires,
        produces=spec.produces,
        error={
            "type": "dependency_blocked",
            "message": f"Blocked by failed dependencies: {', '.join(blocked_dependencies)}",
            "blocked_dependencies": list(blocked_dependencies),
        },
    )


def _lineage_from_context(
    context: AnalysisContext,
    *,
    source_message_id: str | None = None,
    source_asset_key: str | None = None,
) -> CorpusLineage:
    base = context.lineage or context.manifest.lineage
    return base.model_copy(
        update={
            "source_message_id": source_message_id,
            "source_asset_key": source_asset_key,
        }
    )


def _default_operation_for_scope(scope_level: str) -> str:
    if scope_level == "thread":
        return "compact"
    if scope_level == "topic":
        return "group"
    if scope_level == "asset":
        return "annotate"
    return "annotate"


def _make_id(prefix: str, *parts: Any) -> str:
    raw = "|".join(str(part) for part in parts if part is not None and str(part) != "")
    token = raw or prefix
    return f"{prefix}_{sha1(token.encode('utf-8')).hexdigest()[:12]}"


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _read_json(path: Path) -> dict[str, Any]:
    payload = orjson.loads(path.read_bytes())
    if not isinstance(payload, Mapping):
        raise ValueError(f"Expected JSON object at {path}")
    return dict(payload)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            payload = orjson.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            rows.append(dict(payload))
    return rows


def write_json(path: Path, payload: Any) -> Path:
    encoded = orjson.dumps(json_safe(payload), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    return atomic_write_bytes(path, encoded)


def write_jsonl(path: Path, rows: Sequence[Any]) -> Path:
    encoded = b"".join(orjson.dumps(json_safe(row)) + b"\n" for row in rows)
    return atomic_write_bytes(path, encoded)


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return json_safe(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [json_safe(item) for item in value]
    return repr(value)
