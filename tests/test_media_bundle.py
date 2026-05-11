from __future__ import annotations

from pathlib import Path
import shutil
import uuid
from contextlib import contextmanager
import hashlib
import os
from datetime import datetime

import orjson

from qq_data_core import ChatExportService
from qq_data_core.export_forensics import ExportForensicsCollector, ExportInvestigativeFailure, StrictMissingPolicy
from qq_data_core.models import (
    EXPORT_TIMEZONE,
    NormalizedMessage,
    NormalizedSegment,
    NormalizedSnapshot,
)
from qq_data_integrations import FixtureSnapshotLoader


@contextmanager
def _repo_temp_dir(prefix: str):
    temp_dir = Path("state") / "test_tmp" / f"{prefix}_{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield temp_dir
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_write_bundle_materializes_direct_segment_paths() -> None:
    with _repo_temp_dir("media_bundle_direct") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        source_root = tmp_path / "source"
        image_path = source_root / "Pic" / "demo_image.JPG"
        file_path = source_root / "FileRecv" / "report.png"
        speech_path = source_root / "Ptt" / "VOICE.amr"
        sticker_static = (
            source_root / "Emoji" / "marketface" / "237962" / "821860_aio.png"
        )
        sticker_dynamic = source_root / "Emoji" / "marketface" / "237962" / "821860"
        for path, payload in [
            (image_path, b"\xff\xd8\xff\xdbfakejpg"),
            (file_path, b"fakepng"),
            (speech_path, b"#!AMR\nvoice"),
            (sticker_static, b"\x89PNG\r\n\x1a\npng"),
            (sticker_dynamic, b"RIFFxxxxWEBPdata"),
        ]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        normalized.messages[1].segments[0].path = str(image_path)
        normalized.messages[2].segments[0].path = str(file_path)
        normalized.messages[5].segments[0].path = str(speech_path)
        normalized.messages[4].segments[0].extra["static_path"] = str(sticker_static)
        normalized.messages[4].segments[0].extra["dynamic_path"] = str(sticker_dynamic)

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000000.jsonl"
        bundle = service.write_bundle(normalized, out_path, fmt="jsonl")

        assert bundle.data_path.exists()
        assert bundle.manifest_path.exists()
        assert bundle.copied_asset_count == 5
        assert bundle.missing_asset_count == 0
        assert (bundle.assets_dir / "images" / "demo_image.JPG").exists()
        assert (bundle.assets_dir / "files" / "report.png").exists()
        assert (bundle.assets_dir / "audio" / "VOICE.amr").exists()
        assert (bundle.assets_dir / "stickers" / "static" / "821860_aio.png").exists()
        assert (bundle.assets_dir / "stickers" / "dynamic" / "821860.webp").exists()

        manifest = orjson.loads(bundle.manifest_path.read_bytes())
        assert manifest["asset_summary"]["copied"] == 5
        assert manifest["data_file"] == out_path.name


def test_write_bundle_emits_materialization_step_timing_progress() -> None:
    with _repo_temp_dir("media_bundle_materialize_step_progress") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        source_root = tmp_path / "source"
        image_path = source_root / "Pic" / "demo_image.JPG"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_bytes(b"\xff\xd8\xff\xdbfakejpg")
        normalized.messages[1].segments[0].path = str(image_path)
        normalized.messages[1].segments[0].extra["file_id"] = "image-file-id-1"
        normalized.messages[1].segments[0].extra["url"] = "https://example.com/demo_image.JPG"

        out_path = tmp_path / "exports" / "friend_1507833383_step_progress.jsonl"
        progress_events: list[dict[str, object]] = []
        service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            progress_callback=progress_events.append,
        )

        step_start = next(
            event
            for event in progress_events
            if event.get("phase") == "materialize_asset_step" and event.get("stage") == "start"
        )
        step_done = next(
            event
            for event in progress_events
            if event.get("phase") == "materialize_asset_step" and event.get("stage") == "done"
        )
        materialize = next(
            event
            for event in progress_events
            if event.get("phase") == "materialize_assets"
        )

        assert step_start["current"] == 1
        assert step_start["file_name"] == "demo_image.JPG"
        assert step_start["hint_file_id"] == "image-file-id-1"
        assert step_start["hint_url"] == "https://example.com/demo_image.JPG"
        assert step_done["current"] == 1
        assert step_done["status"] == "copied"
        assert step_done["resolver"] == "segment_path"
        assert step_done["resolved_source_path"] == str(image_path.resolve())
        assert float(step_done["step_elapsed_s"]) >= 0.0
        assert materialize["status"] == "copied"
        assert materialize["resolver"] == "segment_path"
        assert float(materialize["step_elapsed_s"]) >= 0.0


def test_write_bundle_emits_missing_diagnostics_in_materialization_step_trace() -> None:
    with _repo_temp_dir("media_bundle_missing_step_diagnostics") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        image_segment = normalized.messages[1].segments[0]
        image_segment.path = ""
        image_segment.extra["file_id"] = "/downloaded-file-id-1"
        image_segment.extra["url"] = "https://multimedia.nt.qq.com.cn/download?file=demo"
        image_segment.extra["_forward_parent"] = {
            "message_id_raw": "msg-forward-parent-1",
            "element_id": "element-forward-parent-1",
        }

        class _MissingManager:
            def prepare_for_export(self, requests):
                return None

            def resolve_for_export(self, request):
                return None, "missing_after_napcat"

        out_path = tmp_path / "exports" / "friend_missing_step.jsonl"
        progress_events: list[dict[str, object]] = []
        service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_MissingManager(),
            progress_callback=progress_events.append,
        )

        step_done = next(
            event
            for event in progress_events
            if event.get("phase") == "materialize_asset_step" and event.get("stage") == "done"
        )
        assert step_done["status"] == "missing"
        assert step_done["resolver"] == "missing_after_napcat"
        assert step_done["missing_kind"] == "missing_after_napcat"
        assert step_done["hint_file_id"] == "/downloaded-file-id-1"
        assert step_done["hint_url"] == "https://multimedia.nt.qq.com.cn/download?file=demo"
        assert step_done["forward_parent_message_id_raw"] == "msg-forward-parent-1"
        assert step_done["forward_parent_element_id"] == "element-forward-parent-1"


def test_write_bundle_materializes_nested_forward_assets() -> None:
    with _repo_temp_dir("media_bundle_forward_nested") as tmp_path:
        service = ChatExportService()
        nested_image = tmp_path / "source" / "Pic" / "nested-forward.png"
        nested_image.parent.mkdir(parents=True, exist_ok=True)
        nested_image.write_bytes(b"\x89PNG\r\n\x1a\npng")

        snapshot = NormalizedSnapshot(
            chat_type="private",
            chat_id="1507833383",
            chat_name="1507833383",
            messages=[
                NormalizedMessage(
                    chat_type="private",
                    chat_id="1507833383",
                    peer_id="1507833383",
                    sender_id="3956020260",
                    sender_name="wiki",
                    message_id="fwd-1",
                    message_seq="94",
                    timestamp_ms=1773319027000,
                    timestamp_iso="2026-03-12T20:37:07+08:00",
                    content="[forward message] 甲: [image:nested-forward.png]",
                    text_content="甲: [image:nested-forward.png]",
                    segments=[
                        NormalizedSegment(
                            type="forward",
                            token="[forward message]",
                            summary="聊天记录",
                            extra={
                                "forward_depth": 2,
                                "forward_messages": [
                                    {
                                        "sender_id": "111",
                                        "sender_name": "甲",
                                        "content": "[forward message] [image:nested-forward.png]",
                                        "segments": [
                                            {
                                                "type": "forward",
                                                "extra": {
                                                    "forward_depth": 1,
                                                    "forward_messages": [
                                                        {
                                                            "sender_id": "222",
                                                            "sender_name": "乙",
                                                            "content": "[image:nested-forward.png]",
                                                            "segments": [
                                                                {
                                                                    "type": "image",
                                                                    "file_name": "nested-forward.png",
                                                                    "path": str(
                                                                        nested_image
                                                                    ),
                                                                    "md5": None,
                                                                    "extra": {},
                                                                }
                                                            ],
                                                        }
                                                    ],
                                                },
                                            }
                                        ],
                                    }
                                ],
                            },
                        )
                    ],
                )
            ],
        )

        out_path = tmp_path / "exports" / "friend_1507833383_nested.jsonl"
        bundle = service.write_bundle(snapshot, out_path, fmt="jsonl")

        assert bundle.copied_asset_count == 1
        assert bundle.missing_asset_count == 0
        assert (bundle.assets_dir / "images" / "nested-forward.png").exists()
        manifest = orjson.loads(bundle.manifest_path.read_bytes())
        assert manifest["asset_summary"]["copied"] == 1


