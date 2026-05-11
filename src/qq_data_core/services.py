from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from .export_forensics import ExportForensicsCollector
from .exporters import write_jsonl, write_txt
from .media_bundle import write_export_bundle
from .models import ExportBundleResult, NormalizedSnapshot, SourceChatSnapshot
from .normalize import normalize_snapshot

SUPPORTED_EXPORT_FORMATS = frozenset({"jsonl", "txt"})


def normalize_export_format(fmt: str) -> str:
    normalized = str(fmt or "").strip().lower()
    if normalized not in SUPPORTED_EXPORT_FORMATS:
        raise ValueError(
            f"Unsupported export format {fmt!r}. Expected one of: {', '.join(sorted(SUPPORTED_EXPORT_FORMATS))}."
        )
    return normalized


class ChatExportService:
    def build_snapshot(
        self,
        source_snapshot: SourceChatSnapshot,
        *,
        include_raw: bool = False,
    ) -> NormalizedSnapshot:
        return normalize_snapshot(source_snapshot, include_raw=include_raw)

    def write_jsonl(self, snapshot: NormalizedSnapshot, output_path: Path) -> Path:
        return write_jsonl(snapshot, output_path)

    def write_txt(self, snapshot: NormalizedSnapshot, output_path: Path) -> Path:
        return write_txt(snapshot, output_path)

    def write_bundle(
        self,
        snapshot: NormalizedSnapshot,
        output_path: Path,
        *,
        fmt: str,
        media_resolution_mode: Literal["napcat_only", "legacy_local_research"] = "legacy_local_research",
        media_search_roots: Iterable[Path] | None = None,
        media_cache_dir: Path | None = None,
        media_download_callback: Callable[[dict[str, Any]], str | Path | None] | None = None,
        media_download_manager: Any | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        forensics_collector: ExportForensicsCollector | None = None,
    ) -> ExportBundleResult:
        normalized_fmt = normalize_export_format(fmt)
        writer: Callable[[NormalizedSnapshot, Path], Path]
        if normalized_fmt == "txt":
            writer = write_txt
        else:
            writer = write_jsonl
        return write_export_bundle(
            snapshot,
            output_path,
            write_data=writer,
            media_resolution_mode=media_resolution_mode,
            media_search_roots=media_search_roots,
            media_cache_dir=media_cache_dir,
            media_download_callback=media_download_callback,
            media_download_manager=media_download_manager,
            progress_callback=progress_callback,
            forensics_collector=forensics_collector,
        )
