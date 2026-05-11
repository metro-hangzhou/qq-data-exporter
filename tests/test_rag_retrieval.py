from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from qq_data_process import (
    ChunkPolicySpec,
    DeterministicEmbeddingProvider,
    EmbeddingPolicy,
    PreprocessJobConfig,
    PreprocessService,
    RagService,
)
from qq_data_process.rag_models import RetrievalConfig


def test_rag_service_retrieves_hits_and_context_from_preprocessed_state() -> None:
    tmp_path = Path(".tmp") / "test_rag_service"
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    policy = EmbeddingPolicy(provider_name="deterministic", vector_size_hint=8)
    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/smoke.jsonl"),
        state_dir=tmp_path / "state",
        embedding_policy=policy,
        chunk_policy_specs=[
            ChunkPolicySpec(name="window", params={"window_size": 2, "overlap": 1})
        ],
    )
    run_result = service.run(config)

    rag = RagService.from_state(
        sqlite_path=run_result.sqlite_path,
        qdrant_path=run_result.qdrant_location,
        run_id=run_result.run_id,
    )
    try:
        retrieval = rag.retrieve(
            RetrievalConfig(
                query_text="收到",
                run_id=run_result.run_id,
                keyword_top_k=3,
                vector_top_k=3,
                top_k=3,
            )
        )
    finally:
        rag.close()

    assert retrieval.hits
    assert retrieval.hits[0].content.endswith("收到")
    assert retrieval.context_blocks
    assert any("收到" in block.rendered_text for block in retrieval.context_blocks)


def test_rag_service_blocks_raw_projection_without_danger_flag() -> None:
    tmp_path = Path(".tmp") / "test_rag_raw_projection"
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    policy = EmbeddingPolicy(provider_name="deterministic", vector_size_hint=8)
    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/smoke.jsonl"),
        state_dir=tmp_path / "state",
        embedding_policy=policy,
    )
    run_result = service.run(config)

    rag = RagService.from_state(
        sqlite_path=run_result.sqlite_path,
        qdrant_path=run_result.qdrant_location,
        run_id=run_result.run_id,
    )
    try:
        with pytest.raises(RuntimeError):
            rag.retrieve(
                RetrievalConfig(
                    query_text="Hi",
                    run_id=run_result.run_id,
                    projection_mode="raw",
                )
            )
    finally:
        rag.close()


def test_rag_service_falls_back_to_keyword_only_without_vector_index() -> None:
    tmp_path = Path(".tmp") / "test_rag_keyword_only_without_vectors"
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/smoke.jsonl"),
        state_dir=tmp_path / "state",
        embedding_policy=EmbeddingPolicy(provider_name="deterministic", vector_size_hint=8),
        skip_vector_index=True,
    )
    run_result = service.run(config)

    rag = RagService.from_state(
        sqlite_path=run_result.sqlite_path,
        qdrant_path=run_result.qdrant_location,
        run_id=run_result.run_id,
    )
    try:
        retrieval = rag.retrieve(
            RetrievalConfig(
                query_text="收到",
                run_id=run_result.run_id,
                keyword_top_k=3,
                vector_top_k=3,
                top_k=3,
            )
        )
    finally:
        rag.close()

    assert retrieval.hits
    assert retrieval.hits[0].content.endswith("收到")
    assert all("vector" not in hit.match_sources for hit in retrieval.hits)
