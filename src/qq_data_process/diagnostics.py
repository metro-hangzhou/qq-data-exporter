from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(slots=True)
class AssetCoverageStats:
    referenced: int = 0
    materialized: int = 0
    missing: int = 0
    error: int = 0

    @property
    def missing_ratio(self) -> float:
        return self.missing / self.referenced if self.referenced else 0.0


@dataclass(slots=True)
class ExportDiagnosticReport:
    export_path: Path
    manifest_path: Path | None
    message_count: int
    chat_type: str
    chat_id: str
    image_stats: AssetCoverageStats
    file_stats: AssetCoverageStats
    sticker_static_stats: AssetCoverageStats
    sticker_dynamic_stats: AssetCoverageStats
    video_stats: AssetCoverageStats
    speech_stats: AssetCoverageStats
    manifest_total_assets: int
    manifest_missing: int

    def format_summary(self) -> str:
        return "\n".join(
            [
                "Export Diagnostic Report",
                f"- ExportPath: {self.export_path}",
                f"- ManifestPath: {self.manifest_path or 'none'}",
                f"- Chat: {self.chat_type}:{self.chat_id}",
                f"- Messages: {self.message_count}",
                "",
                "Media Coverage Summary",
                f"- Images: refs={self.image_stats.referenced} mat={self.image_stats.materialized} miss={self.image_stats.missing} err={self.image_stats.error}",
                f"- Files: refs={self.file_stats.referenced} mat={self.file_stats.materialized} miss={self.file_stats.missing} err={self.file_stats.error}",
                f"- Stickers(static): refs={self.sticker_static_stats.referenced} mat={self.sticker_static_stats.materialized} miss={self.sticker_static_stats.missing} err={self.sticker_static_stats.error}",
                f"- Stickers(dynamic): refs={self.sticker_dynamic_stats.referenced} mat={self.sticker_dynamic_stats.materialized} miss={self.sticker_dynamic_stats.missing} err={self.sticker_dynamic_stats.error}",
                f"- Videos: refs={self.video_stats.referenced} mat={self.video_stats.materialized} miss={self.video_stats.missing} err={self.video_stats.error}",
                f"- Speech: refs={self.speech_stats.referenced} mat={self.speech_stats.materialized} miss={self.speech_stats.missing} err={self.speech_stats.error}",
                f"- ManifestTotalAssets: {self.manifest_total_assets}",
                f"- ManifestMissing: {self.manifest_missing}",
            ]
        )


def diagnose_export(
    export_path: Path,
    manifest_path: Path | None = None,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> ExportDiagnosticReport:
    resolved_manifest = manifest_path or export_path.with_suffix("").with_suffix(
        ".manifest.json"
    )
    manifest_payload = {}
    manifest_assets: list[dict] = []
    if resolved_manifest.exists():
        manifest_payload = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        manifest_assets = list(manifest_payload.get("assets", []))
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "diagnostics_manifest",
                    "current": len(manifest_assets),
                    "total": len(manifest_assets),
                    "message": f"Loaded manifest asset entries {len(manifest_assets)}",
                }
            )
    else:
        resolved_manifest = None

    message_count = 0
    chat_type = ""
    chat_id = ""
    image_refs = 0
    file_refs = 0
    video_refs = 0
    speech_refs = 0
    sticker_refs = 0

    with export_path.open("r", encoding="utf-8") as handle:
        for line_index, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            message_count += 1
            chat_type = chat_type or str(payload.get("chat_type", ""))
            chat_id = chat_id or str(payload.get("chat_id", ""))
            for segment in payload.get("segments", []):
                segment_type = segment.get("type")
                if segment_type == "image":
                    image_refs += 1
                elif segment_type in {"file", "onlinefile"}:
                    file_refs += 1
                elif segment_type == "video":
                    video_refs += 1
                elif segment_type in {"speech", "record"}:
                    speech_refs += 1
                elif segment_type == "sticker":
                    sticker_refs += 1
            if progress_callback is not None and message_count % 5000 == 0:
                progress_callback(
                    {
                        "phase": "diagnostics_scan",
                        "current": message_count,
                        "total": 0,
                        "message": (
                            f"Scanned {message_count} messages for media coverage "
                            f"(line={line_index})"
                        ),
                    }
                )

    def _build_stats(
        asset_type: str, *, asset_role: str | None = None
    ) -> AssetCoverageStats:
        rows = [
            item
            for item in manifest_assets
            if item.get("asset_type") == asset_type
            and (asset_role is None or item.get("asset_role") == asset_role)
        ]
        return AssetCoverageStats(
            referenced=len(rows),
            materialized=sum(
                1 for item in rows if item.get("status") in {"copied", "reused"}
            ),
            missing=sum(1 for item in rows if item.get("status") == "missing"),
            error=sum(1 for item in rows if item.get("status") == "error"),
        )

    image_stats = _build_stats("image")
    if image_stats.referenced == 0:
        image_stats.referenced = image_refs

    file_stats = _build_stats("file")
    if file_stats.referenced == 0:
        file_stats.referenced = file_refs

    video_stats = _build_stats("video")
    if video_stats.referenced == 0:
        video_stats.referenced = video_refs

    speech_stats = _build_stats("speech")
    if speech_stats.referenced == 0:
        speech_stats.referenced = speech_refs

    sticker_static_stats = _build_stats("sticker", asset_role="static")
    if sticker_static_stats.referenced == 0:
        sticker_static_stats.referenced = sticker_refs

    sticker_dynamic_rows = [
        item
        for item in manifest_assets
        if item.get("asset_type") == "sticker" and item.get("asset_role") == "dynamic"
    ]
    sticker_dynamic_stats = AssetCoverageStats(
        referenced=0,
        materialized=sum(
            1
            for item in sticker_dynamic_rows
            if item.get("status") in {"copied", "reused"}
        ),
        missing=sum(
            1 for item in sticker_dynamic_rows if item.get("status") == "missing"
        ),
        error=sum(1 for item in sticker_dynamic_rows if item.get("status") == "error"),
    )

    return ExportDiagnosticReport(
        export_path=export_path,
        manifest_path=resolved_manifest,
        message_count=message_count,
        chat_type=chat_type,
        chat_id=chat_id,
        image_stats=image_stats,
        file_stats=file_stats,
        sticker_static_stats=sticker_static_stats,
        sticker_dynamic_stats=sticker_dynamic_stats,
        video_stats=video_stats,
        speech_stats=speech_stats,
        manifest_total_assets=len(manifest_assets),
        manifest_missing=sum(
            1 for item in manifest_assets if item.get("status") == "missing"
        ),
    )
