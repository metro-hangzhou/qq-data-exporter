from __future__ import annotations

from types import SimpleNamespace

import qq_data_cli.terminal_compat as terminal_module
from qq_data_cli.terminal_compat import (
    apply_cli_ui_mode_override,
    build_cli_ui_profile,
    TerminalProbe,
    probe_terminal_environment,
    render_terminal_doctor_lines,
    resolve_cli_ui_mode,
)


def test_resolve_cli_ui_mode_prefers_compat_for_classic_windows_console() -> None:
    probe = TerminalProbe(
        platform_system="Windows",
        windows_release="10",
        windows_version="10.0.19045",
        windows_build=19045,
        terminal_host="classic_console",
        shell_name="powershell",
        stdin_tty=True,
        stdout_tty=True,
        columns=120,
        lines=30,
        stdout_encoding="utf-8",
        preferred_encoding="cp936",
        term=None,
        term_program=None,
        wt_session=False,
        vscode_terminal=False,
        conemu_session=False,
        ansicon_present=False,
        virtual_terminal_enabled=False,
        stdout_console_mode=0,
    )

    decision = resolve_cli_ui_mode(probe, requested_mode="auto")

    assert decision.resolved_mode == "compat"
    assert decision.reason == "classic_windows_console"


def test_render_terminal_doctor_lines_includes_ui_decision() -> None:
    probe = TerminalProbe(
        platform_system="Windows",
        windows_release="11",
        windows_version="10.0.26100",
        windows_build=26100,
        terminal_host="windows_terminal",
        shell_name="powershell",
        stdin_tty=True,
        stdout_tty=True,
        columns=140,
        lines=40,
        stdout_encoding="utf-8",
        preferred_encoding="utf-8",
        term="xterm-256color",
        term_program="vscode",
        wt_session=True,
        vscode_terminal=True,
        conemu_session=False,
        ansicon_present=False,
        virtual_terminal_enabled=True,
        stdout_console_mode=7,
    )
    decision = resolve_cli_ui_mode(probe, requested_mode="auto")

    lines = render_terminal_doctor_lines(probe, decision)

    assert any(line == "terminal_host=windows_terminal" for line in lines)
    assert any(line == "recommended_ui_mode=full" for line in lines)
    assert any(line.startswith("ui_mode_reason=") for line in lines)
    assert any(line == "ui_override_flag=--ui auto|full|compat" for line in lines)
    assert any(line == "ui_override_env=CLI_UI_MODE" for line in lines)


def test_probe_terminal_environment_uses_env_hints(monkeypatch) -> None:
    monkeypatch.setattr(terminal_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(terminal_module.platform, "release", lambda: "11")
    monkeypatch.setattr(terminal_module.platform, "version", lambda: "10.0.26100")
    monkeypatch.setattr(terminal_module.shutil, "get_terminal_size", lambda fallback=(0, 0): SimpleNamespace(columns=132, lines=44))
    monkeypatch.setattr(terminal_module, "_get_windows_stdout_console_mode", lambda: 0x0004)

    fake_stdin = SimpleNamespace(isatty=lambda: True)
    fake_stdout = SimpleNamespace(isatty=lambda: True, encoding="utf-8")
    probe = probe_terminal_environment(
        env={
            "WT_SESSION": "1",
            "TERM_PROGRAM": "vscode",
            "PSModulePath": "x",
            "TERM": "xterm-256color",
        },
        stdin=fake_stdin,
        stdout=fake_stdout,
    )

    assert probe.platform_system == "Windows"
    assert probe.windows_build == 26100
    assert probe.terminal_host == "windows_terminal"
    assert probe.wt_session is True
    assert probe.virtual_terminal_enabled is True
    assert probe.columns == 132


def test_apply_cli_ui_mode_override_updates_env() -> None:
    env: dict[str, str] = {}

    normalized = apply_cli_ui_mode_override("compat", env=env)

    assert normalized == "compat"
    assert env["CLI_UI_MODE"] == "compat"


def test_build_cli_ui_profile_disables_risky_features_for_compat() -> None:
    probe = TerminalProbe(
        platform_system="Windows",
        windows_release="10",
        windows_version="10.0.19045",
        windows_build=19045,
        terminal_host="classic_console",
        shell_name="powershell",
        stdin_tty=True,
        stdout_tty=True,
        columns=120,
        lines=30,
        stdout_encoding="utf-8",
        preferred_encoding="cp936",
        term=None,
        term_program=None,
        wt_session=False,
        vscode_terminal=False,
        conemu_session=False,
        ansicon_present=False,
        virtual_terminal_enabled=False,
        stdout_console_mode=0,
    )

    decision = resolve_cli_ui_mode(probe, requested_mode="auto")
    profile = build_cli_ui_profile(decision)

    assert profile.mode == "compat"
    assert profile.show_completion_menu is False
    assert profile.complete_while_typing is False
    assert profile.watch_full_screen is False
    assert profile.use_custom_scrollbar is False
    assert profile.use_highlight_style is False
