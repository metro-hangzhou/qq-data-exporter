from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .models import (
    EmbeddingPolicy,
    ImageEmbeddingInput,
    PreparedImageAsset,
    TextEmbeddingInput,
)

_FORMAT_VERSION = "flat-memmap-v1"
_VECTOR_DTYPE = np.float16
_PRELOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024


def _collection_dir(base: Path, name: str) -> Path:
    safe = name.replace("/", "_")
    return base / safe


def _manifest_path(base: Path, name: str) -> Path:
    return _collection_dir(base, name) / "manifest.json"


def _vectors_path(base: Path, name: str) -> Path:
    return _collection_dir(base, name) / "vectors.f16"


def _meta_path(base: Path, name: str) -> Path:
    return _collection_dir(base, name) / "meta.jsonl"


def _normalize_rows(vectors: np.ndarray) -> np.ndarray:
    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms == 0.0, 1.0, norms)
    return arr / norms


@dataclass(slots=True)
class _CollectionData:
    manifest: dict[str, Any]
    metadata: list[dict[str, Any]]
    message_uids: list[str]
    run_ids: np.ndarray
    chat_id_raws: np.ndarray
    chat_alias_ids: np.ndarray
    timestamp_ms: np.ndarray
    vectors: np.ndarray


class QdrantIndexWriter:
    def __init__(self, path: Path, vector_size: int) -> None:
        self.path = path
        self.vector_size = vector_size
        self.path.mkdir(parents=True, exist_ok=True)

    def ensure_collections(self, policy: EmbeddingPolicy) -> None:
        self._ensure_collection(policy.text_collection_name)
        self._ensure_collection(policy.image_collection_name)

    def _ensure_collection(self, name: str) -> None:
        collection_dir = _collection_dir(self.path, name)
        collection_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = _manifest_path(self.path, name)
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_size = int(manifest["vector_size"])
            if existing_size != self.vector_size:
                raise ValueError(
                    f"Vector size mismatch for collection {name}: "
                    f"{existing_size} != {self.vector_size}"
                )
            return
        manifest = {
            "format": _FORMAT_VERSION,
            "vector_size": self.vector_size,
            "dtype": "float16",
            "count": 0,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        _vectors_path(self.path, name).touch()
        _meta_path(self.path, name).touch()

    def index_messages(
        self,
        *,
        collection_name: str,
        inputs: list[TextEmbeddingInput],
        vectors: list[list[float]],
    ) -> None:
        records = []
        for item in inputs:
            payload = dict(item.payload)
            payload.setdefault("message_uid", item.unit_id)
            payload.setdefault("run_id", "")
            payload.setdefault("chat_id_raw", "")
            payload.setdefault("chat_alias_id", "")
            payload.setdefault("timestamp_ms", 0)
            records.append(payload)
        self._append(collection_name=collection_name, records=records, vectors=vectors)

    def index_assets(
        self,
        *,
        collection_name: str,
        inputs: list[ImageEmbeddingInput],
        prepared_assets: dict[str, PreparedImageAsset],
        vectors: list[list[float]],
    ) -> None:
        records = []
        for item in inputs:
            prepared = prepared_assets[item.asset_id]
            payload = dict(item.payload)
            payload.update(prepared.payload)
            payload["future_multimodal_parse"] = prepared.future_multimodal_parse
            payload.setdefault("message_uid", item.payload.get("message_uid", ""))
            payload.setdefault("run_id", item.payload.get("run_id", ""))
            payload.setdefault("chat_id_raw", item.payload.get("chat_id_raw", ""))
            payload.setdefault("chat_alias_id", item.payload.get("chat_alias_id", ""))
            payload.setdefault("timestamp_ms", item.payload.get("timestamp_ms", 0))
            records.append(payload)
        self._append(collection_name=collection_name, records=records, vectors=vectors)

    def _append(
        self,
        *,
        collection_name: str,
        records: list[dict[str, Any]],
        vectors: list[list[float]],
    ) -> None:
        if not records:
            return
        manifest_path = _manifest_path(self.path, collection_name)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[1] != self.vector_size:
            raise ValueError(
                f"Vector batch shape mismatch for {collection_name}: {arr.shape}"
            )
        arr = _normalize_rows(arr).astype(_VECTOR_DTYPE, copy=False)
        with _vectors_path(self.path, collection_name).open("ab") as handle:
            handle.write(arr.tobytes(order="C"))
        with _meta_path(self.path, collection_name).open("a", encoding="utf-8") as handle:
            for payload in records:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        manifest["count"] = int(manifest.get("count", 0)) + int(arr.shape[0])
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )

    def close(self) -> None:
        return None


class QdrantIndexReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[str, _CollectionData] = {}

    def has_collection(self, name: str) -> bool:
        manifest_path = _manifest_path(self.path, name)
        if not manifest_path.exists():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return int(manifest.get("count", 0)) > 0

    def count(self, name: str) -> int:
        manifest_path = _manifest_path(self.path, name)
        if not manifest_path.exists():
            return 0
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return int(manifest.get("count", 0))

    def search_messages(
        self,
        *,
        collection_name: str,
        query_vector: list[float],
        limit: int,
        run_id: str | None = None,
        chat_id_raw: str | None = None,
        chat_alias_id: str | None = None,
        start_timestamp_ms: int | None = None,
        end_timestamp_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self.has_collection(collection_name):
            return []

        collection = self._load_collection(collection_name)
        mask = np.ones(len(collection.message_uids), dtype=bool)
        if run_id is not None:
            mask &= collection.run_ids == run_id
        if chat_id_raw is not None:
            mask &= collection.chat_id_raws == chat_id_raw
        if chat_alias_id is not None:
            mask &= collection.chat_alias_ids == chat_alias_id
        if start_timestamp_ms is not None:
            mask &= collection.timestamp_ms >= start_timestamp_ms
        if end_timestamp_ms is not None:
            mask &= collection.timestamp_ms <= end_timestamp_ms

        indices = np.flatnonzero(mask)
        if indices.size == 0:
            return []

        query = np.asarray(query_vector, dtype=np.float32)
        norm = float(np.linalg.norm(query))
        if norm == 0.0:
            return []
        query = query / norm
        vectors = collection.vectors[indices].astype(np.float32, copy=False)
        scores = vectors @ query
        top_k = min(limit, int(indices.size))
        if top_k <= 0:
            return []
        if top_k == indices.size:
            order = np.argsort(-scores)
        else:
            candidate = np.argpartition(scores, -top_k)[-top_k:]
            order = candidate[np.argsort(-scores[candidate])]

        results: list[dict[str, Any]] = []
        for pos in order:
            row_index = int(indices[int(pos)])
            payload = dict(collection.metadata[row_index])
            results.append(
                {
                    "message_uid": collection.message_uids[row_index],
                    "vector_score": float(scores[int(pos)]),
                    "payload": payload,
                }
            )
        return results

    def close(self) -> None:
        self._cache.clear()

    def _load_collection(self, name: str) -> _CollectionData:
        cached = self._cache.get(name)
        if cached is not None:
            return cached

        manifest = json.loads(_manifest_path(self.path, name).read_text(encoding="utf-8"))
        count = int(manifest.get("count", 0))
        vector_size = int(manifest["vector_size"])
        metadata = []
        with _meta_path(self.path, name).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                metadata.append(json.loads(line))
        if len(metadata) != count:
            raise RuntimeError(
                f"Vector metadata count mismatch for collection {name}: "
                f"{len(metadata)} != {count}"
            )
        vectors_file = _vectors_path(self.path, name)
        raw = np.memmap(
            vectors_file,
            dtype=_VECTOR_DTYPE,
            mode="r",
            shape=(count, vector_size),
            order="C",
        )
        if vectors_file.stat().st_size <= _PRELOAD_MAX_BYTES:
            vectors = np.asarray(raw, dtype=np.float32)
        else:
            vectors = raw
        data = _CollectionData(
            manifest=manifest,
            metadata=metadata,
            message_uids=[str(item.get("message_uid", "")) for item in metadata],
            run_ids=np.asarray([str(item.get("run_id", "")) for item in metadata], dtype=object),
            chat_id_raws=np.asarray(
                [str(item.get("chat_id_raw", "")) for item in metadata], dtype=object
            ),
            chat_alias_ids=np.asarray(
                [str(item.get("chat_alias_id", "")) for item in metadata], dtype=object
            ),
            timestamp_ms=np.asarray(
                [int(item.get("timestamp_ms", 0) or 0) for item in metadata],
                dtype=np.int64,
            ),
            vectors=vectors,
        )
        self._cache[name] = data
        return data