def test_write_bundle_corrects_misleading_jpg_extension_for_gif_payload() -> None:
    with _repo_temp_dir("media_bundle_magic_ext") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        source_root = tmp_path / "source"
        disguised_gif = (
            source_root / "Emoji" / "emoji-recv" / "2026-02" / "Ori" / "animated.jpg"
        )
        disguised_gif.parent.mkdir(parents=True, exist_ok=True)
        disguised_gif.write_bytes(b"GIF89afakegifpayload")

        normalized.messages[1].segments[0].file_name = "animated.jpg"
        normalized.messages[1].segments[0].path = str(disguised_gif)

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000000.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].exported_rel_path == "images/animated.gif"
        assert (bundle.assets_dir / "images" / "animated.gif").exists()


def test_write_bundle_falls_back_to_qq_media_roots_and_guesses_extension() -> None:
    with _repo_temp_dir("media_bundle_fallback") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        expected_root = tmp_path / "QQ"
        actual_source = (
            expected_root
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2025-08"
            / "Ori"
            / "1cfd32f7610078b2771c7049e5b14459"
        )
        actual_source.parent.mkdir(parents=True, exist_ok=True)
        actual_source.write_bytes(b"\xff\xd8\xff\xe0jpegdata")

        image_segment = normalized.messages[1].segments[0]
        image_segment.file_name = "1cfd32f7610078b2771c7049e5b14459"
        image_segment.path = r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Ori\1cfd32f7610078b2771c7049e5b14459"

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000001.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[expected_root],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver in {
            "qq_media_root_scan",
            "qq_media_root_original_scan",
        }
        assert (
            bundle.assets[0].exported_rel_path
            == "images/1cfd32f7610078b2771c7049e5b14459.jpg"
        )
        assert (
            bundle.assets_dir / "images" / "1cfd32f7610078b2771c7049e5b14459.jpg"
        ).exists()


def test_write_bundle_downloads_blank_source_file_via_callback() -> None:
    with _repo_temp_dir("media_bundle_download_file") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        file_segment = normalized.messages[2].segments[0]
        file_segment.path = ""
        file_segment.extra["file_id"] = "file-uuid"

        downloaded_file = tmp_path / "downloads" / "report.png"
        downloaded_file.parent.mkdir(parents=True, exist_ok=True)
        downloaded_file.write_bytes(b"downloaded-report")

        seen_requests: list[dict[str, object]] = []

        def download_callback(payload: dict[str, object]) -> Path | None:
            seen_requests.append(payload)
            if payload.get("asset_type") == "file":
                return downloaded_file
            return None

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000001.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[2]]}),
            out_path,
            fmt="jsonl",
            media_download_callback=download_callback,
        )

        assert seen_requests
        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "napcat_action_download"
        assert bundle.assets[0].resolved_source_path == str(downloaded_file.resolve())
        assert (bundle.assets_dir / "files" / "report.png").exists()


def test_write_bundle_downloads_blank_source_image_via_callback() -> None:
    with _repo_temp_dir("media_bundle_download_image") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        image_segment = normalized.messages[1].segments[0]
        image_segment.path = ""
        image_segment.extra["file_id"] = "image-uuid"

        downloaded_image = tmp_path / "downloads" / "hydrated.png"
        downloaded_image.parent.mkdir(parents=True, exist_ok=True)
        downloaded_image.write_bytes(b"\x89PNG\r\n\x1a\nhydrated")

        seen_requests: list[dict[str, object]] = []

        def download_callback(payload: dict[str, object]) -> Path | None:
            seen_requests.append(payload)
            if payload.get("asset_type") == "image":
                return downloaded_image
            return None

        out_path = (
            tmp_path / "exports" / "friend_1507833383_20260309_000001_image.jsonl"
        )
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_download_callback=download_callback,
        )

        assert seen_requests
        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "napcat_action_download"
        assert bundle.assets[0].resolved_source_path == str(downloaded_image.resolve())
        assert bundle.assets[0].exported_rel_path is not None
        assert (bundle.assets_dir / bundle.assets[0].exported_rel_path).exists()


def test_write_bundle_napcat_only_ignores_callback_and_marks_missing() -> None:
    with _repo_temp_dir("media_bundle_napcat_only_missing") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        image_segment = normalized.messages[1].segments[0]
        image_segment.path = ""
        image_segment.extra["file_id"] = "image-uuid"

        downloaded_image = tmp_path / "downloads" / "hydrated.png"
        downloaded_image.parent.mkdir(parents=True, exist_ok=True)
        downloaded_image.write_bytes(b"\x89PNG\r\n\x1a\nhydrated")

        seen_requests: list[dict[str, object]] = []

        def download_callback(payload: dict[str, object]) -> Path | None:
            seen_requests.append(payload)
            return downloaded_image

        class _NullManager:
            def prepare_for_export(self, requests):
                return None

            def resolve_for_export(self, request):
                return None, None

        out_path = tmp_path / "exports" / "friend_napcat_only_missing.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_NullManager(),
            media_download_callback=download_callback,
        )

        assert seen_requests == []
        assert bundle.copied_asset_count == 0
        assert bundle.missing_asset_count == 1
        assert bundle.assets[0].resolver == "missing_after_napcat"
        assert bundle.assets[0].missing_kind == "missing_after_napcat"


def test_write_bundle_napcat_only_preserves_expired_missing_classification() -> None:
    with _repo_temp_dir("media_bundle_napcat_only_expired") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        image_segment = normalized.messages[1].segments[0]
        image_segment.path = ""
        image_segment.extra["file_id"] = "image-uuid"

        class _ExpiredManager:
            def prepare_for_export(self, requests):
                return None

            def resolve_for_export(self, request):
                return None, "qq_expired_after_napcat"

        out_path = tmp_path / "exports" / "friend_napcat_only_expired.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_ExpiredManager(),
        )

        assert bundle.copied_asset_count == 0
        assert bundle.missing_asset_count == 1
        assert bundle.assets[0].resolver == "qq_expired_after_napcat"
        assert bundle.assets[0].missing_kind == "qq_expired_after_napcat"
        assert "expired in QQ/NapCat" in (bundle.assets[0].note or "")


def test_write_bundle_napcat_only_uses_manager_resolution() -> None:
    with _repo_temp_dir("media_bundle_napcat_only_manager") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        image_segment = normalized.messages[1].segments[0]
        image_segment.path = ""
        image_segment.extra["file_id"] = "image-uuid"

        hydrated_image = tmp_path / "downloads" / "hydrated.png"
        hydrated_image.parent.mkdir(parents=True, exist_ok=True)
        hydrated_image.write_bytes(b"\x89PNG\r\n\x1a\nhydrated")

        class _Manager:
            def prepare_for_export(self, requests):
                return None

            def resolve_for_export(self, request):
                return hydrated_image, "napcat_context_hydrated"

        out_path = tmp_path / "exports" / "friend_napcat_only_manager.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_Manager(),
        )

        assert bundle.copied_asset_count == 1
        assert bundle.missing_asset_count == 0
        assert bundle.assets[0].resolver == "napcat_context_hydrated"
        assert (bundle.assets_dir / "images" / "demo_image.png").exists()


