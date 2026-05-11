from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .detect import detect_source_type, load_local_corpus_descriptor, resolve_source_path
from .models import ImportSource, LocalCorpusDescriptor


DEFAULT_CORPORA_INDEX_PATH = Path("dev/testdata/local/corpora_index.json")


class LocalCorpusIndexEntry(BaseModel):
    corpus_id: str
    path: str
    role: str | None = None
    deletion_guard: bool = False
    chat_id: str | None = None
    chat_name: str | None = None
    record_count: int | None = None
    large_group_corpus: bool | None = None
    shi_density: str | None = None
    manifest_shape_version_detected: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class LocalCorpusResolution(BaseModel):
    corpus_id: str
    corpus_dir: Path
    source_path: Path
    source_type: ImportSource
    chat_id: str
    chat_name: str | None = None
    role: str | None = None
    record_count: int | None = None
    deletion_guard: bool = False
    large_group_corpus: bool | None = None
    summary_path: Path | None = None
    manifest_path: Path | None = None
    assets_dir: Path | None = None
    dataset_meta: dict[str, Any] = Field(default_factory=dict)
    summary_payload: dict[str, Any] = Field(default_factory=dict)
    index_entry: LocalCorpusIndexEntry | None = None
    descriptor: LocalCorpusDescriptor | None = None


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _read_json(path)


def load_local_corpora_index(
    corpora_index_path: Path = DEFAULT_CORPORA_INDEX_PATH,
) -> dict[str, LocalCorpusIndexEntry]:
    resolved = corpora_index_path.expanduser().resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    mapping: dict[str, LocalCorpusIndexEntry] = {}
    for raw in payload.get("corpora") or []:
        known = {
            "corpus_id",
            "path",
            "role",
            "deletion_guard",
            "chat_id",
            "chat_name",
            "record_count",
            "large_group_corpus",
            "shi_density",
            "manifest_shape_version_detected",
        }
        extra = {k: v for k, v in raw.items() if k not in known}
        item = LocalCorpusIndexEntry(
            corpus_id=str(raw.get("corpus_id") or ""),
            path=str(raw.get("path") or ""),
            role=raw.get("role"),
            deletion_guard=bool(raw.get("deletion_guard", False)),
            chat_id=str(raw.get("chat_id") or "") or None,
            chat_name=str(raw.get("chat_name") or "") or None,
            record_count=raw.get("record_count"),
            large_group_corpus=raw.get("large_group_corpus"),
            shi_density=raw.get("shi_density"),
            manifest_shape_version_detected=raw.get("manifest_shape_version_detected"),
            extra=extra,
        )
        if item.corpus_id:
            mapping[item.corpus_id] = item
    return mapping


def resolve_local_corpora(
    *,
    corpora_index_path: Path = DEFAULT_CORPORA_INDEX_PATH,
    include_corpus_ids: list[str] | None = None,
    exclude_corpus_ids: list[str] | None = None,
    roles: list[str] | None = None,
    large_group_only: bool = False,
    include_reference: bool = False,
) -> list[LocalCorpusResolution]:
    index = load_local_corpora_index(corpora_index_path)
    include_set = {str(item).strip() for item in (include_corpus_ids or []) if str(item).strip()}
    exclude_set = {str(item).strip() for item in (exclude_corpus_ids or []) if str(item).strip()}
    role_set = {str(item).strip() for item in (roles or []) if str(item).strip()}

    selected: list[LocalCorpusResolution] = []
    for corpus_id, entry in index.items():
        if include_set and corpus_id not in include_set:
            continue
        if corpus_id in exclude_set:
            continue
        if role_set and str(entry.role or "") not in role_set:
            continue
        if large_group_only and not bool(entry.large_group_corpus):
            continue
        if not include_reference and str(entry.role or "") == "central_reference_baseline":
            continue
        selected.append(resolve_local_corpus(corpus_id, corpora_index_path=corpora_index_path))
    return selected


