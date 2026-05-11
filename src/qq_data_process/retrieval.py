from __future__ import annotations

from typing import Any

from .qdrant_store import QdrantIndexReader
from .rag_models import RetrievalConfig, RetrievedMessageHit
from .sqlite_store import SqlitePreprocessStore


class HybridRetriever:
    def __init__(
        self,
        *,
        sqlite_store: SqlitePreprocessStore,
        qdrant_reader: QdrantIndexReader,
        embedding_provider: object,
        text_collection_name: str,
    ) -> None:
        self.sqlite_store = sqlite_store
        self.qdrant_reader = qdrant_reader
        self.embedding_provider = embedding_provider
        self.text_collection_name = text_collection_name

    def retrieve(self, config: RetrievalConfig) -> list[RetrievedMessageHit]:
        if config.projection_mode == "raw" and not config.danger_allow_raw_identity_output:
            raise RuntimeError(
                "Raw identity retrieval requires danger_allow_raw_identity_output=True."
            )

        keyword_rows = self.sqlite_store.search_messages_fts(
            query=config.query_text,
            limit=config.keyword_top_k,
            run_id=config.run_id,
            chat_id_raw=config.chat_id_raw,
            chat_alias_id=config.chat_alias_id,
            start_timestamp_ms=config.start_timestamp_ms,
            end_timestamp_ms=config.end_timestamp_ms,
        )

        vector_rows: list[dict[str, Any]] = []
        vector_available = self.qdrant_reader.has_collection(self.text_collection_name)
        if config.vector_top_k > 0 and config.query_text.strip() and vector_available:
            query_vector = self.embedding_provider.embed_queries([config.query_text])[0]
            vector_rows = self.qdrant_reader.search_messages(
                collection_name=self.text_collection_name,
                query_vector=query_vector,
                limit=config.vector_top_k,
                run_id=config.run_id,
                chat_id_raw=config.chat_id_raw,
                chat_alias_id=config.chat_alias_id,
                start_timestamp_ms=config.start_timestamp_ms,
                end_timestamp_ms=config.end_timestamp_ms,
            )

        fused: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(keyword_rows, start=1):
            item = fused.setdefault(
                row["message_uid"],
                {
                    "message_uid": row["message_uid"],
                    "rrf": 0.0,
                    "keyword_rank": None,
                    "vector_rank": None,
                    "keyword_score": None,
                    "vector_score": None,
                    "match_sources": set(),
                },
            )
            item["rrf"] += 1.0 / (config.rrf_k + rank)
            item["keyword_rank"] = rank
            item["keyword_score"] = float(row["keyword_score"])
            item["match_sources"].add("keyword")

        for rank, row in enumerate(vector_rows, start=1):
            item = fused.setdefault(
                row["message_uid"],
                {
                    "message_uid": row["message_uid"],
                    "rrf": 0.0,
                    "keyword_rank": None,
                    "vector_rank": None,
                    "keyword_score": None,
                    "vector_score": None,
                    "match_sources": set(),
                },
            )
            item["rrf"] += 1.0 / (config.rrf_k + rank)
            item["vector_rank"] = rank
            item["vector_score"] = float(row["vector_score"])
            item["match_sources"].add("vector")

        ordered_uids = [
            item["message_uid"]
            for item in sorted(
                fused.values(),
                key=lambda item: item["rrf"],
                reverse=True,
            )[: config.top_k]
        ]
        details = self.sqlite_store.load_messages_by_uids(ordered_uids)
        by_uid = {row["message_uid"]: row for row in details}

        hits: list[RetrievedMessageHit] = []
        for message_uid in ordered_uids:
            row = by_uid.get(message_uid)
            if row is None:
                continue
            fused_item = fused[message_uid]
            projection = self._project_row(row, config)
            hits.append(
                RetrievedMessageHit(
                    message_uid=message_uid,
                    run_id=row["run_id"],
                    chat_type=row["chat_type"],
                    chat_id=projection["chat_id"],
                    chat_name=projection["chat_name"],
                    sender_id=projection["sender_id"],
                    sender_name=projection["sender_name"],
                    timestamp_ms=row["timestamp_ms"],
                    timestamp_iso=row["timestamp_iso"],
                    content=row["content"],
                    text_content=row["text_content"],
                    asset_count=int(row["asset_count"] or 0),
                    fused_score=float(fused_item["rrf"]),
                    keyword_rank=fused_item["keyword_rank"],
                    vector_rank=fused_item["vector_rank"],
                    keyword_score=fused_item["keyword_score"],
                    vector_score=fused_item["vector_score"],
                    match_sources=sorted(fused_item["match_sources"]),
                )
            )
        return hits

    def _project_row(
        self, row: dict[str, Any], config: RetrievalConfig
    ) -> dict[str, Any]:
        if config.projection_mode == "raw":
            return {
                "chat_id": row["chat_id_raw"],
                "chat_name": row["chat_name_raw"],
                "sender_id": row["sender_id_raw"],
                "sender_name": row["sender_name_raw"],
            }
        return {
            "chat_id": row["chat_alias_id"],
            "chat_name": row["chat_alias_label"],
            "sender_id": row["sender_alias_id"],
            "sender_name": row["sender_alias_label"],
        }
