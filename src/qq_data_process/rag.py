from __future__ import annotations

from pathlib import Path

from .context import ContextBuilder
from .embeddings import build_embedding_provider
from .generation import DeepSeekGenerator
from .models import EmbeddingPolicy
from .qdrant_store import QdrantIndexReader
from .rag_models import RagAnswer, RetrievalConfig, RetrievalResult
from .retrieval import HybridRetriever
from .sqlite_store import SqlitePreprocessStore


class _DisabledEmbeddingProvider:
    def embed_queries(self, _texts: list[str]) -> list[list[float]]:
        raise RuntimeError("Vector retrieval is disabled for this state.")


class RagService:
    def __init__(
        self,
        *,
        sqlite_store: SqlitePreprocessStore,
        qdrant_reader: QdrantIndexReader,
        embedding_policy: EmbeddingPolicy,
        embedding_provider: object,
    ) -> None:
        self.sqlite_store = sqlite_store
        self.qdrant_reader = qdrant_reader
        self.embedding_policy = embedding_policy
        self.embedding_provider = embedding_provider
        self.retriever = HybridRetriever(
            sqlite_store=sqlite_store,
            qdrant_reader=qdrant_reader,
            embedding_provider=embedding_provider,
            text_collection_name=embedding_policy.text_collection_name,
        )
        self.context_builder = ContextBuilder(sqlite_store=sqlite_store)

    @classmethod
    def from_state(
        cls,
        *,
        sqlite_path: Path,
        qdrant_path: Path,
        run_id: str | None = None,
        embedding_policy: EmbeddingPolicy | None = None,
        embedding_provider: object | None = None,
    ) -> "RagService":
        sqlite_store = SqlitePreprocessStore(sqlite_path)
        sqlite_store.initialize()
        resolved_policy = embedding_policy or sqlite_store.load_embedding_policy(run_id)
        if resolved_policy is None:
            raise RuntimeError(
                "No embedding policy metadata was found in the SQLite state. "
                "Run preprocessing before retrieval."
            )
        qdrant_reader = QdrantIndexReader(qdrant_path)
        if embedding_provider is not None:
            provider = embedding_provider
        elif qdrant_reader.has_collection(resolved_policy.text_collection_name):
            provider = build_embedding_provider(resolved_policy)
        else:
            provider = _DisabledEmbeddingProvider()
        return cls(
            sqlite_store=sqlite_store,
            qdrant_reader=qdrant_reader,
            embedding_policy=resolved_policy,
            embedding_provider=provider,
        )

    def retrieve(self, config: RetrievalConfig) -> RetrievalResult:
        resolved_config = self._resolve_run_scope(config)
        hits = self.retriever.retrieve(resolved_config)
        contexts = self.context_builder.build(hits=hits, config=resolved_config)
        return RetrievalResult(
            config=resolved_config,
            sqlite_path=self.sqlite_store.path,
            qdrant_path=self.qdrant_reader.path,
            hits=hits,
            context_blocks=contexts,
            warnings=[],
        )

    def answer(
        self,
        *,
        config: RetrievalConfig,
        generator: DeepSeekGenerator | None = None,
    ) -> RagAnswer:
        retrieval = self.retrieve(config)
        return (generator or DeepSeekGenerator()).generate(
            query_text=config.query_text,
            retrieval=retrieval,
        )

    def close(self) -> None:
        self.qdrant_reader.close()

    def _resolve_run_scope(self, config: RetrievalConfig) -> RetrievalConfig:
        if config.run_id is not None:
            return config
        latest = self.sqlite_store.load_run_record()
        if latest is None:
            return config
        return config.model_copy(update={"run_id": latest["run_id"]})