def build_filtered_corpora_index_payload(
    corpora: list[LocalCorpusResolution],
    *,
    schema_version: int = 1,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "generated_at": datetime.now().date().isoformat(),
        "corpora": [
            {
                "corpus_id": corpus.corpus_id,
                "path": str(corpus.corpus_dir),
                "role": corpus.role,
                "deletion_guard": corpus.deletion_guard,
                "chat_id": corpus.chat_id,
                "chat_name": corpus.chat_name,
                "record_count": corpus.record_count,
                "large_group_corpus": corpus.large_group_corpus,
                "shi_density": corpus.dataset_meta.get("shi_density") or corpus.summary_payload.get("shi_density"),
                "manifest_shape_version_detected": corpus.dataset_meta.get("manifest_shape_version_detected")
                or (corpus.index_entry.manifest_shape_version_detected if corpus.index_entry else None),
            }
            for corpus in corpora
        ],
    }


def resolve_local_corpus(
    target: str | Path,
    *,
    corpora_index_path: Path = DEFAULT_CORPORA_INDEX_PATH,
) -> LocalCorpusResolution:
    target_str = str(target).strip()
    index = load_local_corpora_index(corpora_index_path)

    index_entry = index.get(target_str)
    if index_entry is not None:
        corpus_dir = Path(index_entry.path)
    else:
        corpus_dir = Path(target_str)
        index_entry = None

    corpus_dir = corpus_dir.expanduser().resolve()
    if not corpus_dir.exists() or not corpus_dir.is_dir():
        raise FileNotFoundError(f"Local corpus directory does not exist: {corpus_dir}")

    if index_entry is None:
        for candidate in index.values():
            candidate_path = Path(candidate.path).expanduser().resolve()
            if candidate_path == corpus_dir:
                index_entry = candidate
                break

    source_path = resolve_source_path(corpus_dir)
    source_type = detect_source_type(corpus_dir)
    dataset_meta = _optional_json(corpus_dir / "dataset_meta.json")
    summary_path = corpus_dir / "summary.json"
    summary_payload = _optional_json(summary_path)
    manifest_path = corpus_dir / "export.manifest.json"
    assets_dir = corpus_dir / "assets"

    descriptor: LocalCorpusDescriptor | None = None
    try:
        descriptor = load_local_corpus_descriptor(corpus_dir)
    except (FileNotFoundError, ValueError):
        descriptor = None

    corpus_id = str(
        dataset_meta.get("corpus_id")
        or (index_entry.corpus_id if index_entry is not None else "")
        or corpus_dir.name
    ).strip()
    chat_id = str(
        dataset_meta.get("chat_id")
        or (index_entry.chat_id if index_entry is not None else "")
        or summary_payload.get("group_id")
        or summary_payload.get("chat_id")
        or ""
    ).strip()
    if not chat_id:
        raise RuntimeError(f"chat_id missing for local corpus target: {target}")
    chat_name = str(
        dataset_meta.get("chat_name")
        or (index_entry.chat_name if index_entry is not None else "")
        or corpus_id
    ).strip()
    return LocalCorpusResolution(
        corpus_id=corpus_id,
        corpus_dir=corpus_dir,
        source_path=source_path,
        source_type=source_type,
        chat_id=chat_id,
        chat_name=chat_name,
        role=(
            dataset_meta.get("role_assignment")
            or (index_entry.role if index_entry is not None else None)
        ),
        record_count=(
            dataset_meta.get("record_count")
            or (index_entry.record_count if index_entry is not None else None)
            or summary_payload.get("record_count")
        ),
        deletion_guard=bool(index_entry.deletion_guard) if index_entry is not None else False,
        large_group_corpus=(
            dataset_meta.get("large_group_corpus")
            if "large_group_corpus" in dataset_meta
            else (index_entry.large_group_corpus if index_entry is not None else None)
        ),
        summary_path=summary_path if summary_path.exists() else None,
        manifest_path=manifest_path if manifest_path.exists() else None,
        assets_dir=assets_dir if assets_dir.exists() else None,
        dataset_meta=dataset_meta,
        summary_payload=summary_payload,
        index_entry=index_entry,
        descriptor=descriptor,
    )
