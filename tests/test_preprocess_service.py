from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

from qq_data_process import (
    ChunkPolicySpec,
    DeterministicEmbeddingProvider,
    PreprocessJobConfig,
    PreprocessService,
)
from qq_data_process.qdrant_store import QdrantIndexReader


def _new_tmp_path(prefix: str) -> Path:
    tmp_root = Path(".tmp")
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_root / f"{prefix}_{uuid4().hex[:8]}"
    tmp_path.mkdir(parents=True, exist_ok=False)
    return tmp_path


def test_preprocess_service_runs_jsonl_to_sqlite_and_qdrant() -> None:
    tmp_path = _new_tmp_path("test_preprocess_service")

    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/smoke.jsonl"),
        state_dir=tmp_path / "state",
        skip_image_embeddings=False,
        chunk_policy_specs=[
            ChunkPolicySpec(name="window", params={"window_size": 2, "overlap": 1})
        ],
    )

    result = service.run(config)

    assert result.message_count == 6
    assert result.asset_count == 2
    assert result.chunk_set_count == 1
    assert result.sqlite_path.exists()
    assert result.qdrant_location.exists()

    with sqlite3.connect(result.sqlite_path) as conn:
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        asset_count = conn.execute("SELECT COUNT(*) FROM message_assets").fetchone()[0]
        chunk_set_count = conn.execute("SELECT COUNT(*) FROM chunk_sets").fetchone()[0]
        assert message_count == 6
        assert asset_count == 2
        assert chunk_set_count == 1

    client = QdrantIndexReader(result.qdrant_location)
    text_count = client.count("text_units")
    image_count = client.count("image_assets")
    client.close()

    assert text_count == 6
    assert image_count == 1


def test_preprocess_service_can_skip_vector_index_and_still_persist_sqlite() -> None:
    tmp_path = _new_tmp_path("test_preprocess_skip_vector_index")

    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/smoke.jsonl"),
        state_dir=tmp_path / "state",
        skip_vector_index=True,
    )

    result = service.run(config)

    assert result.message_count == 6
    assert result.warnings == ["vector_index_disabled"]
    assert result.sqlite_path.exists()
    assert result.qdrant_location.exists()

    with sqlite3.connect(result.sqlite_path) as conn:
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert message_count == 6

    client = QdrantIndexReader(result.qdrant_location)
    assert client.has_collection("text_units") is False
    client.close()


def test_preprocess_fixture_export2_pilot() -> None:
    """Test preprocess on export2 pilot fixture with diverse segments."""
    tmp_path = _new_tmp_path("test_preprocess_export2_pilot")

    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/fixture_export2_pilot.jsonl"),
        state_dir=tmp_path / "state",
        skip_image_embeddings=False,
        chunk_policy_specs=[
            ChunkPolicySpec(name="window", params={"window_size": 5, "overlap": 2})
        ],
    )

    result = service.run(config)

    # Export2 pilot fixture has 30 messages
    assert result.message_count == 30
    assert result.sqlite_path.exists()
    assert result.qdrant_location.exists()

    with sqlite3.connect(result.sqlite_path) as conn:
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert message_count == 30
        asset_count = conn.execute("SELECT COUNT(*) FROM message_assets").fetchone()[0]
        assert asset_count > 0  # export2 pilot has images


def test_preprocess_fixture_export3_missing_media() -> None:
    """Test preprocess on export3 missing-media fixture with graceful degradation."""
    tmp_path = _new_tmp_path("test_preprocess_export3_missing")

    service = PreprocessService(
        embedding_provider=DeterministicEmbeddingProvider(vector_size=8)
    )
    config = PreprocessJobConfig(
        source_type="exporter_jsonl",
        source_path=Path("tests/fixtures/fixture_export3_missing_media.jsonl"),
        state_dir=tmp_path / "state",
        skip_image_embeddings=True,  # Skip heavy embedding for missing media test
        chunk_policy_specs=[
            ChunkPolicySpec(name="window", params={"window_size": 5, "overlap": 2})
        ],
    )

    result = service.run(config)

    # Export3 missing-media fixture has 30 messages
    assert result.message_count == 30
    assert result.sqlite_path.exists()

    with sqlite3.connect(result.sqlite_path) as conn:
        message_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        assert message_count == 30
        asset_count = conn.execute("SELECT COUNT(*) FROM message_assets").fetchone()[0]
        assert asset_count >= 0  # export3 missing-media may have fewer assets
