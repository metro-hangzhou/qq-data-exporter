from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path


def _fresh_test_dir(name: str) -> Path:
    path = Path(".tmp") / "tests" / "restart_napcat_service" / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def test_restart_napcat_service_launches_helper_with_quick_login_env() -> None:
    tmp_path = _fresh_test_dir("launches_helper")
    script_src = Path(__file__).resolve().parents[1] / "restart_napcat_service.ps1"
    script_dst = tmp_path / "restart_napcat_service.ps1"
    script_dst.write_text(script_src.read_text(encoding="utf-8"), encoding="utf-8")

    probe_file = tmp_path / "launcher_probe.txt"
    launcher = tmp_path / "start_napcat_logged.bat"
    launcher.write_text(
        "\n".join(
            [
                "@echo off",
                f'echo launcher_args=%* > "{probe_file}"',
                f'echo launcher_env=%NAPCAT_QUICK_ACCOUNT% >> "{probe_file}"',
                "exit /b 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_dst),
            "-RepoPath",
            str(tmp_path),
            "-LauncherPath",
            str(launcher),
            "-QuickLoginUin",
            "3956020260",
        ],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    deadline = time.time() + 5.0
    while time.time() < deadline and not probe_file.exists():
        time.sleep(0.1)
    assert probe_file.exists()
    probe_text = probe_file.read_text(encoding="utf-8")
    assert "launcher_env=3956020260" in probe_text


def test_restart_napcat_service_targets_repo_napcat_and_logged_qq_processes() -> None:
    script_text = (Path(__file__).resolve().parents[1] / "restart_napcat_service.ps1").read_text(
        encoding="utf-8"
    )

    assert "NapCatWinBootMain" in script_text
    assert 'QQ.exe' in script_text
    assert "--enable-logging" in script_text