def test_write_bundle_napcat_only_second_pass_public_token_recovers_recent_missing() -> None:
    with _repo_temp_dir("media_bundle_napcat_only_second_pass_public") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        image_segment = normalized.messages[1].segments[0]
        image_segment.path = ""
        image_segment.extra["message_id_raw"] = "msg-recent-second-pass"
        image_segment.extra["element_id"] = "element-recent-second-pass"
        image_segment.extra["peer_uid"] = "1507833383"
        image_segment.extra["chat_type_raw"] = 1

        hydrated_image = tmp_path / "downloads" / "second-pass.png"
        hydrated_image.parent.mkdir(parents=True, exist_ok=True)
        hydrated_image.write_bytes(b"\x89PNG\r\n\x1a\nsecond-pass")

        class _Manager:
            def prepare_for_export(self, requests):
                return None

            def resolve_for_export(self, request):
                return None, "missing_after_napcat"

            def resolve_via_public_token_route(self, request):
                return hydrated_image, "napcat_public_token_get_image_remote_url"

        out_path = tmp_path / "exports" / "friend_napcat_only_second_pass.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_Manager(),
        )

        assert bundle.copied_asset_count == 1
        assert bundle.missing_asset_count == 0
        assert bundle.assets[0].resolver == "napcat_public_token_get_image_remote_url"
        assert (bundle.assets_dir / "images" / "demo_image.png").exists()


def test_write_bundle_falls_back_to_ntqq_thumb_when_original_missing() -> None:
    with _repo_temp_dir("media_bundle_ntqq_thumb_fallback") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        qq_root = tmp_path / "QQ"
        thumb_source = (
            qq_root
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2025-08"
            / "Thumb"
            / "thumbonly123_0.jpg"
        )
        thumb_source.parent.mkdir(parents=True, exist_ok=True)
        thumb_source.write_bytes(b"\xff\xd8\xff\xe0thumbjpg")

        image_segment = normalized.messages[1].segments[0]
        image_segment.file_name = "thumbonly123.jpg"
        image_segment.path = (
            r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Ori\thumbonly123.jpg"
        )
        image_segment.md5 = None

        out_path = (
            tmp_path / "exports" / "friend_1507833383_20260309_000001_thumb.jsonl"
        )
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[qq_root],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "qq_media_root_original_scan"
        assert bundle.assets[0].resolved_source_path == str(thumb_source.resolve())
        assert bundle.assets[0].exported_rel_path == "images/thumbonly123.jpg"
        assert (bundle.assets_dir / "images" / "thumbonly123.jpg").exists()


def test_write_bundle_prefers_larger_thumb_variant_over_zero_variant() -> None:
    with _repo_temp_dir("media_bundle_thumb_variant_priority") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        thumb_dir = (
            tmp_path
            / "QQ"
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2026-03"
            / "Thumb"
        )
        thumb_dir.mkdir(parents=True, exist_ok=True)
        zero_variant = thumb_dir / "1CD73C34F424B805F14A481531D0E692_0.png"
        big_variant = thumb_dir / "1CD73C34F424B805F14A481531D0E692_720.png"
        zero_variant.write_bytes(b"\x89PNG\r\n\x1a\nsmall")
        big_variant.write_bytes(b"\x89PNG\r\n\x1a\nlarge")

        image_segment = normalized.messages[1].segments[0]
        image_segment.file_name = "1CD73C34F424B805F14A481531D0E692.png"
        image_segment.path = None
        image_segment.md5 = "1cd73c34f424b805f14a481531d0e692"

        out_path = tmp_path / "exports" / "friend_1507833383_20260312_000001.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolved_source_path == str(big_variant.resolve())


def test_write_bundle_finds_ntqq_thumb_variant_without_extension_for_blank_forward_source() -> (
    None
):
    with _repo_temp_dir("media_bundle_ntqq_thumb_variant") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        thumb_path = (
            tmp_path
            / "QQ"
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2026-03"
            / "Thumb"
            / "1CD73C34F424B805F14A481531D0E692_0"
        )
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(b"\x89PNG\r\n\x1a\nthumb")

        image_segment = normalized.messages[1].segments[0]
        image_segment.file_name = "1CD73C34F424B805F14A481531D0E692.png"
        image_segment.path = None
        image_segment.md5 = "1cd73c34f424b805f14a481531d0e692"

        out_path = tmp_path / "exports" / "friend_1507833383_20260312_000000.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolved_source_path == str(thumb_path.resolve())


def test_write_bundle_searches_pic_thumb_for_emoji_recv_image_when_emoji_ori_missing() -> (
    None
):
    with _repo_temp_dir("media_bundle_emoji_recv_pic_thumb") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        qq_root = tmp_path / "QQ"
        thumb_source = (
            qq_root
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2025-10"
            / "Thumb"
            / "eaff7e714ba31eccb411ed872f1c3c23_0.jpg"
        )
        thumb_source.parent.mkdir(parents=True, exist_ok=True)
        thumb_source.write_bytes(b"\xff\xd8\xff\xe0thumbjpg")

        image_segment = normalized.messages[1].segments[0]
        image_segment.file_name = "EAFF7E714BA31ECCB411ED872F1C3C23.png"
        image_segment.path = r"C:\QQ\3956020260\nt_qq\nt_data\Emoji\emoji-recv\2025-10\Ori\eaff7e714ba31eccb411ed872f1c3c23.png"
        image_segment.md5 = None

        out_path = (
            tmp_path / "exports" / "friend_1507833383_20260309_000001_emoji_thumb.jsonl"
        )
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[qq_root],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "qq_media_root_original_scan"
        assert bundle.assets[0].resolved_source_path == str(thumb_source.resolve())
        assert (
            bundle.assets[0].exported_rel_path
            == "images/EAFF7E714BA31ECCB411ED872F1C3C23.jpg"
        )
        assert (
            bundle.assets_dir / "images" / "EAFF7E714BA31ECCB411ED872F1C3C23.jpg"
        ).exists()


def test_write_bundle_falls_back_to_legacy_group2_md5_lookup() -> None:
    with _repo_temp_dir("media_bundle_legacy_md5") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        payload = b"\x89PNG\r\n\x1a\nlegacy-png"
        digest = hashlib.md5(payload).hexdigest()
        legacy_file = (
            tmp_path
            / "QQ"
            / "3956020260"
            / "Image"
            / "Group2"
            / "$A"
            / "BC"
            / "$ABCLEGACY.png"
        )
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_bytes(payload)
        legacy_ts = datetime(2025, 8, 20, 12, 0, 0, tzinfo=EXPORT_TIMEZONE).timestamp()
        os.utime(legacy_file, (legacy_ts, legacy_ts))

        image_segment = normalized.messages[1].segments[0]
        message = normalized.messages[1]
        message.timestamp_ms = int(legacy_ts * 1000)
        message.timestamp_iso = datetime.fromtimestamp(
            legacy_ts, tz=EXPORT_TIMEZONE
        ).isoformat()
        image_segment.file_name = digest
        image_segment.md5 = digest
        image_segment.path = rf"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Ori\{digest}"

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000002.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [message]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "legacy_md5_index"
        assert bundle.assets[0].exported_rel_path == f"images/{digest}.png"
        assert (bundle.assets_dir / "images" / f"{digest}.png").exists()


def test_write_bundle_reuses_legacy_md5_cache_without_rehash(monkeypatch) -> None:
    with _repo_temp_dir("media_bundle_legacy_cache") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        payload = b"\xff\xd8\xff\xe0legacy-cache"
        digest = hashlib.md5(payload).hexdigest()
        legacy_file = (
            tmp_path
            / "QQ"
            / "3956020260"
            / "Image"
            / "Group2"
            / "AA"
            / "BB"
            / "legacy-cache.jpg"
        )
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_bytes(payload)
        legacy_ts = datetime(2025, 8, 20, 12, 0, 0, tzinfo=EXPORT_TIMEZONE).timestamp()
        os.utime(legacy_file, (legacy_ts, legacy_ts))

        image_segment = normalized.messages[1].segments[0]
        message = normalized.messages[1]
        message.timestamp_ms = int(legacy_ts * 1000)
        message.timestamp_iso = datetime.fromtimestamp(
            legacy_ts, tz=EXPORT_TIMEZONE
        ).isoformat()
        image_segment.file_name = digest
        image_segment.md5 = digest
        image_segment.path = rf"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Ori\{digest}"

        cache_dir = tmp_path / "cache"
        out_path_1 = tmp_path / "exports" / "friend_1507833383_20260309_000003.jsonl"
        bundle_1 = service.write_bundle(
            normalized.model_copy(update={"messages": [message]}),
            out_path_1,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
            media_cache_dir=cache_dir,
        )

        assert bundle_1.copied_asset_count == 1
        cache_files = list(cache_dir.glob("legacy_md5_*.json"))
        assert len(cache_files) == 1

        import qq_data_core.media_bundle as media_bundle

        def fail_file_md5(_path: Path) -> str | None:
            raise AssertionError(
                "legacy md5 cache should avoid recomputing unchanged files"
            )

        monkeypatch.setattr(media_bundle, "_file_md5", fail_file_md5)

        out_path_2 = tmp_path / "exports" / "friend_1507833383_20260309_000004.jsonl"
        bundle_2 = service.write_bundle(
            normalized.model_copy(update={"messages": [message]}),
            out_path_2,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
            media_cache_dir=cache_dir,
        )

        assert bundle_2.copied_asset_count == 1
        assert bundle_2.assets[0].resolver == "legacy_md5_index"


