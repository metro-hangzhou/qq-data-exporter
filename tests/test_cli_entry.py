from __future__ import annotations

import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import uuid

from typer.testing import CliRunner

import qq_data_cli.app as cli_app
from qq_data_core.models import MaterializedAsset
from qq_data_integrations.napcat import NapCatTargetLookupError


def test_no_args_enters_repl(monkeypatch) -> None:
    state: dict[str, bool] = {"ran": False}
    previous_ui = os.environ.get("CLI_UI_MODE")

    class FakeRepl:
        def __init__(self) -> None:
            state["ui"] = os.environ.get("CLI_UI_MODE")

        def run(self) -> None:
            state["ran"] = True

    monkeypatch.setattr(cli_app, "SlashRepl", FakeRepl)
    try:
        result = CliRunner().invoke(cli_app.app, ["--ui", "compat"])
        assert result.exit_code == 0
        assert state["ran"] is True
        assert state["ui"] == "compat"
    finally:
        if previous_ui is None:
            os.environ.pop("CLI_UI_MODE", None)
        else:
            os.environ["CLI_UI_MODE"] = previous_ui


def test_shell_command_enters_repl(monkeypatch) -> None:
    state: dict[str, int] = {"count": 0}

    class FakeRepl:
        def run(self) -> None:
            state["count"] += 1

    monkeypatch.setattr(cli_app, "SlashRepl", FakeRepl)
    result = CliRunner().invoke(cli_app.app, ["shell"])
    assert result.exit_code == 0
    assert state["count"] == 1


def test_export_history_uses_tail_fetch_for_large_limit(monkeypatch) -> None:
    calls: list[tuple[str, object, object]] = []
    callback_presence: dict[str, bool] = {"tail": False, "bundle": False}
    discover_calls: dict[str, int] = {"count": 0}
    tmp_path = Path("state") / "test_tmp" / f"cli_entry_{uuid.uuid4().hex}"
    tmp_path.mkdir(parents=True, exist_ok=True)

    try:
        class FakeSettings:
            def __init__(self) -> None:
                self.http_url = "http://127.0.0.1:3000"
                self.access_token = None
                self.export_dir = tmp_path
                self.state_dir = tmp_path
                self.project_root = tmp_path
                self.use_system_proxy = False
                self.fast_history_mode = "auto"
                self.fast_history_url = None
                self.webui_url = None
                self.webui_token = None

            def model_copy(self, update):
                copied = FakeSettings()
                for key, value in update.items():
                    setattr(copied, key, value)
                return copied

        class FakeBootstrapper:
            def __init__(self, settings) -> None:
                self.settings = settings

            def ensure_endpoint(self, _endpoint: str):
                return SimpleNamespace(
                    ready=True,
                    message="",
                    attempted_start=False,
                    attempted_configure=False,
                )

        class FakeGateway:
            def __init__(self, _settings) -> None:
                pass

            def fetch_snapshot(self, request):
                calls.append(("fetch_snapshot", request.limit, None))
                return SimpleNamespace(messages=[], metadata={}, chat_type="group", chat_id="922065597", chat_name="922065597")

            def fetch_snapshot_tail(self, request, *, data_count, page_size, progress_callback=None):
                calls.append(("fetch_snapshot_tail", data_count, page_size))
                callback_presence["tail"] = progress_callback is not None
                return SimpleNamespace(messages=[], metadata={"requested_data_count": data_count}, chat_type="group", chat_id="922065597", chat_name="922065597")

            def build_media_download_manager(self):
                return None

            def close(self) -> None:
                calls.append(("close", None, None))

        class FakeService:
            def build_snapshot(self, source_snapshot, *, include_raw=False):
                return source_snapshot

            def write_bundle(self, snapshot, output_path, **kwargs):
                callback_presence["bundle"] = kwargs.get("progress_callback") is not None
                return SimpleNamespace(
                    data_path=output_path,
                    manifest_path=output_path.with_suffix(".manifest.json"),
                    copied_asset_count=0,
                    reused_asset_count=0,
                    missing_asset_count=1,
                    assets=[
                        MaterializedAsset(
                            sender_id="1",
                            timestamp_iso="2026-03-14T00:00:00+08:00",
                            asset_type="image",
                            status="missing",
                            resolver="qq_expired_after_napcat",
                            missing_kind="qq_expired_after_napcat",
                        )
                    ],
                )

        class FakeTrace:
            def __init__(self, *_args, **_kwargs) -> None:
                self.path = tmp_path / "trace.jsonl"

            def write_event(self, *_args, **_kwargs) -> None:
                return None

            def build_summary(self, *, record_count=None):
                return {
                    "elapsed_s": 1.234,
                    "pages_scanned": 4,
                    "retry_events": 0,
                    "record_count": record_count or 0,
                }

            def close(self) -> None:
                return None

        monkeypatch.setattr(cli_app.NapCatSettings, "from_env", staticmethod(lambda: FakeSettings()))
        monkeypatch.setattr(cli_app, "NapCatBootstrapper", FakeBootstrapper)
        monkeypatch.setattr(cli_app, "NapCatGateway", FakeGateway)
        monkeypatch.setattr(cli_app, "ChatExportService", FakeService)
        monkeypatch.setattr(cli_app, "ExportPerfTraceWriter", FakeTrace)
        monkeypatch.setattr(cli_app, "collect_debug_preflight_evidence", lambda settings: {})
        monkeypatch.setattr(
            cli_app,
            "discover_qq_media_roots",
            lambda: discover_calls.__setitem__("count", discover_calls["count"] + 1),
        )
        monkeypatch.setattr(
            cli_app,
            "_resolve_target",
            lambda gateway, chat_type, query, chat_name=None, refresh=False: SimpleNamespace(
                chat_type=chat_type,
                chat_id="922065597",
                display_name="922065597",
            ),
        )

        out_path = tmp_path / "out.txt"
        result = CliRunner().invoke(
            cli_app.app,
            [
                "export-history",
                "group",
                "922065597",
                "--limit",
                "2000",
                "--format",
                "txt",
                "--out",
                str(out_path),
            ],
        )

        assert result.exit_code == 0
        assert ("fetch_snapshot_tail", 2000, 500) in calls
        assert not any(call[0] == "fetch_snapshot" for call in calls)
        assert callback_presence == {"tail": True, "bundle": True}
        assert discover_calls["count"] == 0
        assert "trace=" in result.output
        assert "missing_kinds=[qq_expired_after_napcat:1]" in result.output
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_format_cli_export_progress_uses_bulk_label_for_bulk_tail_scan() -> None:
    text = cli_app._format_cli_export_progress(
        {
            "phase": "tail_scan",
            "pages_scanned": 11,
            "matched_messages": 2000,
            "requested_data_count": 2000,
            "page_size": 200,
            "page_duration_s": 4.25,
            "history_source": "napcat_fast_history_bulk",
        }
    )

    assert text is not None
    assert "bulk=4.25s" in text
    assert "page=4.25s" not in text


