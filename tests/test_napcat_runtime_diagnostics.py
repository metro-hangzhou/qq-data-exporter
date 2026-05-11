from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

from qq_data_integrations.napcat.runtime import (
    _append_napcat_log_hint,
    _prepare_napcat_launch_artifacts,
    _read_napcat_effective_launcher,
)


def _fresh_test_dir(name: str) -> Path:
    base = Path.cwd() / ".tmp" / "test_napcat_runtime_diagnostics" / name
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    return base


def test_append_napcat_log_hint_surfaces_admin_mode_requirement() -> None:
    temp_dir = _fresh_test_dir("admin_mode")
    log_path = temp_dir / "napcat.log"
    log_path.write_text("Please run this script in administrator mode.\n", encoding="utf-8")

    message = _append_napcat_log_hint("start failed", log_path)

    assert "administrator mode is required" in message
    assert str(log_path) in message


def test_append_napcat_log_hint_surfaces_missing_runtime_module() -> None:
    temp_dir = _fresh_test_dir("missing_module")
    log_path = temp_dir / "napcat.log"
    log_path.write_text("Error: Cannot find module 'path-to-regexp'\n", encoding="utf-8")

    message = _append_napcat_log_hint("start failed", log_path)

    assert "missing a required local module/dependency" in message


def test_read_napcat_effective_launcher_from_wrapper_log() -> None:
    temp_dir = _fresh_test_dir("effective_launcher")
    log_path = temp_dir / "napcat.log"
    expected = temp_dir / "start_napcat_logged.bat"
    log_path.write_text(
        f"wrapper_effective_launcher={expected}\n",
        encoding="utf-8",
    )

    resolved = _read_napcat_effective_launcher(log_path)

    assert resolved is not None
    assert resolved.name == "start_napcat_logged.bat"


def test_prepare_napcat_launch_artifacts_passes_quick_login_uin_to_launcher() -> None:
    temp_path = _fresh_test_dir("quick_login_uin")
    launcher = temp_path / "launcher-win10.bat"
    launcher.write_text("@echo off\n", encoding="utf-8")

    _log_path, wrapper_path, _latest_path = _prepare_napcat_launch_artifacts(
        launcher,
        temp_path,
        quick_login_uin="3956020260",
    )

    wrapper_text = wrapper_path.read_text(encoding="utf-8")
    assert '-q 3956020260' in wrapper_text
    assert 'set "NAPCAT_QUICK_ACCOUNT=3956020260"' in wrapper_text
    assert 'wrapper_quick_account_env=3956020260' in wrapper_text
    assert 'wrapper_launch_command=call "' in wrapper_text


def test_generated_wrapper_forwards_quick_login_args_to_launcher() -> None:
    temp_path = _fresh_test_dir("wrapper_exec")
    args_out = temp_path / "args.txt"
    env_out = temp_path / "env.txt"
    launcher = temp_path / "launcher-win10.bat"
    launcher.write_text(
        "\n".join(
            [
                "@echo off",
                f'echo launcher_args=%* > "{args_out}"',
                f'echo launcher_env=%NAPCAT_QUICK_ACCOUNT% > "{env_out}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _log_path, wrapper_path, _latest_path = _prepare_napcat_launch_artifacts(
        launcher,
        temp_path,
        quick_login_uin="3956020260",
    )

    subprocess.run(
        ["cmd.exe", "/d", "/c", str(wrapper_path)],
        cwd=str(temp_path),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    args_text = args_out.read_text(encoding="utf-8").strip()
    env_text = env_out.read_text(encoding="utf-8").strip()
    assert args_text == "launcher_args=-q 3956020260"
    assert env_text == "launcher_env=3956020260"


def test_prepare_napcat_launch_artifacts_prefers_project_logged_helper() -> None:
    temp_path = _fresh_test_dir("prefer_logged_helper")
    state_dir = temp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    args_out = temp_path / "helper_args.txt"
    override_out = temp_path / "helper_override.txt"
    helper = temp_path / "start_napcat_logged.bat"
    helper.write_text(
        "\n".join(
            [
                "@echo off",
                f'echo helper_args=%* > "{args_out}"',
                f'echo helper_override=%NAPCAT_LAUNCHER_OVERRIDE% > "{override_out}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    launcher = temp_path / "NapCat" / "napcat" / "launcher-win10.bat"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("@echo off\n", encoding="utf-8")

    _log_path, wrapper_path, _latest_path = _prepare_napcat_launch_artifacts(
        launcher,
        state_dir,
        quick_login_uin="3956020260",
    )

    wrapper_text = wrapper_path.read_text(encoding="utf-8")
    assert 'start_napcat_logged.bat' in wrapper_text
    assert 'NAPCAT_LAUNCHER_OVERRIDE=' in wrapper_text

    subprocess.run(
        ["cmd.exe", "/d", "/c", str(wrapper_path)],
        cwd=str(temp_path),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    assert args_out.read_text(encoding="utf-8").strip() == "helper_args=-q 3956020260"
    helper_override = override_out.read_text(encoding="utf-8").strip()
    assert helper_override.startswith("helper_override=")
    assert helper_override.endswith("launcher-win10.bat")