def test_write_bundle_limits_legacy_md5_scan_by_time_window(monkeypatch) -> None:
    with _repo_temp_dir("media_bundle_legacy_window") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        in_window_payload = b"\xff\xd8\xff\xe0in-window"
        out_window_payload = b"\xff\xd8\xff\xe0out-window"
        target_digest = hashlib.md5(in_window_payload).hexdigest()

        legacy_dir = tmp_path / "QQ" / "3956020260" / "Image" / "Group2" / "AA" / "BB"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        in_window = legacy_dir / "in-window.jpg"
        out_window = legacy_dir / "out-window.jpg"
        in_window.write_bytes(in_window_payload)
        out_window.write_bytes(out_window_payload)

        in_window_ts = datetime(
            2025, 8, 20, 12, 0, 0, tzinfo=EXPORT_TIMEZONE
        ).timestamp()
        out_window_ts = datetime(
            2024, 1, 15, 12, 0, 0, tzinfo=EXPORT_TIMEZONE
        ).timestamp()
        os.utime(in_window, (in_window_ts, in_window_ts))
        os.utime(out_window, (out_window_ts, out_window_ts))

        message = normalized.messages[1]
        message.timestamp_ms = int(in_window_ts * 1000)
        message.timestamp_iso = datetime.fromtimestamp(
            in_window_ts, tz=EXPORT_TIMEZONE
        ).isoformat()
        image_segment = message.segments[0]
        image_segment.file_name = target_digest
        image_segment.md5 = target_digest
        image_segment.path = (
            rf"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Ori\{target_digest}"
        )

        import qq_data_core.media_bundle as media_bundle

        original_file_md5 = media_bundle._file_md5
        hashed_names: list[str] = []

        def tracking_file_md5(path: Path) -> str | None:
            hashed_names.append(path.name)
            return original_file_md5(path)

        monkeypatch.setattr(media_bundle, "_file_md5", tracking_file_md5)

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000005.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [message]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
        )

        assert bundle.copied_asset_count == 1
        assert hashed_names == ["in-window.jpg"]


def test_write_bundle_legacy_md5_loose_fallback_recovers_old_reused_cache(
    monkeypatch,
) -> None:
    with _repo_temp_dir("media_bundle_legacy_loose_fallback") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        payload = b"GIF89aold-reused-cache"
        target_digest = hashlib.md5(payload).hexdigest()
        legacy_dir = tmp_path / "QQ" / "3956020260" / "Image" / "Group2" / "IP" / "YT"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_file = legacy_dir / "IPYTB_L(W]U4O8Z`XOB)([X.gif"
        legacy_file.write_bytes(payload)

        # Simulate a file cached long before the later message that references it.
        old_cache_ts = datetime(
            2025, 8, 27, 21, 42, 35, tzinfo=EXPORT_TIMEZONE
        ).timestamp()
        message_ts = datetime(
            2025, 10, 4, 3, 43, 33, tzinfo=EXPORT_TIMEZONE
        ).timestamp()
        os.utime(legacy_file, (old_cache_ts, old_cache_ts))

        message = normalized.messages[1]
        message.timestamp_ms = int(message_ts * 1000)
        message.timestamp_iso = datetime.fromtimestamp(
            message_ts, tz=EXPORT_TIMEZONE
        ).isoformat()
        image_segment = message.segments[0]
        image_segment.file_name = f"{target_digest}.jpg"
        image_segment.md5 = target_digest
        image_segment.path = (
            rf"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-10\Ori\{target_digest}"
        )

        import qq_data_core.media_bundle as media_bundle

        original_file_md5 = media_bundle._file_md5
        hashed_names: list[str] = []

        def tracking_file_md5(path: Path) -> str | None:
            hashed_names.append(path.name)
            return original_file_md5(path)

        monkeypatch.setattr(media_bundle, "_file_md5", tracking_file_md5)

        out_path = (
            tmp_path / "exports" / "friend_1507833383_20260309_000005_loose.jsonl"
        )
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [message]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "legacy_md5_index"
        assert bundle.assets[0].resolved_source_path == str(legacy_file.resolve())
        assert bundle.assets[0].exported_rel_path == f"images/{target_digest}.gif"
        assert "IPYTB_L(W]U4O8Z`XOB)([X.gif" in hashed_names


def test_write_bundle_legacy_md5_loose_bucket_reused_once(monkeypatch) -> None:
    with _repo_temp_dir("media_bundle_legacy_loose_bucket_once") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        payload_a = b"GIF89aloose-a"
        payload_b = b"GIF89aloose-b"
        digest_a = hashlib.md5(payload_a).hexdigest()
        digest_b = hashlib.md5(payload_b).hexdigest()
        legacy_dir = tmp_path / "QQ" / "3956020260" / "Image" / "Group2" / "IP" / "YT"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "A.gif").write_bytes(payload_a)
        (legacy_dir / "B.gif").write_bytes(payload_b)

        old_cache_ts = datetime(
            2025, 8, 27, 21, 42, 35, tzinfo=EXPORT_TIMEZONE
        ).timestamp()
        os.utime(legacy_dir / "A.gif", (old_cache_ts, old_cache_ts))
        os.utime(legacy_dir / "B.gif", (old_cache_ts, old_cache_ts))

        message_a = normalized.messages[1].model_copy(deep=True)
        message_b = normalized.messages[1].model_copy(deep=True)
        for msg, digest, seq in [
            (message_a, digest_a, "1"),
            (message_b, digest_b, "2"),
        ]:
            message_ts = datetime(
                2025, 10, 4, 3, 43, 33, tzinfo=EXPORT_TIMEZONE
            ).timestamp()
            msg.timestamp_ms = int(message_ts * 1000)
            msg.timestamp_iso = datetime.fromtimestamp(
                message_ts, tz=EXPORT_TIMEZONE
            ).isoformat()
            msg.message_seq = seq
            image_segment = msg.segments[0]
            image_segment.file_name = f"{digest}.jpg"
            image_segment.md5 = digest
            image_segment.path = (
                rf"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-10\Ori\{digest}"
            )

        import qq_data_core.media_bundle as media_bundle

        original_build = media_bundle._build_legacy_md5_matches
        loose_calls = 0

        def tracking_build(*args, **kwargs):
            nonlocal loose_calls
            if (
                kwargs.get("time_window_ms") is None
                and kwargs.get("month_hints") == set()
            ):
                loose_calls += 1
            return original_build(*args, **kwargs)

        monkeypatch.setattr(media_bundle, "_build_legacy_md5_matches", tracking_build)

        out_path = (
            tmp_path
            / "exports"
            / "friend_1507833383_20260309_000005_loose_bucket.jsonl"
        )
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [message_a, message_b]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[tmp_path / "QQ"],
        )

        assert bundle.copied_asset_count == 2
        assert loose_calls == 1


def test_write_bundle_prefers_original_gif_over_thumb_jpg() -> None:
    with _repo_temp_dir("media_bundle_prefers_original_gif") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        qq_root = tmp_path / "QQ"
        ori_file = (
            qq_root
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2025-08"
            / "Ori"
            / "animated123.gif"
        )
        thumb_file = (
            qq_root
            / "3956020260"
            / "nt_qq"
            / "nt_data"
            / "Pic"
            / "2025-08"
            / "Thumb"
            / "animated123_0.jpg"
        )
        ori_file.parent.mkdir(parents=True, exist_ok=True)
        thumb_file.parent.mkdir(parents=True, exist_ok=True)
        ori_file.write_bytes(b"GIF89afakegif")
        thumb_file.write_bytes(b"\xff\xd8\xff\xe0thumbjpg")

        image_segment = normalized.messages[1].segments[0]
        image_segment.file_name = "animated123"
        image_segment.path = (
            r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2025-08\Thumb\animated123_0.jpg"
        )
        image_segment.md5 = None

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000006.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[1]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[qq_root],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "qq_media_root_original_scan"
        assert bundle.assets[0].resolved_source_path == str(ori_file.resolve())
        assert bundle.assets[0].exported_rel_path == "images/animated123.gif"
        assert (bundle.assets_dir / "images" / "animated123.gif").exists()