def test_format_cli_export_progress_reports_asset_substep_timeout() -> None:
    text = cli_app._format_cli_export_progress(
        {
            "phase": "materialize_asset_substep",
            "stage": "done",
            "status": "timeout",
            "substep": "forward_context_materialize",
            "asset_type": "video",
            "file_name": "demo-video.mp4",
            "timeout_s": 25.0,
            "elapsed_s": 25.0,
        }
    )

    assert text is not None
    assert "asset substep timeout" in text
    assert "forward_context_materialize" in text
    assert "video:demo-video.mp4" in text


def test_format_cli_export_progress_reports_forensic_incident() -> None:
    text = cli_app._format_cli_export_progress(
        {
            "phase": "forensic_incident",
            "stage": "recorded",
            "incident_id": "incident_001",
            "reason_category": "hint_path_missing",
            "asset_type": "video",
            "file_name": "demo-video.mp4",
            "is_new_incident": True,
            "incident_path": r"D:\state\export_forensics\run\incident_001.json",
        }
    )

    assert text is not None
    assert "export_incident:" in text
    assert "incident_001" in text
    assert "hint_path_missing" in text
    assert "video:demo-video.mp4" in text


def test_cli_resolve_target_prefers_metadata_for_numeric_id() -> None:
    class FakeGateway:
        def resolve_target(self, chat_type: str, query: str, *, refresh_if_missing: bool):
            assert chat_type == "private"
            assert query == "1507833383"
            assert refresh_if_missing is True
            return SimpleNamespace(chat_type="private", chat_id="1507833383", name="真实昵称", display_name="真实昵称")

    target = cli_app._resolve_target(
        FakeGateway(),
        "private",
        "1507833383",
        chat_name=None,
        refresh=False,
    )

    assert target.name == "真实昵称"


def test_cli_resolve_target_falls_back_for_unknown_numeric_id() -> None:
    class FakeGateway:
        def resolve_target(self, chat_type: str, query: str, *, refresh_if_missing: bool):
            raise NapCatTargetLookupError("not found")

    target = cli_app._resolve_target(
        FakeGateway(),
        "private",
        "1507833383",
        chat_name="空白好友",
        refresh=False,
    )

    assert target.chat_id == "1507833383"
    assert target.name == "空白好友"


def test_terminal_doctor_command_prints_probe_summary(monkeypatch) -> None:
    monkeypatch.setattr(cli_app, "_init_cli_logging", lambda: None)
    monkeypatch.setattr(cli_app, "probe_terminal_environment", lambda: "probe")
    monkeypatch.setattr(cli_app, "read_requested_cli_ui_mode", lambda: "auto")
    monkeypatch.setattr(
        cli_app,
        "resolve_cli_ui_mode",
        lambda probe, requested_mode="auto": SimpleNamespace(requested_mode="auto", resolved_mode="compat", reason="classic_windows_console"),
    )
    monkeypatch.setattr(
        cli_app,
        "render_terminal_doctor_lines",
        lambda probe, decision: ["terminal_host=classic_console", "recommended_ui_mode=compat"],
    )

    result = CliRunner().invoke(cli_app.app, ["terminal-doctor"])

    assert result.exit_code == 0
    assert "terminal_host=classic_console" in result.output
    assert "recommended_ui_mode=compat" in result.output


def test_invalid_ui_override_returns_parameter_error(monkeypatch) -> None:
    monkeypatch.setattr(cli_app, "_init_cli_logging", lambda: None)

    result = CliRunner().invoke(cli_app.app, ["--ui", "broken", "terminal-doctor"])

    assert result.exit_code != 0
    assert "ui mode must be one of" in result.output
