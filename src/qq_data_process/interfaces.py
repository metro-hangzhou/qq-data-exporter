from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import (
    ChunkBuildResult,
    ChunkPolicySpec,
    ImageEmbeddingInput,
    ImportedChatBundle,
    PreparedImageAsset,
    TextEmbeddingInput,
)


class InputAdapter(Protocol):
    source_type: str

    def load(self, source_path: Path) -> ImportedChatBundle:
        ...


class ChunkPolicy(Protocol):
    name: str
    version: str

    def build(
        self,
        *,
        run_id: str,
        chat_id: str,
        spec: ChunkPolicySpec,
        messages: list,
    ) -> ChunkBuildResult:
        ...


class EmbeddingProvider(Protocol):
    vector_size: int

    def embed_documents(self, inputs: list[TextEmbeddingInput]) -> list[list[float]]:
        ...

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        ...

    def embed_images(self, inputs: list[ImageEmbeddingInput]) -> list[list[float]]:
        ...


class ImageFeatureProvider(Protocol):
    def prepare_assets(self, assets: list) -> list[PreparedImageAsset]:
        ...
