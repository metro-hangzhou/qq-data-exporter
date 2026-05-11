from __future__ import annotations

from pathlib import Path


def test_start_cli_handoffs_after_update_and_marks_napcat_restart() -> None:
    script_text = (Path(__file__).resolve().parents[1] / "start_cli.bat").read_text(encoding="utf-8")

    assert "CLI_POST_UPDATE_HANDOFF" in script_text
    assert "_CLI_EXIT_AFTER_UPDATE" in script_text
    assert "CLI_NAPCAT_RESTART_REQUIRED=1" in script_text
    assert '"%ComSpec%" /d /s /c call "%~f0" %_CLI_ARGS%' in script_text
    assert "call :handoff_after_update" not in script_text
    assert '_CLI_REEXEC_CMD' not in script_text
    assert '\\"%~f0\\"' not in script_text
    assert "--post-update-handoff" not in script_text
    assert "restart_napcat_service.ps1" in script_text
    assert "NapCat update detected. Restarting NapCatQQ Service..." in script_text


def test_start_cli_tracks_napcat_related_diff_paths() -> None:
    script_text = (Path(__file__).resolve().parents[1] / "start_cli.bat").read_text(encoding="utf-8")

    assert 'if /I "!_UPDATED_PATH!"=="start_napcat_logged.bat" set "_NAPCAT_DIFF_CHANGED=1"' in script_text
    assert 'if /I "!_UPDATED_PATH!"=="restart_napcat_service.ps1" set "_NAPCAT_DIFF_CHANGED=1"' in script_text
    assert 'if /I "!_UPDATED_PATH:~0,7!"=="NapCat/" set "_NAPCAT_DIFF_CHANGED=1"' in script_text
    assert 'if /I "!_UPDATED_PATH:~0,32!"=="src/qq_data_integrations/napcat/" set "_NAPCAT_DIFF_CHANGED=1"' in script_text
