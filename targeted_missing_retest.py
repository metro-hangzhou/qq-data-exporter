from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent
SRC_PATH = REPO_ROOT / "src"
RUNTIME_SITE_PACKAGES = REPO_ROOT / "runtime_site_packages"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))
if RUNTIME_SITE_PACKAGES.exists() and str(RUNTIME_SITE_PACKAGES) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SITE_PACKAGES))

from qq_data_cli.export_cleanup import cleanup_gateway_media_cache
from qq_data_core import (
    ChatExportService,
    ExportPerfTraceWriter,
    ExportRequest,
    build_export_content_summary,
    format_export_datetime,
    parse_time_expression,
    resolve_time_expression,
)
from qq_data_core.paths import build_timestamp_token
from qq_data_integrations.napcat.bootstrap import NapCatBootstrapper
from qq_data_integrations.napcat.gateway import NapCatGateway
from qq_data_integrations.napcat.models import normalize_chat_type
from qq_data_integrations.napcat.settings import NapCatSettings


@dataclass(frozen=True, slots=True)
class RetryCluster:
    index: int
    start_token: str
    end_token: str
    repl_command: str | None


MISSING_RETRY_CLUSTER_GAP = timedelta(minutes=10)
MISSING_RETRY_WINDOW_PADDING = timedelta(seconds=15)
BACKGROUND_MISSING_KINDS = {
    "qq_not_downloaded_local_placeholder",
    "qq_expired_after_napcat",
}


