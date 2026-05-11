from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .models import IdentityMode


class RetrievalConfig(BaseModel):
    query_text: str
    run_id: str | None = None
    chat_id_raw: str | None = None
    chat_alias_id: str | None = None
    start_timestamp_ms: int | None = None
    end_timestamp_ms: int | None = None
    keyword_top_k: int = 8
    vector_top_k: int = 8
    top_k: int = 8
    rrf_k: int = 60
    projection_mode: IdentityMode = "alias"
    prefer_chunk_context: bool = True
    context_window_before: int = 2
    context_window_after: int = 2
    max_context_blocks: int = 6
    max_messages_per_block: int = 24
    danger_allow_raw_identity_output: bool = False


class RetrievedMessageHit(BaseModel):
    message_uid: str
    run_id: str
    chat_type: str
    chat_id: str
    chat_name: str | None = None
    sender_id: str
    sender_name: str | None = None
    timestamp_ms: int
    timestamp_iso: str
    content: str
    text_content: str
    asset_count: int = 0
    fused_score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None
    keyword_score: float | None = None
    vector_score: float | None = None
    match_sources: list[Literal["keyword", "vector"]] = Field(default_factory=list)


class ContextMessage(BaseModel):
    message_uid: str
    timestamp_iso: str
    chat_id: str
    chat_name: str | None = None
    sender_id: str
    sender_name: str | None = None
    content: str


class ContextBlock(BaseModel):
    block_id: str
    source_kind: Literal["chunk", "window"]
    anchor_message_uid: str
    messages: list[ContextMessage] = Field(default_factory=list)
    rendered_text: str = ""


class RetrievalResult(BaseModel):
    config: RetrievalConfig
    sqlite_path: Path
    qdrant_path: Path
    hits: list[RetrievedMessageHit] = Field(default_factory=list)
    context_blocks: list[ContextBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DeepSeekConfig(BaseModel):
    model: str = "deepseek-reasoner"
    base_url: str = "https://api.deepseek.com"
    api_key_env: str = "DEEPSEEK_API_KEY"
    system_prompt: str = (
        "You analyze exported QQ chat records. Ground every claim in retrieved evidence."
    )
    temperature: float = 0.2


class RagAnswer(BaseModel):
    query_text: str
    retrieval: RetrievalResult
    model: str
    answer_text: str
    raw_response: dict = Field(default_factory=dict)
