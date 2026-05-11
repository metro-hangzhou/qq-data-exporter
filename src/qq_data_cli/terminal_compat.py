from __future__ import annotations

import ctypes
import locale
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


CliUiMode = Literal["auto", "full", "compat"]
ResolvedCliUiMode = Literal["full", "compat"]

_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_STD_OUTPUT_HANDLE = -11


@dataclass(frozen=True, slots=True)
class TerminalProbe:
    platform_system: str
    windows_release: str | None
    windows_version: str | None
    windows_build: int | None
    terminal_host: str
    shell_name: str | None
    stdin_tty: bool
    stdout_tty: bool
    columns: int
    lines: int
    stdout_encoding: str | None
    preferred_encoding: str | None
    term: str | None
    term_program: str | None
    wt_session: bool
    vscode_terminal: bool
    conemu_session: bool
    ansicon_present: bool
    virtual_terminal_enabled: bool | None
    stdout_console_mode: int | None


@dataclass(frozen=True, slots=True)
class TerminalUiDecision:
    requested_mode: CliUiMode
    resolved_mode: ResolvedCliUiMode
    reason: str


@dataclass(frozen=True, slots=True)
class CliUiProfile:
    mode: ResolvedCliUiMode
    show_completion_menu: bool
    complete_while_typing: bool
    watch_full_screen: bool
    use_custom_scrollbar: bool
    use_highlight_style: bool