def test_write_bundle_searches_file_name_when_source_path_is_blank() -> None:
    with _repo_temp_dir("media_bundle_file_name_scan") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        qq_root = tmp_path / "QQ"
        file_source = qq_root / "3956020260" / "FileRecv" / "report-final.zip"
        file_source.parent.mkdir(parents=True, exist_ok=True)
        file_source.write_bytes(b"zipdata")

        file_segment = normalized.messages[2].segments[0]
        file_segment.file_name = "report-final.zip"
        file_segment.path = None
        file_segment.md5 = None

        out_path = tmp_path / "exports" / "friend_1507833383_20260309_000007.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[2]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[qq_root],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolver == "qq_media_root_scan"
        assert bundle.assets[0].resolved_source_path == str(file_source.resolve())
        assert bundle.assets[0].exported_rel_path == "files/report-final.zip"
        assert (bundle.assets_dir / "files" / "report-final.zip").exists()


def test_write_bundle_blank_file_source_avoids_full_root_rglob(monkeypatch) -> None:
    with _repo_temp_dir("media_bundle_targeted_file_search") as tmp_path:
        loader = FixtureSnapshotLoader()
        service = ChatExportService()
        snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
        normalized = service.build_snapshot(snapshot)

        qq_root = tmp_path / "QQ"
        file_source = qq_root / "3956020260" / "FileRecv" / "screen-record.mp4"
        file_source.parent.mkdir(parents=True, exist_ok=True)
        file_source.write_bytes(b"mp4data")

        file_segment = normalized.messages[2].segments[0]
        file_segment.file_name = "screen-record.mp4"
        file_segment.path = None
        file_segment.md5 = None

        original_rglob = Path.rglob

        def guarded_rglob(self: Path, pattern: str):
            if self.resolve() == qq_root.resolve():
                raise AssertionError(
                    "full-root rglob should not be used for blank file source fallback"
                )
            return original_rglob(self, pattern)

        monkeypatch.setattr(Path, "rglob", guarded_rglob)

        out_path = tmp_path / "exports" / "friend_targeted_file.jsonl"
        bundle = service.write_bundle(
            normalized.model_copy(update={"messages": [normalized.messages[2]]}),
            out_path,
            fmt="jsonl",
            media_search_roots=[qq_root],
        )

        assert bundle.copied_asset_count == 1
        assert bundle.assets[0].resolved_source_path == str(file_source.resolve())


def test_write_bundle_reuses_resolution_for_duplicate_assets() -> None:
    with _repo_temp_dir("media_bundle_resolution_cache") as tmp_path:
        service = ChatExportService()
        downloaded_image = tmp_path / "downloads" / "same-image.png"
        downloaded_image.parent.mkdir(parents=True, exist_ok=True)
        downloaded_image.write_bytes(b"\x89PNG\r\n\x1a\nsame")

        message_a = NormalizedMessage(
            chat_type="group",
            chat_id="1",
            group_id="1",
            sender_id="100",
            sender_name="A",
            message_id="1",
            message_seq="1",
            timestamp_ms=1770000000000,
            timestamp_iso="2026-02-02T02:02:02+08:00",
            content="[image:same-image.png]",
            text_content="",
            segments=[
                NormalizedSegment(
                    type="image",
                    token="[image:same-image.png]",
                    file_name="same-image.png",
                    path="",
                    md5="abc123",
                    extra={"file_id": "same-file-id"},
                )
            ],
        )
        message_b = message_a.model_copy(
            update={
                "message_id": "2",
                "message_seq": "2",
                "timestamp_ms": 1770000001000,
                "timestamp_iso": "2026-02-02T02:02:03+08:00",
            }
        )

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="1",
            chat_name="demo",
            messages=[message_a, message_b],
            metadata={},
        )

        callback_calls: list[dict[str, object]] = []

        def download_callback(payload: dict[str, object]) -> Path | None:
            callback_calls.append(payload)
            return downloaded_image

        out_path = tmp_path / "exports" / "group_1_cache.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_download_callback=download_callback,
        )

        assert len(callback_calls) == 1
        assert bundle.copied_asset_count == 1
        assert bundle.reused_asset_count == 1


def test_write_bundle_reuses_same_content_across_distinct_source_files() -> None:
    with _repo_temp_dir("media_bundle_same_content_dedupe") as tmp_path:
        service = ChatExportService()
        source_root = tmp_path / "source"
        left = source_root / "left" / "same-a.png"
        right = source_root / "right" / "same-b.jpg"
        payload = b"\x89PNG\r\n\x1a\nsame-content"
        for path in (left, right):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        message_a = NormalizedMessage(
            chat_type="group",
            chat_id="1",
            group_id="1",
            sender_id="100",
            sender_name="A",
            message_id="1",
            message_seq="1",
            timestamp_ms=1770000000000,
            timestamp_iso="2026-02-02T02:02:02+08:00",
            content="[image:same-a.png]",
            text_content="",
            segments=[
                NormalizedSegment(
                    type="image",
                    token="[image:same-a.png]",
                    file_name="same-a.png",
                    path=str(left),
                    md5=None,
                    extra={},
                )
            ],
        )
        message_b = message_a.model_copy(
            update={
                "message_id": "2",
                "message_seq": "2",
                "timestamp_ms": 1770000001000,
                "timestamp_iso": "2026-02-02T02:02:03+08:00",
                "content": "[image:same-b.jpg]",
                "segments": [
                    NormalizedSegment(
                        type="image",
                        token="[image:same-b.jpg]",
                        file_name="same-b.jpg",
                        path=str(right),
                        md5=None,
                        extra={},
                    )
                ],
            }
        )

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="1",
            chat_name="demo",
            messages=[message_a, message_b],
            metadata={},
        )

        out_path = tmp_path / "exports" / "group_1_same_content.jsonl"
        bundle = service.write_bundle(snapshot, out_path, fmt="jsonl")

        image_files = sorted((bundle.assets_dir / "images").iterdir())
        assert bundle.copied_asset_count == 1
        assert bundle.reused_asset_count == 1
        assert len(image_files) == 1
        assert bundle.assets[0].exported_rel_path == bundle.assets[1].exported_rel_path


def test_write_bundle_keeps_distinct_content_variants_with_same_export_name() -> None:
    with _repo_temp_dir("media_bundle_distinct_content_variants") as tmp_path:
        service = ChatExportService()
        source_root = tmp_path / "source"
        left = source_root / "left" / "same-name-a.jpg"
        right = source_root / "right" / "same-name-b.jpg"
        left.parent.mkdir(parents=True, exist_ok=True)
        right.parent.mkdir(parents=True, exist_ok=True)
        left.write_bytes(b"\xff\xd8\xff\xdbfirst-version")
        right.write_bytes(b"\xff\xd8\xff\xdbsecond-version-with-diff")

        message_a = NormalizedMessage(
            chat_type="group",
            chat_id="1",
            group_id="1",
            sender_id="100",
            sender_name="A",
            message_id="1",
            message_seq="1",
            timestamp_ms=1770000000000,
            timestamp_iso="2026-02-02T02:02:02+08:00",
            content="[image:same-name.jpg]",
            text_content="",
            segments=[
                NormalizedSegment(
                    type="image",
                    token="[image:same-name.jpg]",
                    file_name="same-name.jpg",
                    path=str(left),
                    md5=None,
                    extra={},
                )
            ],
        )
        message_b = message_a.model_copy(
            update={
                "message_id": "2",
                "message_seq": "2",
                "timestamp_ms": 1770000001000,
                "timestamp_iso": "2026-02-02T02:02:03+08:00",
                "segments": [
                    NormalizedSegment(
                        type="image",
                        token="[image:same-name.jpg]",
                        file_name="same-name.jpg",
                        path=str(right),
                        md5=None,
                        extra={},
                    )
                ],
            }
        )

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="1",
            chat_name="demo",
            messages=[message_a, message_b],
            metadata={},
        )

        out_path = tmp_path / "exports" / "group_1_distinct_variants.jsonl"
        bundle = service.write_bundle(snapshot, out_path, fmt="jsonl")

        image_files = sorted((bundle.assets_dir / "images").iterdir())
        assert bundle.copied_asset_count == 2
        assert bundle.reused_asset_count == 0
        assert len(image_files) == 2
        assert bundle.assets[0].exported_rel_path != bundle.assets[1].exported_rel_path


