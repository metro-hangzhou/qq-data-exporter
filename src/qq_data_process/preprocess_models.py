from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from qq_data_process.models import (
    AssetType,
    ChatType,
    CorpusLineage,
    CorpusProvenance,
    PROCESS_TIMEZONE,
    ResourceState,
)
from qq_data_process.preprocess_types import (
    DeliveryProfile,
    PreprocessOperationType,
    PreprocessScopeLevel,
    ProcessedViewKind,
)


class PreprocessPluginProvenance(BaseModel):
    plugin_id: str
    plugin_version: str
    plugin_kind: str = "preprocessor"
    build_profile: str = "default"
    created_at: datetime = Field(default_factory=lambda: datetime.now(PROCESS_TIMEZONE))
    extra: dict[str, Any] = Field(default_factory=dict)


class PreprocessDirective(BaseModel):
    directive_id: str | None = None
    title: str | None = None
    analysis_goal: str | None = None
    relevance_policy: str = "meme_focus"
    noise_handling_mode: str = "compact_cluster"
    preserve_evidence_window: int = 2
    target_topics: list[str] = Field(default_factory=list)
    suppress_topics: list[str] = Field(default_factory=list)
    target_participants: list[str] = Field(default_factory=list)
    suppress_participants: list[str] = Field(default_factory=list)
    suppress_message_patterns: list[str] = Field(default_factory=list)
    suppress_message_examples: list[str] = Field(default_factory=list)
    retain_modalities: list[str] = Field(default_factory=list)
    suppress_non_target_chatter: bool = False
    prefer_compaction_over_deletion: bool = True
    preserve_reply_context: bool = True
    preserve_media_neighbors: bool = True
    cluster_gap_seconds: int = 180
    max_compaction_span_messages: int = 24
    notes: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class PreprocessArtifact(BaseModel):
    artifact_id: str | None = None
    artifact_type: str
    path: str
    title: str | None = None
    note: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage | None = None
    provenance: PreprocessPluginProvenance | None = None


class PreprocessAnnotation(BaseModel):
    annotation_id: str | None = None
    operation_type: PreprocessOperationType
    scope_level: PreprocessScopeLevel
    label: str | None = None
    summary: str
    decision_summary: str | None = None
    confidence: float = 0.0
    source_message_ids: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    source_thread_ids: list[str] = Field(default_factory=list)
    target_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[PreprocessArtifact] = Field(default_factory=list)
    lineage: CorpusLineage | None = None
    provenance: PreprocessPluginProvenance | None = None


class ProcessedMessageView(BaseModel):
    processed_message_id: str | None = None
    view_id: str
    message_id: str | None = None
    chat_type: ChatType
    chat_id: str
    source_message_ids: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    operation_type: PreprocessOperationType
    delivery_profile: DeliveryProfile = "raw_plus_processed"
    raw_text: str | None = None
    processed_text: str | None = None
    summary: str | None = None
    decision_summary: str | None = None
    confidence: float = 0.0
    suppressed: bool = False
    labels: list[str] = Field(default_factory=list)
    annotations: list[PreprocessAnnotation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage | None = None
    provenance: PreprocessPluginProvenance | None = None


class ProcessedThreadView(BaseModel):
    processed_thread_id: str | None = None
    view_id: str
    thread_id: str | None = None
    chat_type: ChatType
    chat_id: str
    source_message_ids: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    operation_type: PreprocessOperationType
    delivery_profile: DeliveryProfile = "raw_plus_processed"
    title: str | None = None
    summary: str | None = None
    decision_summary: str | None = None
    confidence: float = 0.0
    labels: list[str] = Field(default_factory=list)
    annotations: list[PreprocessAnnotation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage | None = None
    provenance: PreprocessPluginProvenance | None = None


class ProcessedAssetView(BaseModel):
    processed_asset_id: str | None = None
    view_id: str
    asset_id: str | None = None
    message_id: str | None = None
    asset_type: AssetType
    resource_state: ResourceState
    file_name: str | None = None
    source_message_ids: list[str] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    operation_type: PreprocessOperationType
    delivery_profile: DeliveryProfile = "raw_plus_processed"
    caption: str | None = None
    summary: str | None = None
    decision_summary: str | None = None
    confidence: float = 0.0
    labels: list[str] = Field(default_factory=list)
    annotations: list[PreprocessAnnotation] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage | None = None
    provenance: PreprocessPluginProvenance | None = None


class PreprocessBuildReport(BaseModel):
    run_id: str
    view_id: str
    started_at: datetime = Field(default_factory=lambda: datetime.now(PROCESS_TIMEZONE))
    finished_at: datetime | None = None
    elapsed_s: float | None = None
    processed_message_count: int = 0
    processed_thread_count: int = 0
    processed_asset_count: int = 0
    annotation_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    lineage: CorpusLineage | None = None
    provenance: PreprocessPluginProvenance | None = None


class PreprocessViewManifest(BaseModel):
    schema_version: int = 1
    view_id: str
    corpus_id: str
    chat_type: ChatType
    chat_id: str
    chat_name: str | None = None
    view_kind: ProcessedViewKind = "processed_view"
    delivery_profile: DeliveryProfile = "raw_plus_processed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(PROCESS_TIMEZONE))
    processed_message_count: int = 0
    processed_thread_count: int = 0
    processed_asset_count: int = 0
    annotation_count: int = 0
    source_exports: list[str] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    directive: PreprocessDirective | None = None
    lineage: CorpusLineage
    provenance: CorpusProvenance = Field(default_factory=CorpusProvenance)
    build_report: PreprocessBuildReport | None = None


class PreprocessViewContext(BaseModel):
    context_id: str
    manifest: PreprocessViewManifest
    message_views: list[ProcessedMessageView] = Field(default_factory=list)
    thread_views: list[ProcessedThreadView] = Field(default_factory=list)
    asset_views: list[ProcessedAssetView] = Field(default_factory=list)
    annotations: list[PreprocessAnnotation] = Field(default_factory=list)
    artifacts: list[PreprocessArtifact] = Field(default_factory=list)
    indexes: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    directive: PreprocessDirective | None = None
    lineage: CorpusLineage | None = None
    provenance: CorpusProvenance = Field(default_factory=CorpusProvenance)
