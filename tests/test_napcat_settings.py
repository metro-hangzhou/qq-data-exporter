from __future__ import annotations

import shutil
from pathlib import Path

import orjson

from qq_data_integrations.napcat import NapCatSettings


def test_settings_discover_runtime_config(monkeypatch) -> None:
    workdir = Path(".tmp") / "test_napcat_settings"
    shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "config").mkdir(parents=True, exist_ok=True)
    (workdir / "config" / "onebot11.json").write_bytes(
        orjson.dumps(
            {
                "network": {
                    "httpServers": [
                        {"enable": True, "host": "0.0.0.0", "port": 3100, "token": "http-token"}
                    ],
                    "websocketServers": [
                        {"enable": True, "host": "::", "port": 3101, "token": "ws-token"}
                    ],
                }
            }
        )
    )
    (workdir / "config" / "webui.json").write_bytes(
        orjson.dumps(
            {
                "host": "::",
                "port": 6200,
                "token": "webui-secret",
            }
        )
    )

    monkeypatch.setenv("NAPCAT_WORKDIR", str(workdir))
    monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
    monkeypatch.delenv("NAPCAT_WS_URL", raising=False)
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_URL", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_TOKEN", raising=False)
    monkeypatch.delenv("NAPCAT_ONEBOT_CONFIG", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_CONFIG", raising=False)

    settings = NapCatSettings.from_env()

    assert settings.project_root.resolve() == Path.cwd().resolve()
    assert settings.auto_start_napcat is True
    assert settings.http_url == "http://127.0.0.1:3100"
    assert settings.ws_url == "ws://127.0.0.1:3101"
    assert settings.access_token == "http-token"
    assert settings.webui_url == "http://127.0.0.1:6200/api"
    assert settings.webui_token == "webui-secret"
    assert settings.export_dir == Path.cwd().resolve() / "exports"
    assert settings.state_dir == Path.cwd().resolve() / "state"
    assert settings.onebot_config_path == (workdir / "config" / "onebot11.json").resolve()
    assert settings.webui_config_path == (workdir / "config" / "webui.json").resolve()

    shutil.rmtree(workdir, ignore_errors=True)


def test_settings_discover_from_parent_project_layout(monkeypatch) -> None:
    root = Path(".tmp") / "test_napcat_parent_layout"
    root_abs = root.resolve()
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='test'\nversion='0.0.0'\n", encoding="utf-8")
    (root / "src" / "qq_data_cli").mkdir(parents=True, exist_ok=True)
    (root / "NapCatQQ" / "packages" / "napcat-develop" / "config").mkdir(parents=True, exist_ok=True)
    (root / "NapCatQQ" / "packages" / "napcat-webui-backend").mkdir(parents=True, exist_ok=True)

    (root / "NapCatQQ" / "packages" / "napcat-develop" / "config" / "onebot11.json").write_bytes(
        orjson.dumps(
            {
                "network": {
                    "httpServers": [
                        {"enable": True, "host": "0.0.0.0", "port": 3550, "token": ""}
                    ],
                    "websocketServers": [
                        {"enable": True, "host": "0.0.0.0", "port": 3551, "token": ""}
                    ],
                }
            }
        )
    )
    (root / "NapCatQQ" / "packages" / "napcat-webui-backend" / "webui.json").write_bytes(
        orjson.dumps(
            {
                "host": "0.0.0.0",
                "port": 6550,
                "token": "random",
            }
        )
    )

    monkeypatch.chdir(root / "src" / "qq_data_cli")
    monkeypatch.delenv("NAPCAT_WORKDIR", raising=False)
    monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
    monkeypatch.delenv("NAPCAT_WS_URL", raising=False)
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_URL", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_TOKEN", raising=False)
    monkeypatch.delenv("NAPCAT_ONEBOT_CONFIG", raising=False)
    monkeypatch.delenv("NAPCAT_WEBUI_CONFIG", raising=False)
    monkeypatch.delenv("NAPCAT_DIR", raising=False)
    monkeypatch.delenv("NAPCAT_LAUNCHER", raising=False)

    settings = NapCatSettings.from_env()

    assert settings.project_root == root_abs
    assert settings.napcat_dir == root_abs / "NapCatQQ"
    assert settings.napcat_launcher_path is None
    assert settings.http_url == "http://127.0.0.1:3550"
    assert settings.ws_url == "ws://127.0.0.1:3551"
    assert settings.access_token == ""
    assert settings.webui_url == "http://127.0.0.1:6550/api"
    assert settings.webui_token == "random"
    assert settings.export_dir == root_abs / "exports"
    assert settings.state_dir == root_abs / "state"
    assert settings.onebot_config_path == (
        root_abs / "NapCatQQ" / "packages" / "napcat-develop" / "config" / "onebot11.json"
    )
    assert settings.webui_config_path == (
        root_abs / "NapCatQQ" / "packages" / "napcat-webui-backend" / "webui.json"
    )

    shutil.rmtree(root, ignore_errors=True)


def test_settings_prefer_runtime_account_specific_onebot_config(monkeypatch) -> None:
    workdir = Path(".tmp") / "test_napcat_runtime_account_config"
    shutil.rmtree(workdir, ignore_errors=True)
    (workdir / "config").mkdir(parents=True, exist_ok=True)
    (workdir / "config" / "onebot11.json").write_bytes(
        orjson.dumps(
            {
                "network": {
                    "httpServers": [{"enable": True, "host": "127.0.0.1", "port": 3000, "token": ""}],
                    "websocketServers": [{"enable": True, "host": "127.0.0.1", "port": 3001, "token": ""}],
                }
            }
        )
    )
    (workdir / "config" / "onebot11_123456.json").write_bytes(
        orjson.dumps(
            {
                "network": {
                    "httpServers": [{"enable": True, "host": "127.0.0.1", "port": 3456, "token": "abc"}],
                    "websocketServers": [{"enable": True, "host": "127.0.0.1", "port": 3457, "token": "abc"}],
                }
            }
        )
    )

    monkeypatch.setenv("NAPCAT_WORKDIR", str(workdir))
    monkeypatch.delenv("NAPCAT_ONEBOT_CONFIG", raising=False)
    monkeypatch.delenv("NAPCAT_HTTP_URL", raising=False)
    monkeypatch.delenv("NAPCAT_WS_URL", raising=False)
    monkeypatch.delenv("NAPCAT_TOKEN", raising=False)

    settings = NapCatSettings.from_env()

    assert settings.onebot_config_path == (workdir / "config" / "onebot11_123456.json").resolve()
    assert settings.http_url == "http://127.0.0.1:3456"
    assert settings.ws_url == "ws://127.0.0.1:3457"
    assert settings.access_token == "abc"
    assert settings.export_dir == Path.cwd().resolve() / "exports"
    assert settings.state_dir == Path.cwd().resolve() / "state"

    shutil.rmtree(workdir, ignore_errors=True)