def test_write_bundle_keeps_same_size_distinct_content_variants_separate() -> None:
    with _repo_temp_dir("media_bundle_same_size_distinct_variants") as tmp_path:
        service = ChatExportService()
        source_root = tmp_path / "source"
        left = source_root / "left" / "same-name-a.jpg"
        right = source_root / "right" / "same-name-b.jpg"
        left.parent.mkdir(parents=True, exist_ok=True)
        right.parent.mkdir(parents=True, exist_ok=True)
        left.write_bytes(b"\xff\xd8\xff\xdbsame-size-payload-A")
        right.write_bytes(b"\xff\xd8\xff\xdbsame-size-payload-B")
        assert left.stat().st_size == right.stat().st_size

        message_a = NormalizedMessage(
            chat_type="group",
            chat_id="1",
            group_id="1",
            sender_id="100",
            sender_name="A",
            message_id="1",
            message_seq="1",
            timestamp_ms=1770000000000,
            timestamp_iso="2026-02-02T02:02:02+08:00",
            content="[image:same-name.jpg]",
            text_content="",
            segments=[
                NormalizedSegment(
                    type="image",
                    token="[image:same-name.jpg]",
                    file_name="same-name.jpg",
                    path=str(left),
                    md5=None,
                    extra={},
                )
            ],
        )
        message_b = message_a.model_copy(
            update={
                "message_id": "2",
                "message_seq": "2",
                "timestamp_ms": 1770000001000,
                "timestamp_iso": "2026-02-02T02:02:03+08:00",
                "segments": [
                    NormalizedSegment(
                        type="image",
                        token="[image:same-name.jpg]",
                        file_name="same-name.jpg",
                        path=str(right),
                        md5=None,
                        extra={},
                    )
                ],
            }
        )

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="1",
            chat_name="demo",
            messages=[message_a, message_b],
            metadata={},
        )

        out_path = tmp_path / "exports" / "group_1_same_size_distinct_variants.jsonl"
        bundle = service.write_bundle(snapshot, out_path, fmt="jsonl")

        image_files = sorted((bundle.assets_dir / "images").iterdir())
        assert bundle.copied_asset_count == 2
        assert bundle.reused_asset_count == 0
        assert len(image_files) == 2
        assert bundle.assets[0].exported_rel_path != bundle.assets[1].exported_rel_path


def test_write_bundle_keeps_large_sample_collision_variants_separate() -> None:
    with _repo_temp_dir("media_bundle_large_sample_collision_variants") as tmp_path:
        service = ChatExportService()
        source_root = tmp_path / "source"
        left = source_root / "left" / "same-name-a.jpg"
        right = source_root / "right" / "same-name-b.jpg"
        left.parent.mkdir(parents=True, exist_ok=True)
        right.parent.mkdir(parents=True, exist_ok=True)

        size = 400 * 1024
        shared_a = bytearray(b"\x00" * size)
        shared_b = bytearray(b"\x00" * size)
        shared_a[:4] = b"\xff\xd8\xff\xdb"
        shared_b[:4] = b"\xff\xd8\xff\xdb"
        # Keep head/middle/tail windows identical so the fast sampled signature
        # collides; only the exact content check should separate them.
        shared_a[80 * 1024] = ord("A")
        shared_b[80 * 1024] = ord("B")
        left.write_bytes(bytes(shared_a))
        right.write_bytes(bytes(shared_b))
        assert left.stat().st_size == right.stat().st_size

        message_a = NormalizedMessage(
            chat_type="group",
            chat_id="1",
            group_id="1",
            sender_id="100",
            sender_name="A",
            message_id="1",
            message_seq="1",
            timestamp_ms=1770000000000,
            timestamp_iso="2026-02-02T02:02:02+08:00",
            content="[image:same-name.jpg]",
            text_content="",
            segments=[
                NormalizedSegment(
                    type="image",
                    token="[image:same-name.jpg]",
                    file_name="same-name.jpg",
                    path=str(left),
                    md5=None,
                    extra={},
                )
            ],
        )
        message_b = message_a.model_copy(
            update={
                "message_id": "2",
                "message_seq": "2",
                "timestamp_ms": 1770000001000,
                "timestamp_iso": "2026-02-02T02:02:03+08:00",
                "segments": [
                    NormalizedSegment(
                        type="image",
                        token="[image:same-name.jpg]",
                        file_name="same-name.jpg",
                        path=str(right),
                        md5=None,
                        extra={},
                    )
                ],
            }
        )

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="1",
            chat_name="demo",
            messages=[message_a, message_b],
            metadata={},
        )

        out_path = tmp_path / "exports" / "group_1_large_sample_collision_variants.jsonl"
        bundle = service.write_bundle(snapshot, out_path, fmt="jsonl")

        image_files = sorted((bundle.assets_dir / "images").iterdir())
        assert bundle.copied_asset_count == 2
        assert bundle.reused_asset_count == 0
        assert len(image_files) == 2
        assert bundle.assets[0].exported_rel_path != bundle.assets[1].exported_rel_path


def test_write_bundle_skips_legacy_search_context_for_napcat_only(monkeypatch) -> None:
    service = ChatExportService()
    snapshot = NormalizedSnapshot(
        chat_type="group",
        chat_id="922065597",
        chat_name="测试群",
        exported_at=datetime.now(EXPORT_TIMEZONE),
        metadata={},
        messages=[
            NormalizedMessage(
                chat_type="group",
                chat_id="922065597",
                group_id="922065597",
                sender_id="1",
                sender_name="tester",
                message_id="m1",
                message_seq="1",
                timestamp_ms=1773460800000,
                timestamp_iso="2026-03-14T12:00:00+08:00",
                content="[image:test.png]",
                text_content="",
                segments=[
                    NormalizedSegment(
                        type="image",
                        token="[image:test.png]",
                        file_name="test.png",
                        path="",
                        md5=None,
                        extra={},
                    )
                ],
            )
        ],
    )

    class _Manager:
        def prepare_for_export(self, requests, *, progress_callback=None):
            return None

        def resolve_for_export(self, request):
            return None, "qq_expired_after_napcat"

    def _boom(*args, **kwargs):
        raise AssertionError("legacy search context should not be built in napcat_only mode")

    monkeypatch.setattr("qq_data_core.media_bundle._build_media_search_context", _boom)

    with _repo_temp_dir("media_bundle_skip_legacy_context") as tmp_path:
        out_path = tmp_path / "exports" / "skip_legacy_context.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_Manager(),
        )

        assert bundle.missing_asset_count == 1