def _load_manifest(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _find_latest_manifest(exports_dir: Path) -> Path:
    manifests = sorted(
        exports_dir.glob("*.manifest.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not manifests:
        raise FileNotFoundError(f"no manifest found under {exports_dir}")
    return manifests[0]


def _collect_retry_clusters(manifest: dict[str, Any]) -> list[RetryCluster]:
    retry_plan = manifest.get("content_summary", {}).get("missing_retry_plan", {})
    clusters = retry_plan.get("clusters")
    if isinstance(clusters, list) and clusters:
        result: list[RetryCluster] = []
        for idx, cluster in enumerate(clusters, start=1):
            if not isinstance(cluster, dict):
                continue
            start_token = str(cluster.get("start_token") or "").strip()
            end_token = str(cluster.get("end_token") or "").strip()
            if not start_token or not end_token:
                continue
            result.append(
                RetryCluster(
                    index=idx,
                    start_token=start_token,
                    end_token=end_token,
                    repl_command=str(cluster.get("repl_command") or "").strip() or None,
                )
            )
        if result:
            return result
    return _derive_retry_clusters_from_assets(manifest)


def _derive_retry_clusters_from_assets(manifest: dict[str, Any]) -> list[RetryCluster]:
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return []
    actionable_assets: list[tuple[dict[str, Any], datetime]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        if str(asset.get("status") or "").strip().lower() != "missing":
            continue
        missing_kind = str(asset.get("missing_kind") or asset.get("resolver") or "missing").strip()
        if missing_kind in BACKGROUND_MISSING_KINDS:
            continue
        timestamp_iso = str(asset.get("timestamp_iso") or "").strip()
        if not timestamp_iso:
            continue
        try:
            current_dt = datetime.fromisoformat(timestamp_iso)
        except ValueError:
            continue
        actionable_assets.append((asset, current_dt))
    if not actionable_assets:
        return []
    actionable_assets.sort(key=lambda item: item[1])
    clusters: list[list[tuple[dict[str, Any], datetime]]] = []
    current_cluster: list[tuple[dict[str, Any], datetime]] = []
    previous_dt: datetime | None = None
    for asset, current_dt in actionable_assets:
        if not current_cluster or previous_dt is None or current_dt - previous_dt <= MISSING_RETRY_CLUSTER_GAP:
            current_cluster.append((asset, current_dt))
        else:
            clusters.append(current_cluster)
            current_cluster = [(asset, current_dt)]
        previous_dt = current_dt
    if current_cluster:
        clusters.append(current_cluster)
    result: list[RetryCluster] = []
    for idx, cluster in enumerate(clusters, start=1):
        datetimes = [current_dt for _asset, current_dt in cluster]
        start_token = format_export_datetime(min(datetimes) - MISSING_RETRY_WINDOW_PADDING)
        end_token = format_export_datetime(max(datetimes) + MISSING_RETRY_WINDOW_PADDING)
        result.append(
            RetryCluster(
                index=idx,
                start_token=start_token,
                end_token=end_token,
                repl_command=None,
            )
        )
    return result


def _infer_chat_target(manifest_path: Path, manifest: dict[str, Any]) -> tuple[str, str, str | None]:
    summary = manifest.get("content_summary", {})
    chat_type = str(summary.get("chat_type") or "").strip().lower()
    chat_id = str(summary.get("chat_id") or "").strip()
    chat_name = str(summary.get("chat_name") or "").strip() or None
    if not chat_type:
        stem = manifest_path.stem.lower()
        if stem.startswith("group_"):
            chat_type = "group"
        elif stem.startswith("friend_") or stem.startswith("private_"):
            chat_type = "private"
    if not chat_id:
        prefix, _, tail = manifest_path.stem.partition("_")
        if prefix in {"group", "friend", "private"} and tail:
            chat_id = tail.split("_", 1)[0]
    normalized_chat_type = normalize_chat_type(chat_type or "group")
    if not chat_id:
        raise ValueError(f"unable to infer chat id from manifest {manifest_path}")
    return normalized_chat_type, chat_id, chat_name


def _resolve_time_token(token: str) -> datetime:
    expression = parse_time_expression(token)
    return resolve_time_expression(
        expression,
        earliest_content_at=None,
        final_content_at=None,
    )


def _build_run_dir(state_dir: Path) -> Path:
    run_dir = state_dir / "targeted_retests" / f"retest_{build_timestamp_token(include_pid=True)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    latest_path = state_dir / "targeted_retests" / "latest.path"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(str(run_dir), encoding="utf-8")
    return run_dir


def _run_cluster(
    *,
    settings: NapCatSettings,
    manifest_path: Path,
    cluster: RetryCluster,
    run_dir: Path,
) -> dict[str, Any]:
    chat_type, chat_id, chat_name = _infer_chat_target(manifest_path, _load_manifest(manifest_path))
    since_dt = _resolve_time_token(cluster.start_token)
    until_dt = _resolve_time_token(cluster.end_token)
    cluster_dir = run_dir / f"cluster_{cluster.index:02d}"
    cluster_dir.mkdir(parents=True, exist_ok=True)
    trace = ExportPerfTraceWriter(
        settings.state_dir,
        chat_type=chat_type,
        chat_id=chat_id,
        mode=f"targeted_retest_cluster{cluster.index}",
    )
    progress_callback = lambda payload: trace.write_event(str(payload.get("phase") or "progress"), payload)
    gateway: NapCatGateway | None = None
    try:
        start_result = NapCatBootstrapper(settings).ensure_endpoint("onebot_http")
        if not start_result.ready:
            raise RuntimeError(start_result.message or "NapCat onebot_http is not ready")
        gateway = NapCatGateway(settings)
        request = ExportRequest(
            chat_type=chat_type,
            chat_id=chat_id,
            chat_name=chat_name,
            since=since_dt,
            until=until_dt,
        )
        snapshot = gateway.fetch_snapshot_between(request, page_size=100)
        service = ChatExportService()
        normalized = service.build_snapshot(snapshot)
        bundle = service.write_bundle(
            normalized,
            cluster_dir / f"{chat_type}_{chat_id}.jsonl",
            fmt="jsonl",
            media_resolution_mode="napcat_only",
            progress_callback=progress_callback,
            media_download_manager=(
                gateway.build_media_download_manager()
                if hasattr(gateway, "build_media_download_manager")
                else None
            ),
        )
        cleanup_stats = cleanup_gateway_media_cache(gateway, trace=trace)
        content_summary = build_export_content_summary(
            normalized,
            bundle,
            profile="all",
            fmt="jsonl",
            strict_missing=None,
        )
        result = {
            "cluster_index": cluster.index,
            "start_token": cluster.start_token,
            "end_token": cluster.end_token,
            "repl_command": cluster.repl_command,
            "data_path": str(bundle.data_path),
            "manifest_path": str(bundle.manifest_path),
            "record_count": bundle.record_count,
            "copied_asset_count": bundle.copied_asset_count,
            "reused_asset_count": bundle.reused_asset_count,
            "missing_asset_count": bundle.missing_asset_count,
            "error_asset_count": bundle.error_asset_count,
            "content_summary": content_summary,
            "cleanup": cleanup_stats,
            "trace_path": str(trace.path),
        }
        (cluster_dir / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result
    finally:
        trace.close()
        if gateway is not None:
            gateway.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run narrow export retests for actionable missing clusters.")
    parser.add_argument("--manifest", type=Path, help="explicit manifest path; defaults to latest exports/*.manifest.json")
    parser.add_argument("--only-cluster", type=int, help="1-based cluster index from missing_retry_plan")
    args = parser.parse_args(argv)

    settings = NapCatSettings.from_env()
    manifest_path = args.manifest or _find_latest_manifest(settings.export_dir)
    manifest = _load_manifest(manifest_path)
    clusters = _collect_retry_clusters(manifest)
    if not clusters:
        print(f"No retry clusters found in {manifest_path}")
        return 1
    if args.only_cluster is not None:
        clusters = [cluster for cluster in clusters if cluster.index == args.only_cluster]
        if not clusters:
            print(f"Cluster {args.only_cluster} not found in {manifest_path}")
            return 1

    run_dir = _build_run_dir(settings.state_dir)
    print(f"manifest={manifest_path}")
    print(f"run_dir={run_dir}")
    results: list[dict[str, Any]] = []
    for cluster in clusters:
        print(
            f"cluster={cluster.index} window={cluster.start_token} -> {cluster.end_token}"
        )
        result = _run_cluster(
            settings=settings,
            manifest_path=manifest_path,
            cluster=cluster,
            run_dir=run_dir,
        )
        results.append(result)
        summary = result.get("content_summary", {})
        print(
            "result="
            f"records={result.get('record_count')} "
            f"missing={result.get('missing_asset_count')} "
            f"actionable_missing={summary.get('actionable_missing_count', 0)} "
            f"background_missing={summary.get('background_missing_count', 0)}"
        )
    (run_dir / "run_summary.json").write_text(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "cluster_count": len(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"latest={run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
