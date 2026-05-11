from __future__ import annotations

from pathlib import Path

from qq_data_integrations.napcat.diagnostics import NapCatEndpointProbe
from qq_data_integrations.napcat.runtime import NapCatRuntimeStarter
from qq_data_integrations.napcat.settings import NapCatSettings


def test_runtime_starter_describes_launchable_runtime() -> None:
    root = Path(".tmp") / "test_runtime_launchable"
    launcher_dir = root / "NapCatQQ" / "packages" / "napcat-shell-loader"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    for name in ["launcher-win10.bat", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll", "qqnt.json", "napcat.mjs"]:
        (launcher_dir / name).write_text("x", encoding="utf-8")

    settings = NapCatSettings(
        project_root=root.resolve(),
        napcat_dir=(root / "NapCatQQ").resolve(),
        napcat_launcher_path=(launcher_dir / "launcher-win10.bat").resolve(),
    )
    info = NapCatRuntimeStarter(settings).describe_launch()

    assert info.launchable is True
    assert info.launcher_path == (launcher_dir / "launcher-win10.bat").resolve()


def test_runtime_starter_accepts_node_runtime_launcher() -> None:
    root = Path(".tmp") / "test_runtime_node_launcher"
    runtime_dir = root / "NapCat"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "napcat").mkdir(parents=True, exist_ok=True)
    for name in ["napcat.bat", "node.exe", "index.js", "wrapper.node"]:
        (runtime_dir / name).write_text("x", encoding="utf-8")
    (runtime_dir / "napcat" / "napcat.mjs").write_text("x", encoding="utf-8")

    settings = NapCatSettings(
        project_root=root.resolve(),
        napcat_dir=runtime_dir.resolve(),
        napcat_launcher_path=(runtime_dir / "napcat.bat").resolve(),
    )
    info = NapCatRuntimeStarter(settings).describe_launch()

    assert info.launchable is True
    assert info.launcher_path == (runtime_dir / "napcat.bat").resolve()


def test_runtime_starter_auto_starts_when_endpoint_is_down(monkeypatch) -> None:
    root = Path(".tmp") / "test_runtime_autostart"
    launcher_dir = root / "NapCatQQ" / "packages" / "napcat-shell-loader"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = launcher_dir / "launcher-win10.bat"
    for name in ["launcher-win10.bat", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll", "qqnt.json", "napcat.mjs"]:
        (launcher_dir / name).write_text("x", encoding="utf-8")

    settings = NapCatSettings(
        project_root=root.resolve(),
        napcat_dir=(root / "NapCatQQ").resolve(),
        napcat_launcher_path=launcher_path.resolve(),
        webui_url="http://127.0.0.1:6099/api",
    )

    calls = {"count": 0}
    launches: list[Path] = []

    def fake_probe(name: str, url: str, timeout: float = 0.25) -> NapCatEndpointProbe:
        calls["count"] += 1
        return NapCatEndpointProbe(
            name=name,
            url=url,
            host="127.0.0.1",
            port=6099,
            listening=calls["count"] >= 2,
        )

    ticks = iter([0.0, 0.1, 0.2, 0.3])
    monkeypatch.setattr("qq_data_integrations.napcat.runtime.probe_endpoint", fake_probe)
    starter = NapCatRuntimeStarter(
        settings,
        monotonic=lambda: next(ticks),
        sleep=lambda _: None,
        launch_process=lambda path: launches.append(path),
    )

    result = starter.ensure_endpoint("webui", timeout_seconds=1.0, poll_interval=0.0)

    assert result.ready is True
    assert result.attempted_start is True
    assert launches == [launcher_path.resolve()]


def test_runtime_starter_does_not_restart_when_other_endpoint_is_alive(monkeypatch) -> None:
    root = Path(".tmp") / "test_runtime_existing_instance"
    launcher_dir = root / "NapCat" / "napcat"
    launcher_dir.mkdir(parents=True, exist_ok=True)
    launcher_path = launcher_dir / "launcher-win10.bat"
    for name in ["launcher-win10.bat", "NapCatWinBootMain.exe", "NapCatWinBootHook.dll"]:
        (launcher_dir / name).write_text("x", encoding="utf-8")

    settings = NapCatSettings(
        project_root=root.resolve(),
        napcat_dir=(root / "NapCat").resolve(),
        napcat_launcher_path=launcher_path.resolve(),
        http_url="http://127.0.0.1:3000",
        ws_url="ws://127.0.0.1:3001",
        webui_url="http://127.0.0.1:6099/api",
    )

    launches: list[Path] = []

    def fake_probe(name: str, url: str, timeout: float = 0.25) -> NapCatEndpointProbe:
        return NapCatEndpointProbe(
            name=name,
            url=url,
            host="127.0.0.1",
            port=6099 if name == "webui" else 3000,
            listening=name == "webui",
        )

    monkeypatch.setattr("qq_data_integrations.napcat.runtime.probe_endpoint", fake_probe)
    starter = NapCatRuntimeStarter(
        settings,
        launch_process=lambda path: launches.append(path),
    )

    result = starter.ensure_endpoint("onebot_http", timeout_seconds=0.1, poll_interval=0.0)

    assert result.ready is False
    assert result.attempted_start is False
    assert "already running" in result.message
    assert launches == []
