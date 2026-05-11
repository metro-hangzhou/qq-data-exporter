from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

import qq_data_process.models as _models

if not hasattr(_models, "PROCESS_TIMEZONE"):
    _models.PROCESS_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
if not hasattr(_models, "ResourceState"):
    _models.ResourceState = Literal[
        "available",
        "missing",
        "expired",
        "placeholder",
        "unsupported",
    ]
if not hasattr(_models, "CorpusLineage"):
    class _CompatCorpusLineage(BaseModel):
        model_config = ConfigDict(extra="allow")

        source_export_id: str | None = None
        source_message_id: str | None = None
        source_asset_key: str | None = None
        source_chat_id: str | None = None
        extra: dict[str, Any] = Field(default_factory=dict)

    _models.CorpusLineage = _CompatCorpusLineage
if not hasattr(_models, "CorpusProvenance"):
    class _CompatCorpusProvenance(BaseModel):
        model_config = ConfigDict(extra="allow")

        build_profile: str = "preprocess"
        created_at: datetime = Field(
            default_factory=lambda: datetime.now(_models.PROCESS_TIMEZONE)
        )
        source_type: str | None = None
        source_path: str | None = None
        extra: dict[str, Any] = Field(default_factory=dict)

    _models.CorpusProvenance = _CompatCorpusProvenance

from .models import (
    AssetType,
    CanonicalAssetRecord,
    CanonicalMessageRecord,
    ChatType,
    ChunkBuildResult,
    ChunkMembershipRecord,
    ChunkPolicySpec,
    ChunkRecord,
    ChunkSetRecord,
    EmbeddingPolicy,
    IdentityMode,
    IdentityProjection,
    IdentityProjectionPolicy,
    ImageEmbeddingInput,
    ImportedChatBundle,
    ImportSource,
    InputFidelity,
    LocalCorpusDescriptor,
    PreparedImageAsset,
    PreprocessJobConfig,
    PreprocessRunResult,
    TextEmbeddingInput,
)
from .preprocess_models import (
    PreprocessAnnotation,
    PreprocessArtifact,
    PreprocessBuildReport,
    PreprocessDirective,
    PreprocessPluginProvenance,
    PreprocessViewContext,
    PreprocessViewManifest,
    ProcessedAssetView,
    ProcessedMessageView,
    ProcessedThreadView,
)
from .preprocess_service import (
    PreprocessRunResult as PreprocessViewRunResult,
    build_preprocess_view,
    load_preprocess_view,
    run_preprocess,
)
from .preprocess_context import (
    ImportedBundleAsset,
    ImportedBundleManifest,
    ImportedBundleMessage,
    ImportedBundlePreprocessContext,
    ImportedBundleSegment,
    ImportedBundleThread,
    build_preprocess_context_from_bundle,
    build_preprocess_context_from_exporter_jsonl,
)
from .preprocess_types import (
    DeliveryProfile,
    PreprocessOperationType,
    PreprocessScopeLevel,
    ProcessedViewKind,
)
from .embeddings import DeterministicEmbeddingProvider
from .detect import detect_source_type, load_local_corpus_descriptor, resolve_source_path
from .local_corpora import (
    build_filtered_corpora_index_payload,
    LocalCorpusIndexEntry,
    LocalCorpusResolution,
    load_local_corpora_index,
    resolve_local_corpus,
    resolve_local_corpora,
)
from .rag import RagService
from .service import PreprocessService

PROCESS_TIMEZONE = _models.PROCESS_TIMEZONE
ResourceState = _models.ResourceState
CorpusLineage = _models.CorpusLineage
CorpusProvenance = _models.CorpusProvenance

__all__ = [
    "PROCESS_TIMEZONE",
    "ImportSource",
    "InputFidelity",
    "ChatType",
    "AssetType",
    "IdentityMode",
    "ResourceState",
    "CorpusLineage",
    "CorpusProvenance",
    "CanonicalAssetRecord",
    "CanonicalMessageRecord",
    "ImportedChatBundle",
    "LocalCorpusDescriptor",
    "ChunkPolicySpec",
    "ChunkSetRecord",
    "ChunkRecord",
    "ChunkMembershipRecord",
    "ChunkBuildResult",
    "EmbeddingPolicy",
    "IdentityProjectionPolicy",
    "PreprocessJobConfig",
    "PreprocessRunResult",
    "TextEmbeddingInput",
    "ImageEmbeddingInput",
    "PreparedImageAsset",
    "IdentityProjection",
    "PreprocessOperationType",
    "DeliveryProfile",
    "PreprocessScopeLevel",
    "ProcessedViewKind",
    "PreprocessPluginProvenance",
    "PreprocessDirective",
    "PreprocessArtifact",
    "PreprocessAnnotation",
    "ProcessedMessageView",
    "ProcessedThreadView",
    "ProcessedAssetView",
    "PreprocessBuildReport",
    "PreprocessViewManifest",
    "PreprocessViewContext",
    "PreprocessViewRunResult",
    "build_preprocess_view",
    "run_preprocess",
    "load_preprocess_view",
    "ImportedBundleSegment",
    "ImportedBundleMessage",
    "ImportedBundleAsset",
    "ImportedBundleThread",
    "ImportedBundleManifest",
    "ImportedBundlePreprocessContext",
    "build_preprocess_context_from_bundle",
    "build_preprocess_context_from_exporter_jsonl",
    "detect_source_type",
    "resolve_source_path",
    "load_local_corpus_descriptor",
    "LocalCorpusIndexEntry",
    "LocalCorpusResolution",
    "load_local_corpora_index",
    "resolve_local_corpus",
    "resolve_local_corpora",
    "build_filtered_corpora_index_payload",
    "RagService",
    "PreprocessService",
    "DeterministicEmbeddingProvider",
]
