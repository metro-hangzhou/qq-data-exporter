from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

ImportSource = Literal["exporter_jsonl", "qce_json", "qq_txt"]
InputFidelity = Literal["high", "compat", "lossy"]
ChatType = Literal["group", "private"]
AssetType = Literal["image", "file", "video", "unknown"]
IdentityMode = Literal["alias", "raw"]
EmbeddingProviderName = Literal["deterministic", "jina_v4", "openrouter", "qwen3_vl"]
PROCESS_TIMEZONE = datetime.now().astimezone().tzinfo or timezone.utc
ResourceState = Literal[
    "available",
    "missing",
    "expired",
    "placeholder",
    "unsupported",
]


class CorpusLineage(BaseModel):
    source_export_id: str | None = None
    source_message_id: str | None = None
    source_asset_key: str | None = None
    source_chat_id: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CorpusProvenance(BaseModel):
    build_profile: str = "preprocess"
    created_at: datetime = Field(default_factory=lambda: datetime.now(PROCESS_TIMEZONE))
    source_type: str | None = None
    source_path: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CanonicalAssetRecord(BaseModel):
    asset_id: str
    message_uid: str
    asset_type: AssetType
    file_name: str | None = None
    path: str | None = None
    md5: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    future_multimodal_parse: bool = True


class CanonicalMessageRecord(BaseModel):
    message_uid: str
    import_source: ImportSource
    fidelity: InputFidelity
    chat_type: ChatType
    chat_id: str
    chat_name: str | None = None
    sender_id_raw: str
    sender_name_raw: str | None = None
    message_id: str | None = None
    message_seq: str | None = None
    timestamp_ms: int
    timestamp_iso: str
    content: str
    text_content: str = ""
    assets: list[CanonicalAssetRecord] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class ImportedChatBundle(BaseModel):
    source_type: ImportSource
    fidelity: InputFidelity
    source_path: Path
    chat_type: ChatType
    chat_id: str
    chat_name: str | None = None
    messages: list[CanonicalMessageRecord] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LocalCorpusDescriptor(BaseModel):
    corpus_id: str
    corpus_dir: Path
    export_jsonl_path: Path
    manifest_path: Path
    assets_dir: Path
    dataset_meta: dict[str, Any] = Field(default_factory=dict)


class ChunkPolicySpec(BaseModel):
    name: str
    version: str = "v1"
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class ChunkSetRecord(BaseModel):
    chunk_set_id: str
    run_id: str
    chat_id: str
    policy_name: str
    policy_version: str
    policy_params: dict[str, Any] = Field(default_factory=dict)
    chunk_kind: str


class ChunkRecord(BaseModel):
    chunk_id: str
    chunk_set_id: str
    chat_id: str
    chunk_kind: str
    ordinal: int
    start_message_uid: str
    end_message_uid: str
    start_timestamp_ms: int
    end_timestamp_ms: int
    message_count: int
    content_preview: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class ChunkMembershipRecord(BaseModel):
    chunk_id: str
    message_uid: str
    ordinal: int


class ChunkBuildResult(BaseModel):
    chunk_set: ChunkSetRecord | None = None
    chunks: list[ChunkRecord] = Field(default_factory=list)
    memberships: list[ChunkMembershipRecord] = Field(default_factory=list)


class EmbeddingPolicy(BaseModel):
    provider_name: EmbeddingProviderName = "qwen3_vl"
    model_name: str = "Qwen/Qwen3-VL-Embedding-2B"
    api_base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    text_collection_name: str = "text_units"
    image_collection_name: str = "image_assets"
    document_task: str = "retrieval"
    query_task: str = "retrieval"
    image_fallback_task: str = "retrieval"
    document_prompt_name: str = "passage"
    query_prompt_name: str = "query"
    image_fallback_prompt_name: str = "passage"
    device: str | None = None
    cache_dir: Path | None = Path("state/models/huggingface")
    batch_size: int = 8
    torch_dtype: str = "auto"
    quantization: str = "auto"
    attn_implementation: str | None = "sdpa"
    min_cuda_vram_gb: float = 6.0
    allow_cpu_fallback: bool = True
    vector_size_hint: int | None = None
    normalize: bool = True
    request_timeout_seconds: float = 120.0
    extra: dict[str, Any] = Field(default_factory=dict)

    def compatibility_signature(self) -> dict[str, Any]:
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "api_base_url": self.api_base_url,
            "text_collection_name": self.text_collection_name,
            "image_collection_name": self.image_collection_name,
            "attn_implementation": self.attn_implementation,
            "document_task": self.document_task,
            "query_task": self.query_task,
            "image_fallback_task": self.image_fallback_task,
            "document_prompt_name": self.document_prompt_name,
            "query_prompt_name": self.query_prompt_name,
            "image_fallback_prompt_name": self.image_fallback_prompt_name,
            "normalize": self.normalize,
            "vector_size_hint": self.vector_size_hint,
        }


class IdentityProjectionPolicy(BaseModel):
    default_mode: IdentityMode = "alias"
    danger_allow_raw_identity_output: bool = False


class PreprocessJobConfig(BaseModel):
    source_type: ImportSource
    source_path: Path
    state_dir: Path = Path("state/preprocess")
    sqlite_path: Path | None = None
    qdrant_path: Path | None = None
    chunk_policy_specs: list[ChunkPolicySpec] = Field(default_factory=list)
    embedding_policy: EmbeddingPolicy = Field(default_factory=EmbeddingPolicy)
    skip_vector_index: bool = False
    skip_keyword_index: bool = False
    skip_image_embeddings: bool = True
    identity_policy: IdentityProjectionPolicy = Field(
        default_factory=IdentityProjectionPolicy
    )
    run_label: str | None = None

    def resolved_sqlite_path(self) -> Path:
        if self.sqlite_path is not None:
            return self.sqlite_path
        return self.state_dir / "db" / "analysis.db"

    def resolved_qdrant_path(self) -> Path:
        if self.qdrant_path is not None:
            return self.qdrant_path
        return self.state_dir / "qdrant"


class PreprocessRunResult(BaseModel):
    run_id: str
    source_type: ImportSource
    fidelity: InputFidelity
    sqlite_path: Path
    qdrant_location: Path
    message_count: int
    asset_count: int
    chunk_set_count: int
    warnings: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime


class TextEmbeddingInput(BaseModel):
    unit_id: str
    text: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ImageEmbeddingInput(BaseModel):
    asset_id: str
    text_hint: str
    payload: dict[str, Any] = Field(default_factory=dict)


class PreparedImageAsset(BaseModel):
    asset_id: str
    future_multimodal_parse: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)


class IdentityProjection(BaseModel):
    entity_type: Literal["chat", "sender"]
    raw_id: str
    alias_id: str
    alias_label: str
