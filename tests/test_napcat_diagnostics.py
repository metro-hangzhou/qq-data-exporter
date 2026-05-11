from __future__ import annotations

from pathlib import Path

from qq_data_integrations.napcat.diagnostics import collect_debug_preflight_evidence
from qq_data_integrations.napcat.settings import NapCatSettings


def test_collect_debug_preflight_evidence_includes_path_and_capability_matrix(monkeypatch) -> None:
    root = Path(".tmp") / "test_napcat_debug_preflight"
    plugin_dir = root / "NapCat" / "napcat" / "plugins" / "napcat-plugin-qq-data-fast"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    state_dir = root / "state"
    export_dir = root / "exports"
    state_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)

    settings = NapCatSettings(
        project_root=root.resolve(),
        napcat_dir=(root / "NapCat").resolve(),
        fast_history_url="http://127.0.0.1:6099/plugin/napcat-plugin-qq-data-fast/api",
        fast_history_plugin_id="napcat-plugin-qq-data-fast",
        state_dir=state_dir.resolve(),
        export_dir=export_dir.resolve(),
        workdir=(root / "NapCat" / "napcat").resolve(),
    )

    monkeypatch.setattr(
        "qq_data_integrations.napcat.diagnostics.probe_settings_endpoints",
        lambda settings, timeout=0.5: [],
    )

    class FakeFastClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def health(self):
            return {"ok": True, "plugin": "napcat-plugin-qq-data-fast", "version": "0.1.0"}

        def capabilities(self):
            return {
                "ok": True,
                "routes": [
                    {"name": "health", "method": "GET", "path": "/health"},
                    {"name": "history_tail_bulk", "method": "POST", "path": "/history-tail-bulk"},
                    {"name": "hydrate_forward_media", "method": "POST", "path": "/hydrate-forward-media"},
                ],
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr("qq_data_integrations.napcat.diagnostics.NapCatFastHistoryClient", FakeFastClient)

    evidence = collect_debug_preflight_evidence(settings)

    assert "path_matrix" in evidence
    assert "capability_matrix" in evidence
    path_labels = {item["label"] for item in evidence["path_matrix"]}
    assert "project_root" in path_labels
    assert any(label.startswith("fast_history_plugin_path[") for label in path_labels)
    assert evidence["capability_matrix"]["fast_history_plugin"]["health"]["reachable"] is True
    assert evidence["capability_matrix"]["fast_history_plugin"]["capabilities_source"] == "plugin_route"
    routes = evidence["capability_matrix"]["fast_history_plugin"]["routes"]
    route_map = {route["name"]: route for route in routes}
    assert route_map["history_tail_bulk"]["reachable"] is True
    assert route_map["hydrate_forward_media"]["reachable"] is True