def test_write_bundle_nested_forward_asset_inherits_parent_context_for_napcat_only() -> None:
    with _repo_temp_dir("media_bundle_forward_parent_context") as tmp_path:
        service = ChatExportService()
        hydrated_image = tmp_path / "downloads" / "nested-forward.png"
        hydrated_image.parent.mkdir(parents=True, exist_ok=True)
        hydrated_image.write_bytes(b"\x89PNG\r\n\x1a\nhydrated")

        snapshot = NormalizedSnapshot(
            chat_type="private",
            chat_id="1507833383",
            chat_name="1507833383",
            messages=[
                NormalizedMessage(
                    chat_type="private",
                    chat_id="1507833383",
                    peer_id="1507833383",
                    sender_id="3956020260",
                    sender_name="wiki",
                    message_id="fwd-parent",
                    message_seq="95",
                    timestamp_ms=1773319027000,
                    timestamp_iso="2026-03-12T20:37:07+08:00",
                    content="[forward message] 甲: [image:nested-forward.png]",
                    text_content="甲: [image:nested-forward.png]",
                    segments=[
                        NormalizedSegment(
                            type="forward",
                            token="[forward message]",
                            summary="聊天记录",
                            extra={
                                "message_id_raw": "raw-parent-msg",
                                "element_id": "forward-element-95",
                                "peer_uid": "u_1507833383",
                                "chat_type_raw": 1,
                                "forward_messages": [
                                    {
                                        "sender_id": "111",
                                        "sender_name": "甲",
                                        "content": "[forward message] [image:nested-forward.png]",
                                        "segments": [
                                            {
                                                "type": "forward",
                                                "extra": {
                                                    "forward_messages": [
                                                        {
                                                            "sender_id": "222",
                                                            "sender_name": "乙",
                                                            "content": "[image:nested-forward.png]",
                                                            "segments": [
                                                                {
                                                                    "type": "image",
                                                                    "file_name": "nested-forward.png",
                                                                    "path": "",
                                                                    "md5": None,
                                                                    "extra": {},
                                                                }
                                                            ],
                                                        }
                                                    ],
                                                },
                                            }
                                        ],
                                    }
                                ],
                            },
                        )
                    ],
                )
            ],
        )

        seen_requests: list[dict[str, object]] = []

        class _Manager:
            def prepare_for_export(self, requests):
                return None

            def resolve_for_export(self, request):
                seen_requests.append(request)
                hint = request.get("download_hint") or {}
                parent = hint.get("_forward_parent") or {}
                if (
                    request.get("asset_type") == "image"
                    and parent.get("message_id_raw") == "raw-parent-msg"
                    and parent.get("element_id") == "forward-element-95"
                    and parent.get("peer_uid") == "u_1507833383"
                    and parent.get("chat_type_raw") == 1
                ):
                    return hydrated_image, "napcat_forward_hydrated"
                return None, None

        out_path = tmp_path / "exports" / "friend_forward_parent_context.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_Manager(),
        )

        assert seen_requests
        assert bundle.copied_asset_count == 1
        assert bundle.missing_asset_count == 0
        assert bundle.assets[0].resolver == "napcat_forward_hydrated"
        assert Path(bundle.assets[0].resolved_source_path).resolve() == hydrated_image.resolve()


def test_write_bundle_records_forensic_incident_for_investigative_missing() -> None:
    with _repo_temp_dir("media_bundle_forensics_incident") as tmp_path:
        service = ChatExportService()
        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="751365230",
            chat_name="史数据统计群",
            messages=[
                NormalizedMessage(
                    chat_type="group",
                    chat_id="751365230",
                    group_id="751365230",
                    sender_id="3226175640",
                    sender_name="Kurnal",
                    message_id="m1",
                    message_seq="18345",
                    timestamp_ms=1772730330000,
                    timestamp_iso="2026-03-06T01:05:30+08:00",
                    content="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    text_content="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    segments=[
                        NormalizedSegment(
                            type="video",
                            token="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                            file_name="dc4fdfa37904fb8e25a551363ab52389.mp4",
                            path=r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\dc4fdfa37904fb8e25a551363ab52389.mp4",
                            extra={},
                        )
                    ],
                )
            ],
        )

        class _MissingManager:
            def prepare_for_export(self, requests, *, progress_callback=None):
                return None

            def resolve_for_export(self, request, *, trace_callback=None):
                return None, "missing_after_napcat"

        collector = ExportForensicsCollector(
            tmp_path / "state",
            chat_type="group",
            chat_id="751365230",
            policy=StrictMissingPolicy(mode="collect"),
            command_context={"entrypoint": "test"},
        )
        collector.capture_preflight({"profile": "test"})

        out_path = tmp_path / "exports" / "group_forensics.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_MissingManager(),
            forensics_collector=collector,
        )
        summary_path = collector.finalize(
            export_completed=True,
            aborted=False,
            data_path=bundle.data_path,
            manifest_path=bundle.manifest_path,
        )
        summary = orjson.loads(summary_path.read_bytes())

        assert bundle.missing_asset_count == 1
        assert bundle.forensic_incident_count == 1
        assert bundle.forensic_run_dir is not None and bundle.forensic_run_dir.exists()
        incident_path = bundle.forensic_run_dir / "incident_001.json"
        assert incident_path.exists()
        incident = orjson.loads(incident_path.read_bytes())
        assert incident["reason_category"] == "hint_path_missing"
        assert incident["asset"]["file_name"] == "dc4fdfa37904fb8e25a551363ab52389.mp4"
        assert incident["route_ledger"] == []
        assert summary["grouped_summary"]["by_reason_category"]["hint_path_missing"]["incident_count"] == 1
        assert summary["grouped_summary"]["by_asset_type"]["video"]["incident_count"] == 1
        assert summary["budget_status"]["incident_budget_exhausted"] is False
        assert summary_path is not None and summary_path.exists()


def test_write_bundle_deduplicates_same_failure_fingerprint_in_forensics() -> None:
    with _repo_temp_dir("media_bundle_forensics_dedupe") as tmp_path:
        service = ChatExportService()
        base_message = NormalizedMessage(
            chat_type="group",
            chat_id="751365230",
            group_id="751365230",
            sender_id="3226175640",
            sender_name="Kurnal",
            message_id="m1",
            message_seq="18345",
            timestamp_ms=1772730330000,
            timestamp_iso="2026-03-06T01:05:30+08:00",
            content="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
            text_content="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
            segments=[
                NormalizedSegment(
                    type="video",
                    token="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    file_name="dc4fdfa37904fb8e25a551363ab52389.mp4",
                    path=r"D:\QQHOT\Tencent Files\2141129832\nt_qq\nt_data\Video\2026-02\Ori\dc4fdfa37904fb8e25a551363ab52389.mp4",
                    extra={},
                )
            ],
        )
        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="751365230",
            chat_name="史数据统计群",
            messages=[
                base_message,
                base_message.model_copy(
                    update={
                        "message_id": "m2",
                        "message_seq": "18395",
                        "timestamp_ms": 1772731717000,
                        "timestamp_iso": "2026-03-06T01:28:37+08:00",
                    }
                ),
            ],
        )

        class _MissingManager:
            def prepare_for_export(self, requests, *, progress_callback=None):
                return None

            def resolve_for_export(self, request, *, trace_callback=None):
                return None, "missing_after_napcat"

        collector = ExportForensicsCollector(
            tmp_path / "state",
            chat_type="group",
            chat_id="751365230",
            policy=StrictMissingPolicy(mode="collect"),
            command_context={"entrypoint": "test"},
        )
        collector.capture_preflight({"profile": "test"})

        out_path = tmp_path / "exports" / "group_forensics_dedupe.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_MissingManager(),
            forensics_collector=collector,
        )
        summary_path = collector.finalize(export_completed=True, aborted=False)
        summary = orjson.loads(summary_path.read_bytes())

        assert bundle.missing_asset_count == 2
        assert bundle.forensic_incident_count == 1
        assert summary["incident_count"] == 1
        occurrences = next(iter(summary["occurrences_by_failure"].values()))
        assert len(occurrences) == 2


