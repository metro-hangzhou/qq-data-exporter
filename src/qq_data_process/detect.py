from __future__ import annotations

import json
from pathlib import Path

from .models import ImportSource, LocalCorpusDescriptor


def resolve_source_path(source_path: Path) -> Path:
    if source_path.is_dir():
        for candidate_name in ("export.jsonl", "canonical_messages.jsonl", "canonical_messages.local_assets.jsonl"):
            candidate = source_path / candidate_name
            if candidate.exists():
                return candidate
        raise ValueError(
            "Directory source must contain one of "
            f"export.jsonl / canonical_messages.jsonl / canonical_messages.local_assets.jsonl: {source_path}"
        )
    return source_path


def detect_source_type(source_path: Path) -> ImportSource:
    resolved = resolve_source_path(source_path)
    suffix = resolved.suffix.lower()
    if suffix == ".jsonl":
        return "exporter_jsonl"
    if suffix == ".json":
        return "qce_json"
    if suffix == ".txt":
        return "qq_txt"
    raise ValueError(f"Unsupported preprocessing source suffix: {resolved.suffix}")


def load_local_corpus_descriptor(corpus_dir: Path) -> LocalCorpusDescriptor:
    resolved = corpus_dir.expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise FileNotFoundError(f"Local corpus directory does not exist: {corpus_dir}")
    export_jsonl_path = resolved / "export.jsonl"
    manifest_path = resolved / "export.manifest.json"
    assets_dir = resolved / "assets"
    dataset_meta_path = resolved / "dataset_meta.json"
    if not export_jsonl_path.exists():
        raise FileNotFoundError(f"Local corpus is missing export.jsonl: {resolved}")
    if not manifest_path.exists():
        raise FileNotFoundError(f"Local corpus is missing export.manifest.json: {resolved}")
    if not assets_dir.exists():
        raise FileNotFoundError(f"Local corpus is missing assets/: {resolved}")
    dataset_meta = (
        json.loads(dataset_meta_path.read_text(encoding="utf-8"))
        if dataset_meta_path.exists()
        else {}
    )
    corpus_id = str(dataset_meta.get("corpus_id") or resolved.name)
    return LocalCorpusDescriptor(
        corpus_id=corpus_id,
        corpus_dir=resolved,
        export_jsonl_path=export_jsonl_path,
        manifest_path=manifest_path,
        assets_dir=assets_dir,
        dataset_meta=dataset_meta,
    )
