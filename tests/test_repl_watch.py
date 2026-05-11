from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from qq_data_cli import repl as repl_module
from qq_data_cli.logging_utils import reset_cli_logging_for_tests
from qq_data_integrations.napcat import ChatTarget, NapCatSettings, NapCatTargetLookupError


def test_handle_watch_logs_and_returns_on_exception(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_watch").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []

    class FakeView:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def run(self) -> None:
            raise RuntimeError("boom")

    repl = repl_module.SlashRepl()
    monkeypatch.setattr(repl_module, "WatchConversationView", FakeView)
    monkeypatch.setattr(repl, "_ensure_endpoint_ready", lambda endpoint: None)
    monkeypatch.setattr(repl, "_prime_target_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        repl,
        "_resolve_target",
        lambda *args, **kwargs: ChatTarget(chat_type="group", chat_id="100", name="TestGroup"),
    )
    monkeypatch.setattr(repl, "_require_gateway", lambda: object())
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    repl._handle_watch(["group", "TestGroup"])

    assert any("监视窗口意外关闭" in line for line in outputs)
    latest_log = settings.state_dir / "logs" / "cli_latest.log"
    assert latest_log.exists()
    assert "watch_crashed" in latest_log.read_text(encoding="utf-8")


def test_repl_handles_unclosed_quote_without_crashing(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_watch_quotes").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    result = repl._handle_input('/watch friend "abc')

    assert result is False
    assert any("引号" in line for line in outputs)


def test_repl_plain_text_input_gets_friendly_hint(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_plain_text").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    result = repl._handle_input("hello")

    assert result is False
    assert outputs == ["请输入以 / 开头的命令；可输入 /help 查看示例。"]


def test_repl_unknown_command_gets_recovery_hint(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_unknown_command").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    result = repl._handle_input("/watc")

    assert result is False
    assert outputs == ["未识别的命令：/watc。可输入 /help 查看可用命令。"]


def test_repl_usage_value_error_gets_friendly_message(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_usage_error").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(repl, "_handle_status", lambda: (_ for _ in ()).throw(ValueError("Usage: /status")))
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    result = repl._handle_input("/status")

    assert result is False
    assert outputs == ["命令参数不完整：/status。可输入 /help 查看示例。"]


def test_repl_help_includes_examples_and_default_format_hint(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_help_text").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    result = repl._handle_input("/help")

    assert result is False
    assert len(outputs) == 1
    assert "默认导出 jsonl" in outputs[0]
    assert "/watch friend 1507833383" in outputs[0]
    assert "名称里有空格时，请用引号包起来" in outputs[0]
    assert "--ui compat" in outputs[0]


def test_repl_numeric_target_prefers_metadata_when_available(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_numeric_target").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    class FakeGateway:
        def resolve_target(self, chat_type: str, query: str, *, refresh_if_missing: bool):
            assert chat_type == "private"
            assert query == "1507833383"
            assert refresh_if_missing is True
            return ChatTarget(chat_type="private", chat_id="1507833383", name="真实昵称", remark="真实备注")

    repl = repl_module.SlashRepl()
    monkeypatch.setattr(repl, "_require_gateway", lambda: FakeGateway())

    target = repl._resolve_target("private", "1507833383", refresh=False)

    assert target.name == "真实昵称"
    assert target.remark == "真实备注"


def test_repl_numeric_target_falls_back_when_metadata_missing(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_numeric_fallback").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    class FakeGateway:
        def resolve_target(self, chat_type: str, query: str, *, refresh_if_missing: bool):
            raise NapCatTargetLookupError("not found")

    repl = repl_module.SlashRepl()
    monkeypatch.setattr(repl, "_require_gateway", lambda: FakeGateway())

    target = repl._resolve_target("private", "1507833383", refresh=False)

    assert target.chat_id == "1507833383"
    assert target.name == "1507833383"


def test_batch_export_error_includes_chat_id_and_log_path(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_batch_export_error").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )
    monkeypatch.setattr(
        repl,
        "_resolve_target",
        lambda chat_type, query, refresh=False: ChatTarget(chat_type=chat_type, chat_id="1507833383", name="真实昵称"),
    )
    monkeypatch.setattr(
        repl,
        "_run_single_export",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    parsed = SimpleNamespace(
        out_path=None,
        batch_target_queries=["1507833383"],
        refresh=False,
    )

    repl._handle_batch_export(parsed, chat_type="private")

    assert any("批量导出失败" in line for line in outputs)
    assert any("chat_id=1507833383" in line for line in outputs)
    assert any("日志：" in line for line in outputs)


def test_repl_terminal_doctor_prints_probe_lines(monkeypatch) -> None:
    reset_cli_logging_for_tests()
    tmp_path = Path(".tmp_test_repl_terminal_doctor").resolve()
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    settings = NapCatSettings(
        project_root=tmp_path,
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "exports",
    )
    monkeypatch.setattr(
        repl_module.NapCatSettings,
        "from_env",
        classmethod(lambda cls: settings),
    )
    monkeypatch.setattr(repl_module, "discover_qq_media_roots", lambda: [])
    monkeypatch.setattr(repl_module, "probe_terminal_environment", lambda: "probe")
    monkeypatch.setattr(repl_module, "read_requested_cli_ui_mode", lambda: "auto")
    monkeypatch.setattr(
        repl_module,
        "resolve_cli_ui_mode",
        lambda probe, requested_mode="auto": SimpleNamespace(requested_mode="auto", resolved_mode="compat", reason="classic_windows_console"),
    )
    monkeypatch.setattr(
        repl_module,
        "render_terminal_doctor_lines",
        lambda probe, decision: ["terminal_host=classic_console", "recommended_ui_mode=compat"],
    )

    outputs: list[str] = []
    repl = repl_module.SlashRepl()
    monkeypatch.setattr(
        repl._console,
        "print",
        lambda *args, **kwargs: outputs.append(" ".join(str(arg) for arg in args)),
    )

    result = repl._handle_input("/terminal-doctor")

    assert result is False
    assert outputs == ["terminal_host=classic_console\nrecommended_ui_mode=compat"]