def probe_terminal_environment(
    *,
    env: dict[str, str] | None = None,
    stdin=None,
    stdout=None,
) -> TerminalProbe:
    environment = env or dict(os.environ)
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout

    system = platform.system()
    is_windows = system == "Windows"
    windows_release = platform.release() if is_windows else None
    windows_version = platform.version() if is_windows else None
    windows_build = _parse_windows_build(windows_version) if is_windows else None
    size = shutil.get_terminal_size(fallback=(0, 0))
    stdout_mode = _get_windows_stdout_console_mode() if is_windows else None
    vt_enabled = (
        bool(stdout_mode & _ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        if stdout_mode is not None
        else None
    )

    wt_session = bool(environment.get("WT_SESSION"))
    term_program = environment.get("TERM_PROGRAM") or None
    vscode_terminal = term_program == "vscode"
    conemu_session = bool(environment.get("ConEmuPID"))
    ansicon_present = bool(environment.get("ANSICON"))

    return TerminalProbe(
        platform_system=system,
        windows_release=windows_release,
        windows_version=windows_version,
        windows_build=windows_build,
        terminal_host=_detect_terminal_host(
            is_windows=is_windows,
            wt_session=wt_session,
            vscode_terminal=vscode_terminal,
            conemu_session=conemu_session,
            term_program=term_program,
            stdout_tty=bool(getattr(output_stream, "isatty", lambda: False)()),
        ),
        shell_name=_detect_shell_name(environment),
        stdin_tty=bool(getattr(input_stream, "isatty", lambda: False)()),
        stdout_tty=bool(getattr(output_stream, "isatty", lambda: False)()),
        columns=max(0, int(size.columns)),
        lines=max(0, int(size.lines)),
        stdout_encoding=getattr(output_stream, "encoding", None) or None,
        preferred_encoding=locale.getpreferredencoding(False) or None,
        term=environment.get("TERM") or None,
        term_program=term_program,
        wt_session=wt_session,
        vscode_terminal=vscode_terminal,
        conemu_session=conemu_session,
        ansicon_present=ansicon_present,
        virtual_terminal_enabled=vt_enabled,
        stdout_console_mode=stdout_mode,
    )


def resolve_cli_ui_mode(
    probe: TerminalProbe,
    *,
    requested_mode: CliUiMode = "auto",
) -> TerminalUiDecision:
    if requested_mode == "full":
        return TerminalUiDecision(
            requested_mode=requested_mode,
            resolved_mode="full",
            reason="manual_override_full",
        )
    if requested_mode == "compat":
        return TerminalUiDecision(
            requested_mode=requested_mode,
            resolved_mode="compat",
            reason="manual_override_compat",
        )

    if not probe.stdin_tty or not probe.stdout_tty:
        return TerminalUiDecision(
            requested_mode=requested_mode,
            resolved_mode="compat",
            reason="non_tty_terminal",
        )
    if probe.platform_system == "Windows" and probe.terminal_host == "classic_console":
        return TerminalUiDecision(
            requested_mode=requested_mode,
            resolved_mode="compat",
            reason="classic_windows_console",
        )
    if probe.platform_system == "Windows" and probe.virtual_terminal_enabled is False:
        return TerminalUiDecision(
            requested_mode=requested_mode,
            resolved_mode="compat",
            reason="vt_processing_disabled",
        )
    return TerminalUiDecision(
        requested_mode=requested_mode,
        resolved_mode="full",
        reason="modern_terminal_detected",
    )


def normalize_requested_cli_ui_mode(value: str | None) -> CliUiMode | None:
    raw_value = str(value or "").strip().casefold()
    if raw_value in {"full", "compat", "auto"}:
        return raw_value  # type: ignore[return-value]
    return None


def read_requested_cli_ui_mode(*, env: dict[str, str] | None = None) -> CliUiMode:
    environment = env or dict(os.environ)
    return normalize_requested_cli_ui_mode(environment.get("CLI_UI_MODE")) or "auto"


def apply_cli_ui_mode_override(
    value: str | None,
    *,
    env: dict[str, str] | None = None,
) -> CliUiMode | None:
    environment = env if env is not None else os.environ
    normalized = normalize_requested_cli_ui_mode(value)
    if normalized is None:
        if value is None:
            return None
        raise ValueError("ui mode must be one of: auto, full, compat")
    environment["CLI_UI_MODE"] = normalized
    return normalized


def build_cli_ui_profile(decision: TerminalUiDecision) -> CliUiProfile:
    if decision.resolved_mode == "compat":
        return CliUiProfile(
            mode="compat",
            show_completion_menu=True,
            complete_while_typing=True,
            watch_full_screen=False,
            use_custom_scrollbar=False,
            use_highlight_style=False,
        )
    return CliUiProfile(
        mode="full",
        show_completion_menu=True,
        complete_while_typing=True,
        watch_full_screen=True,
        use_custom_scrollbar=True,
        use_highlight_style=True,
    )


def render_cli_ui_mode_notice(decision: TerminalUiDecision) -> str | None:
    if decision.resolved_mode != "compat":
        return None
    reason_map = {
        "manual_override_compat": "已按设置启用兼容显示模式。",
        "non_tty_terminal": "当前终端不支持完整交互，已切换为兼容显示模式。",
        "classic_windows_console": "检测到经典 Windows 控制台，已切换为兼容显示模式。",
        "vt_processing_disabled": "当前终端 ANSI/VT 能力有限，已切换为兼容显示模式。",
    }
    return reason_map.get(decision.reason, "已切换为兼容显示模式，以提高稳定性。")


def render_terminal_doctor_lines(
    probe: TerminalProbe,
    decision: TerminalUiDecision,
) -> list[str]:
    lines = [
        f"platform={probe.platform_system}",
        f"terminal_host={probe.terminal_host}",
        f"shell={probe.shell_name or ''}",
        f"stdin_tty={probe.stdin_tty}",
        f"stdout_tty={probe.stdout_tty}",
        f"terminal_size={probe.columns}x{probe.lines}",
        f"stdout_encoding={probe.stdout_encoding or ''}",
        f"preferred_encoding={probe.preferred_encoding or ''}",
        f"term={probe.term or ''}",
        f"term_program={probe.term_program or ''}",
        f"windows_terminal={probe.wt_session}",
        f"vscode_terminal={probe.vscode_terminal}",
        f"conemu_session={probe.conemu_session}",
        f"ansicon_present={probe.ansicon_present}",
        f"virtual_terminal_enabled={probe.virtual_terminal_enabled}",
        f"stdout_console_mode={probe.stdout_console_mode if probe.stdout_console_mode is not None else ''}",
        f"requested_ui_mode={decision.requested_mode}",
        f"recommended_ui_mode={decision.resolved_mode}",
        f"ui_mode_reason={decision.reason}",
        "ui_override_flag=--ui auto|full|compat",
        "ui_override_env=CLI_UI_MODE",
    ]
    if probe.platform_system == "Windows":
        lines.insert(1, f"windows_release={probe.windows_release or ''}")
        lines.insert(2, f"windows_version={probe.windows_version or ''}")
        lines.insert(3, f"windows_build={probe.windows_build if probe.windows_build is not None else ''}")
    return lines


def _parse_windows_build(version: str | None) -> int | None:
    if not version:
        return None
    try:
        return int(version.split(".")[-1])
    except (TypeError, ValueError):
        return None


def _detect_shell_name(env: dict[str, str]) -> str | None:
    if env.get("PSModulePath"):
        return "powershell"
    if env.get("COMSPEC"):
        return Path(env["COMSPEC"]).stem
    return None


def _detect_terminal_host(
    *,
    is_windows: bool,
    wt_session: bool,
    vscode_terminal: bool,
    conemu_session: bool,
    term_program: str | None,
    stdout_tty: bool,
) -> str:
    if wt_session:
        return "windows_terminal"
    if vscode_terminal:
        return "vscode_terminal"
    if conemu_session:
        return "conemu"
    if term_program:
        return term_program
    if is_windows and stdout_tty:
        return "classic_console"
    return "unknown"


def _get_windows_stdout_console_mode() -> int | None:
    if platform.system() != "Windows":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        if handle in {0, -1}:
            return None
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            return None
        return int(mode.value)
    except Exception:
        return None