def test_write_bundle_forward_video_forensics_captures_pre_post_diff() -> None:
    with _repo_temp_dir("media_bundle_forensics_forward_diff") as tmp_path:
        service = ChatExportService()
        hinted_dir = tmp_path / "QQHOT" / "Tencent Files" / "2141129832" / "nt_qq" / "nt_data" / "Video" / "2026-02" / "Ori"
        hinted_path = hinted_dir / "dc4fdfa37904fb8e25a551363ab52389.mp4"

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="751365230",
            chat_name="史数据统计群",
            messages=[
                NormalizedMessage(
                    chat_type="group",
                    chat_id="751365230",
                    group_id="751365230",
                    sender_id="3226175640",
                    sender_name="Kurnal",
                    message_id="m-forward-1",
                    message_seq="18345",
                    timestamp_ms=1772730330000,
                    timestamp_iso="2026-03-06T01:05:30+08:00",
                    content="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    text_content="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    segments=[
                        NormalizedSegment(
                            type="video",
                            token="[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                            file_name="dc4fdfa37904fb8e25a551363ab52389.mp4",
                            path=str(hinted_path),
                            extra={
                                "_forward_parent": {
                                    "message_id_raw": "forward-parent-msg-1",
                                    "element_id": "forward-parent-el-1",
                                }
                            },
                        )
                    ],
                )
            ],
        )

        class _DiffMissingManager:
            def prepare_for_export(self, requests, *, progress_callback=None):
                return None

            def resolve_for_export(self, request, *, trace_callback=None):
                hinted_dir.mkdir(parents=True, exist_ok=True)
                (hinted_dir / "materialized-after-route.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42new")
                return None, "missing_after_napcat"

        collector = ExportForensicsCollector(
            tmp_path / "state",
            chat_type="group",
            chat_id="751365230",
            policy=StrictMissingPolicy(mode="collect"),
            command_context={"entrypoint": "test"},
        )
        collector.capture_preflight({"profile": "test"})

        progress_events: list[dict[str, object]] = []
        out_path = tmp_path / "exports" / "group_forensics_forward_diff.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_DiffMissingManager(),
            forensics_collector=collector,
            progress_callback=progress_events.append,
        )

        incident_path = bundle.forensic_run_dir / "incident_001.json"
        assert incident_path.exists()
        incident = orjson.loads(incident_path.read_bytes())
        assert incident["pre_path_evidence"] is not None
        assert incident["post_path_evidence"] is not None
        diff_directories = incident["path_evidence_diff"]["directories"]
        assert any(
            "materialized-after-route.mp4" in directory_diff["added"]
            for directory_diff in diff_directories
        )
        forensic_event = next(
            event for event in progress_events if event.get("phase") == "forensic_incident"
        )
        assert forensic_event["incident_id"] == "incident_001"
        assert forensic_event["reason_category"] == "hint_path_missing"


def test_known_expired_missing_does_not_create_forensic_incident() -> None:
    with _repo_temp_dir("media_bundle_forensics_known_expired") as tmp_path:
        service = ChatExportService()
        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="922065597",
            chat_name="蕾米二次元萌萌群",
            messages=[
                NormalizedMessage(
                    chat_type="group",
                    chat_id="922065597",
                    group_id="922065597",
                    sender_id="3956020260",
                    sender_name="wiki",
                    message_id="expired-1",
                    message_seq="100",
                    timestamp_ms=1770000000000,
                    timestamp_iso="2026-02-02T00:00:00+08:00",
                    content="[image:expired-demo.jpg]",
                    text_content="[image:expired-demo.jpg]",
                    segments=[
                        NormalizedSegment(
                            type="image",
                            token="[image:expired-demo.jpg]",
                            file_name="expired-demo.jpg",
                            path=r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Ori\expired-demo.jpg",
                            extra={},
                        )
                    ],
                )
            ],
        )

        class _ExpiredManager:
            def prepare_for_export(self, requests, *, progress_callback=None):
                return None

            def resolve_for_export(self, request, *, trace_callback=None):
                return None, "qq_expired_after_napcat"

        collector = ExportForensicsCollector(
            tmp_path / "state",
            chat_type="group",
            chat_id="922065597",
            policy=StrictMissingPolicy(mode="collect"),
            command_context={"entrypoint": "test"},
        )
        collector.capture_preflight({"profile": "test"})
        out_path = tmp_path / "exports" / "known_expired.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_ExpiredManager(),
            forensics_collector=collector,
        )
        summary_path = collector.finalize(export_completed=True, aborted=False)
        summary = orjson.loads(summary_path.read_bytes())

        assert bundle.missing_asset_count == 1
        assert bundle.forensic_incident_count == 0
        assert summary["incident_count"] == 0


def test_known_expired_missing_with_uuid_name_shape_does_not_create_forensic_incident() -> None:
    with _repo_temp_dir("media_bundle_forensics_known_expired_uuid_shape") as tmp_path:
        service = ChatExportService()
        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="922065597",
            chat_name="蕾米二次元萌萌群",
            messages=[
                NormalizedMessage(
                    chat_type="group",
                    chat_id="922065597",
                    group_id="922065597",
                    sender_id="3956020260",
                    sender_name="wiki",
                    message_id="expired-uuid-1",
                    message_seq="101",
                    timestamp_ms=1770000000000,
                    timestamp_iso="2026-02-02T00:00:00+08:00",
                    content="[image:{E8020E53-9841-49A8-E223-4CC9C6F96219}.jpg]",
                    text_content="[image:{E8020E53-9841-49A8-E223-4CC9C6F96219}.jpg]",
                    segments=[
                        NormalizedSegment(
                            type="image",
                            token="[image:{E8020E53-9841-49A8-E223-4CC9C6F96219}.jpg]",
                            file_name="{E8020E53-9841-49A8-E223-4CC9C6F96219}.jpg",
                            path=r"C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Ori\e8020e53984149a8e2234cc9c6f96219.jpg",
                            extra={},
                        )
                    ],
                )
            ],
        )

        class _ExpiredManager:
            def prepare_for_export(self, requests, *, progress_callback=None):
                return None

            def resolve_for_export(self, request, *, trace_callback=None):
                return None, "qq_expired_after_napcat"

        collector = ExportForensicsCollector(
            tmp_path / "state",
            chat_type="group",
            chat_id="922065597",
            policy=StrictMissingPolicy(mode="collect"),
            command_context={"entrypoint": "test"},
        )
        collector.capture_preflight({"profile": "test"})
        out_path = tmp_path / "exports" / "known_expired_uuid_shape.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            media_download_manager=_ExpiredManager(),
            forensics_collector=collector,
        )
        summary_path = collector.finalize(export_completed=True, aborted=False)
        summary = orjson.loads(summary_path.read_bytes())

        assert bundle.missing_asset_count == 1
        assert bundle.forensic_incident_count == 0
        assert summary["incident_count"] == 0


def test_write_bundle_forward_video_uses_local_path_hidden_in_hint_url() -> None:
    with _repo_temp_dir("media_bundle_forward_video_hint_url") as tmp_path:
        service = ChatExportService()
        hidden_video = tmp_path / "Video" / "2026-02" / "Ori" / "dc4fdfa37904fb8e25a551363ab52389.mp4"
        hidden_video.parent.mkdir(parents=True, exist_ok=True)
        hidden_video.write_bytes(b"\x00\x00\x00\x18ftypmp42forward-video")

        snapshot = NormalizedSnapshot(
            chat_type="group",
            chat_id="751365230",
            chat_name="史数据统计群",
            messages=[
                NormalizedMessage(
                    chat_type="group",
                    chat_id="751365230",
                    group_id="751365230",
                    sender_id="3226175640",
                    sender_name="Kurnal",
                    message_id="fwd-video-parent",
                    message_seq="18345",
                    timestamp_ms=1772730330000,
                    timestamp_iso="2026-03-06T01:05:30+08:00",
                    content="[forward message] 猪头: [video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    text_content="猪头: [video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                    segments=[
                        NormalizedSegment(
                            type="forward",
                            token="[forward message]",
                            summary="聊天记录",
                            extra={
                                "message_id_raw": "raw-parent-msg",
                                "element_id": "forward-element-18345",
                                "peer_uid": "751365230",
                                "chat_type_raw": 2,
                                "forward_messages": [
                                    {
                                        "sender_id": "1094950020",
                                        "sender_name": "猪头",
                                        "content": "[video:dc4fdfa37904fb8e25a551363ab52389.mp4]",
                                        "segments": [
                                            {
                                                "type": "video",
                                                "file_name": "dc4fdfa37904fb8e25a551363ab52389.mp4",
                                                "path": "",
                                                "md5": None,
                                                "extra": {
                                                    "url": str(hidden_video.resolve()),
                                                    "message_id_raw": "raw-parent-msg",
                                                    "element_id": "forward-element-18345",
                                                    "peer_uid": "751365230",
                                                    "chat_type_raw": 2,
                                                },
                                            }
                                        ],
                                    }
                                ],
                            },
                        )
                    ],
                )
            ],
        )

        out_path = tmp_path / "exports" / "group_forward_video_hint_url.jsonl"
        bundle = service.write_bundle(
            snapshot,
            out_path,
            fmt="jsonl",
            media_resolution_mode="napcat_only",
        )

        assert bundle.copied_asset_count == 1
        assert bundle.missing_asset_count == 0
        assert bundle.assets[0].resolver == "direct_local_path"
        assert Path(bundle.assets[0].resolved_source_path).resolve() == hidden_video.resolve()
