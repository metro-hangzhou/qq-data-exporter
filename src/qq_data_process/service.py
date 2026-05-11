from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from .adapters import ExporterJsonlAdapter, QceJsonAdapter, TxtTranscriptAdapter
from .detect import resolve_source_path
from .chunking import (
    HybridChunkPolicy,
    NoChunkPolicy,
    TimeGapChunkPolicy,
    WindowChunkPolicy,
)
from .embeddings import build_embedding_provider
from .identities import IdentityProjector
from .image_features import ReferenceOnlyImageFeatureProvider
from .diagnostics import diagnose_export
from .models import (
    ChunkBuildResult,
    ImageEmbeddingInput,
    ImportedChatBundle,
    PreprocessJobConfig,
    PreprocessRunResult,
    TextEmbeddingInput,
)
from .qdrant_store import QdrantIndexWriter
from .runtime_control import maybe_cooperative_yield
from .sqlite_store import SqlitePreprocessStore
from .utils import stable_digest


class PreprocessService:
    def __init__(
        self,
        *,
        adapters: dict[str, object] | None = None,
        chunk_policies: dict[str, object] | None = None,
        embedding_provider: object | None = None,
        image_feature_provider: object | None = None,
    ) -> None:
        self._adapters = adapters or {
            "exporter_jsonl": ExporterJsonlAdapter(),
            "qce_json": QceJsonAdapter(),
            "qq_txt": TxtTranscriptAdapter(),
        }
        self._chunk_policies = chunk_policies or {
            "none": NoChunkPolicy(),
            "window": WindowChunkPolicy(),
            "timegap": TimeGapChunkPolicy(),
            "hybrid": HybridChunkPolicy(),
        }
        self._embedding_provider = embedding_provider
        self._image_feature_provider = (
            image_feature_provider or ReferenceOnlyImageFeatureProvider()
        )
        self._identity_projector = IdentityProjector()

    def run(
        self,
        config: PreprocessJobConfig,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> PreprocessRunResult:
        started_at = datetime.now()
        resolved_source_path = resolve_source_path(config.source_path)
        adapter = self._adapters[config.source_type]
        bundle: ImportedChatBundle = adapter.load(  # type: ignore[assignment]
            resolved_source_path,
            progress_callback=progress_callback,
        )
        bundle.messages.sort(key=lambda item: (item.timestamp_ms, item.message_uid))
        self._emit_progress(
            progress_callback,
            phase="load",
            current=len(bundle.messages),
            total=len(bundle.messages),
            message=f"Loaded {len(bundle.messages)} messages from source",
        )

        run_id = f"pre_{stable_digest(config.source_type, resolved_source_path, started_at.isoformat(), length=18)}"
        config.state_dir.mkdir(parents=True, exist_ok=True)

        sqlite_store = SqlitePreprocessStore(config.resolved_sqlite_path())
        sqlite_store.initialize()
        if not config.skip_vector_index:
            sqlite_store.assert_embedding_policy_compatible(config.embedding_policy)

        embedding_provider = None
        if not config.skip_vector_index:
            embedding_provider = self._embedding_provider or build_embedding_provider(
                config.embedding_policy
            )

        chunk_results: list[ChunkBuildResult] = []
        for spec in config.chunk_policy_specs:
            if not spec.enabled:
                continue
            policy = self._chunk_policies[spec.name]
            chunk_results.append(
                policy.build(  # type: ignore[union-attr]
                    run_id=run_id,
                    chat_id=bundle.chat_id,
                    spec=spec,
                    messages=bundle.messages,
                )
            )

        all_assets = [asset for message in bundle.messages for asset in message.assets]
        prepared_assets = {
            item.asset_id: item
            for item in self._image_feature_provider.prepare_assets(all_assets)  # type: ignore[union-attr]
        }

        qdrant_path = config.resolved_qdrant_path()
        qdrant_path.mkdir(parents=True, exist_ok=True)
        qdrant_writer = None
        if not config.skip_vector_index:
            assert embedding_provider is not None
            qdrant_writer = QdrantIndexWriter(
                qdrant_path,
                vector_size=embedding_provider.vector_size,  # type: ignore[attr-defined]
            )
            qdrant_writer.ensure_collections(config.embedding_policy)
            self._emit_progress(
                progress_callback,
                phase="embed_init",
                current=0,
                total=len(bundle.messages),
                message=(
                    f"Embedding provider ready "
                    f"(device={getattr(embedding_provider, '_device', 'unknown')}, "
                    f"batch_size={getattr(embedding_provider, '_batch_size', config.embedding_policy.batch_size)})"
                ),
            )
        else:
            self._emit_progress(
                progress_callback,
                phase="embed_skip",
                current=0,
                total=len(bundle.messages),
                message=(
                    "Vector indexing disabled for this preprocess run; "
                    "SQLite/FTS analysis remains available"
                ),
            )
        if config.skip_keyword_index:
            self._emit_progress(
                progress_callback,
                phase="fts_skip",
                current=0,
                total=0,
                message="Skipping SQLite keyword/FTS index for this preprocess run",
            )

        text_inputs: list[TextEmbeddingInput] = []
        for message_index, message in enumerate(bundle.messages, start=1):
            view = self._identity_projector.project(message)
            text_inputs.append(
                TextEmbeddingInput(
                    unit_id=message.message_uid,
                    text=message.text_content or message.content,
                    payload={
                        "run_id": run_id,
                        "message_uid": message.message_uid,
                        "unit_type": "message",
                        "chat_type": message.chat_type,
                        "chat_id_raw": message.chat_id,
                        "chat_alias_id": view.chat.alias_id,
                        "sender_alias_id": view.sender.alias_id,
                        "timestamp_ms": message.timestamp_ms,
                        "content": message.content,
                        "text_content": message.text_content,
                    },
                )
            )
            maybe_cooperative_yield(message_index)
        if not config.skip_vector_index:
            assert embedding_provider is not None
            assert qdrant_writer is not None
            text_chunk_size = int(
                getattr(
                    embedding_provider,
                    "_preferred_outer_chunk_size",
                    max(
                        64,
                        min(
                            512,
                            getattr(
                                embedding_provider,
                                "_batch_size",
                                config.embedding_policy.batch_size,
                            )
                            * 8,
                        ),
                    ),
                )
            )
            processed_text = 0
            total_text_batches = (
                len(text_inputs) + text_chunk_size - 1
            ) // text_chunk_size
            for batch_index, batch in enumerate(
                _batched(text_inputs, text_chunk_size), start=1
            ):
                self._emit_progress(
                    progress_callback,
                    phase="embed_text_dispatch",
                    current=processed_text,
                    total=len(text_inputs),
                    message=(
                        f"Submitting text batch {batch_index}/{total_text_batches} "
                        f"size={len(batch)}"
                    ),
                )
                text_vectors = embedding_provider.embed_documents(batch)  # type: ignore[union-attr]
                qdrant_writer.index_messages(
                    collection_name=config.embedding_policy.text_collection_name,
                    inputs=batch,
                    vectors=text_vectors,
                )
                processed_text += len(batch)
                self._emit_progress(
                    progress_callback,
                    phase="embed_text",
                    current=processed_text,
                    total=len(text_inputs),
                    message=f"Embedded text units {processed_text}/{len(text_inputs)}",
                )

        image_inputs = []
        if not config.skip_vector_index and not config.skip_image_embeddings:
            for asset in all_assets:
                if asset.asset_type != "image":
                    continue
                image_inputs.append(
                    ImageEmbeddingInput(
                        asset_id=asset.asset_id,
                        text_hint=(
                            f"{asset.asset_type}:{asset.file_name or ''}:{asset.path or ''}:{asset.md5 or ''}"
                        ),
                        payload={
                            "run_id": run_id,
                            "asset_id": asset.asset_id,
                            "asset_type": asset.asset_type,
                            "message_uid": asset.message_uid,
                            "file_name": asset.file_name,
                            "path": asset.path,
                            "md5": asset.md5,
                        },
                    )
                )
        else:
            self._emit_progress(
                progress_callback,
                phase="embed_image_skip",
                current=0,
                total=0,
                message="Skipping image embeddings for this preprocess run",
            )
        if image_inputs:
            assert embedding_provider is not None
            assert qdrant_writer is not None
            image_chunk_size = int(
                getattr(
                    embedding_provider,
                    "_preferred_outer_chunk_size",
                    max(
                        16,
                        min(
                            128,
                            getattr(
                                embedding_provider,
                                "_batch_size",
                                config.embedding_policy.batch_size,
                            )
                            * 4,
                        ),
                    ),
                )
            )
            processed_images = 0
            total_image_batches = (
                len(image_inputs) + image_chunk_size - 1
            ) // image_chunk_size
            for batch_index, batch in enumerate(
                _batched(image_inputs, image_chunk_size), start=1
            ):
                self._emit_progress(
                    progress_callback,
                    phase="embed_image_dispatch",
                    current=processed_images,
                    total=len(image_inputs),
                    message=(
                        f"Submitting image batch {batch_index}/{total_image_batches} "
                        f"size={len(batch)}"
                    ),
                )
                image_vectors = embedding_provider.embed_images(batch)  # type: ignore[union-attr]
                qdrant_writer.index_assets(
                    collection_name=config.embedding_policy.image_collection_name,
                    inputs=batch,
                    prepared_assets=prepared_assets,
                    vectors=image_vectors,
                )
                processed_images += len(batch)
                self._emit_progress(
                    progress_callback,
                    phase="embed_image",
                    current=processed_images,
                    total=len(image_inputs),
                    message=f"Embedded image assets {processed_images}/{len(image_inputs)}",
                )
        if qdrant_writer is not None:
            qdrant_writer.close()

        result = PreprocessRunResult(
            run_id=run_id,
            source_type=bundle.source_type,
            fidelity=bundle.fidelity,
            sqlite_path=config.resolved_sqlite_path(),
            qdrant_location=qdrant_path,
            message_count=len(bundle.messages),
            asset_count=len(all_assets),
            chunk_set_count=sum(
                1 for item in chunk_results if item.chunk_set is not None
            ),
            warnings=(
                (["vector_index_disabled"] if config.skip_vector_index else [])
                + (["keyword_index_disabled"] if config.skip_keyword_index else [])
            ),
            started_at=started_at,
            completed_at=datetime.now(),
        )
        self._emit_progress(
            progress_callback,
            phase="persist_prepare",
            current=0,
            total=result.message_count,
            message="Persisting messages/assets into SQLite analysis store",
        )
        sqlite_store.persist_run(
            result=result,
            bundle=bundle,
            config=config,
            messages=bundle.messages,
            chunks=chunk_results,
            identity_projector=self._identity_projector,
            progress_callback=progress_callback,
        )
        self._emit_progress(
            progress_callback,
            phase="diagnostics",
            current=0,
            total=0,
            message="Scanning export manifest/media coverage diagnostics",
        )
        diagnostic_payload = _export_media_coverage_payload(
            config.source_type,
            config.source_path,
            progress_callback=progress_callback,
        )
        if diagnostic_payload is not None:
            sqlite_store.persist_run_diagnostic(
                run_id=result.run_id,
                diagnostic_kind="export_media_coverage",
                payload=diagnostic_payload,
            )
        self._emit_progress(
            progress_callback,
            phase="complete",
            current=result.message_count,
            total=result.message_count,
            message=f"Completed preprocess run {result.run_id}",
        )
        return result

    def _emit_progress(
        self,
        callback: Callable[[dict[str, object]], None] | None,
        *,
        phase: str,
        current: int,
        total: int,
        message: str,
    ) -> None:
        if callback is None:
            return
        callback(
            {
                "phase": phase,
                "current": current,
                "total": total,
                "message": message,
            }
        )


def _batched(items: list, size: int):
    step = max(1, size)
    for index in range(0, len(items), step):
        yield items[index : index + step]


def _export_media_coverage_payload(
    source_type: str,
    source_path: Path,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object] | None:
    if source_type != "exporter_jsonl":
        return None
    manifest_path = source_path.with_suffix("").with_suffix(".manifest.json")
    if not manifest_path.exists():
        return None
    report = diagnose_export(
        source_path,
        manifest_path,
        progress_callback=progress_callback,
    )
    return {
        "total_image_references": report.image_stats.referenced,
        "total_file_references": report.file_stats.referenced,
        "total_sticker_references": report.sticker_static_stats.referenced,
        "total_video_references": report.video_stats.referenced,
        "total_speech_references": report.speech_stats.referenced,
        "missing_image_count": report.image_stats.missing,
        "missing_file_count": report.file_stats.missing,
        "missing_sticker_count": report.sticker_static_stats.missing,
        "missing_video_count": report.video_stats.missing,
        "missing_speech_count": report.speech_stats.missing,
        "image_missing_ratio": report.image_stats.missing_ratio,
        "file_missing_ratio": report.file_stats.missing_ratio,
        "sticker_missing_ratio": report.sticker_static_stats.missing_ratio,
        "video_missing_ratio": report.video_stats.missing_ratio,
        "speech_missing_ratio": report.speech_stats.missing_ratio,
        "overall_media_missing_ratio": (
            report.manifest_missing / report.manifest_total_assets
            if report.manifest_total_assets
            else 0.0
        ),
        "media_availability_flags": {
            "has_image": report.image_stats.referenced > 0,
            "has_file": report.file_stats.referenced > 0,
            "has_sticker": report.sticker_static_stats.referenced > 0,
            "has_video": report.video_stats.referenced > 0,
            "has_speech": report.speech_stats.referenced > 0,
            "has_missing_media": report.manifest_missing > 0,
        },
    }
