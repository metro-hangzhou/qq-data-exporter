from __future__ import annotations

import argparse
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import orjson

from qq_data_core.media_bundle import (
    _MediaSearchContext,
    _build_media_search_context,
    _existing_path,
    _iter_asset_candidates,
    _resolve_via_legacy_md5,
)
from qq_data_core.models import EXPORT_TIMEZONE, NormalizedMessage, NormalizedSnapshot
from qq_data_integrations.local_qq import discover_qq_media_roots
from qq_data_integrations.napcat.fast_history_client import NapCatFastHistoryClient
from qq_data_integrations.napcat.http_client import NapCatHttpClient
from qq_data_integrations.napcat.media_downloader import NapCatMediaDownloader
from qq_data_integrations.napcat.settings import NapCatSettings
from qq_data_integrations.napcat.webui_client import NapCatWebUiClient, NapCatWebUiError


@dataclass(slots=True)
class RouteMetric:
    attempts: int = 0
    hits: int = 0
    elapsed_ms: list[float] = field(default_factory=list)

    def record(self, *, hit: bool, elapsed_ms: float) -> None:
        self.attempts += 1
        if hit:
            self.hits += 1
        self.elapsed_ms.append(elapsed_ms)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark three media-resolution routes: napcat context, public token, and legacy md5.",
    )
    parser.add_argument("jsonl_path", type=Path, help="Path to an exported JSONL file.")
    parser.add_argument(
        "--limit-assets",
        type=int,
        default=300,
        help="Maximum number of unique assets to benchmark after de-duplication (default: 300).",
    )
    parser.add_argument(
        "--asset-type",
        choices=["image", "file", "speech", "video", "sticker", "all"],
        default="image",
        help="Asset type to benchmark (default: image).",
    )
    parser.add_argument(
        "--with-napcat",
        action="store_true",
        help="Enable NapCat-backed routes (context hydration and public token).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write a JSON benchmark report.",
    )
    return parser.parse_args()


def _load_snapshot(jsonl_path: Path) -> NormalizedSnapshot:
    messages: list[NormalizedMessage] = []
    with jsonl_path.open("rb") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            messages.append(NormalizedMessage.model_validate(orjson.loads(line)))
    if not messages:
        raise RuntimeError(f"No messages found in {jsonl_path}")
    first = messages[0]
    return NormalizedSnapshot(
        chat_type=first.chat_type,
        chat_id=first.chat_id,
        chat_name=first.chat_name,
        exported_at=datetime.now(EXPORT_TIMEZONE),
        metadata={},
        messages=messages,
    )


def _collect_candidates(snapshot: NormalizedSnapshot, asset_type: str, limit_assets: int) -> list[Any]:
    candidates: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for message in snapshot.messages:
        for candidate in _iter_asset_candidates(message):
            if asset_type != "all" and candidate.asset_type != asset_type:
                continue
            key = (
                candidate.asset_type,
                candidate.asset_role,
                (candidate.file_name or "").lower(),
                (candidate.md5 or "").lower(),
                (candidate.source_path or "").lower(),
                candidate.timestamp_ms,
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if len(candidates) >= limit_assets:
                return candidates
    return candidates


def _build_downloader() -> NapCatMediaDownloader | None:
    settings = NapCatSettings.from_env()
    if not settings.http_url:
        return None
    client = NapCatHttpClient(
        settings.http_url,
        access_token=settings.access_token,
        use_system_proxy=settings.use_system_proxy,
    )
    fast_client = None
    if settings.fast_history_mode != "off" and settings.fast_history_url:
        fast_headers = _build_fast_history_headers(settings)
        fast_client = NapCatFastHistoryClient(
            settings.fast_history_url,
            headers=fast_headers,
            use_system_proxy=settings.use_system_proxy,
        )
    return NapCatMediaDownloader(
        client,
        fast_client=fast_client,
        remote_cache_dir=settings.state_dir / "media_remote_cache",
        remote_base_url=settings.http_url,
        use_system_proxy=settings.use_system_proxy,
    )


def _build_fast_history_headers(settings: NapCatSettings) -> dict[str, str] | None:
    if not settings.webui_url or not settings.webui_token:
        return None
    client = NapCatWebUiClient(
        settings.webui_url,
        raw_token=settings.webui_token,
        use_system_proxy=settings.use_system_proxy,
    )
    try:
        credential = client.ensure_authenticated()
    except NapCatWebUiError:
        return None
    finally:
        client.close()
    return {"Authorization": f"Bearer {credential}"}


def _candidate_request(candidate: Any) -> dict[str, Any]:
    return {
        "asset_type": candidate.asset_type,
        "asset_role": candidate.asset_role,
        "file_name": candidate.file_name,
        "source_path": candidate.source_path,
        "md5": candidate.md5,
        "timestamp_ms": candidate.timestamp_ms,
        "download_hint": dict(candidate.download_hint or {}),
    }


def _time_call(fn: Callable[[], tuple[Path | None, str | None]]) -> tuple[tuple[Path | None, str | None], float]:
    started = time.perf_counter()
    result = fn()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return result, elapsed_ms


def _metric_summary(metric: RouteMetric) -> dict[str, Any]:
    elapsed = metric.elapsed_ms
    if not elapsed:
        return {"attempts": 0, "hits": 0, "avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
    sorted_elapsed = sorted(elapsed)
    p95_index = min(len(sorted_elapsed) - 1, max(0, int(len(sorted_elapsed) * 0.95) - 1))
    return {
        "attempts": metric.attempts,
        "hits": metric.hits,
        "avg_ms": round(statistics.fmean(elapsed), 3),
        "p95_ms": round(sorted_elapsed[p95_index], 3),
        "max_ms": round(max(elapsed), 3),
    }


def _age_bucket(timestamp_ms: int) -> str:
    delta_days = (datetime.now(EXPORT_TIMEZONE).timestamp() * 1000 - timestamp_ms) / (24 * 60 * 60 * 1000)
    if delta_days <= 7:
        return "<=7d"
    if delta_days <= 30:
        return "8-30d"
    if delta_days <= 90:
        return "31-90d"
    if delta_days <= 180:
        return "91-180d"
    return ">180d"


def _run_route(
    *,
    route_name: str,
    candidates: list[Any],
    runner: Callable[[Any], tuple[Path | None, str | None]],
) -> dict[str, Any]:
    metric = RouteMetric()
    final_resolvers: Counter[str] = Counter()
    age_buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0, "unresolved": 0})
    rows: list[dict[str, Any]] = []

    for candidate in candidates:
        (resolved_path, resolver), elapsed_ms = _time_call(lambda: runner(candidate))
        hit = resolved_path is not None
        metric.record(hit=hit, elapsed_ms=elapsed_ms)
        final_resolvers[resolver or "unresolved"] += 1
        bucket = _age_bucket(candidate.timestamp_ms)
        age_buckets[bucket]["total"] += 1
        if not hit:
            age_buckets[bucket]["unresolved"] += 1
        rows.append(
            {
                "file_name": candidate.file_name,
                "source_path": candidate.source_path,
                "md5": candidate.md5,
                "timestamp_ms": candidate.timestamp_ms,
                "resolved_path": str(resolved_path) if resolved_path is not None else None,
                "resolver": resolver or "unresolved",
                "elapsed_ms": round(elapsed_ms, 3),
            }
        )

    age_summary = {}
    for bucket, payload in age_buckets.items():
        total = payload["total"]
        unresolved = payload["unresolved"]
        age_summary[bucket] = {
            "total": total,
            "unresolved": unresolved,
            "unresolved_rate": round(unresolved / total, 4) if total else 0.0,
        }

    return {
        "route": route_name,
        "metrics": _metric_summary(metric),
        "final_resolvers": dict(final_resolvers),
        "age_buckets": age_summary,
        "rows": rows,
    }


def _run_legacy_md5_route(candidate: Any, *, context: _MediaSearchContext) -> tuple[Path | None, str | None]:
    if candidate.asset_type not in {"image", "sticker"}:
        return None, "legacy_not_applicable"
    if not candidate.md5:
        return None, "legacy_no_md5"
    resolved = _resolve_via_legacy_md5(candidate, context=context)
    if resolved is None:
        return None, None
    return resolved, "legacy_md5_index"


def _print_summary(report: dict[str, Any]) -> None:
    print("== Media Resolution Benchmark ==")
    print(f"jsonl_path: {report['jsonl_path']}")
    print(f"messages: {report['message_count']}")
    print(f"unique_assets_tested: {report['asset_count']}")
    print(f"asset_type: {report['asset_type']}")
    print(f"search_roots: {len(report['search_roots'])}")
    print(f"with_napcat: {report['with_napcat']}")
    print("")
    print("direct_local_precheck:")
    print(
        f"  hits={report['direct_local_precheck']['hits']} / {report['asset_count']} "
        f"({report['direct_local_precheck']['hit_rate']})"
    )
    print("")
    for route in report["routes"]:
        metrics = route["metrics"]
        print(f"{route['route']}:")
        print(
            f"  attempts={metrics['attempts']} hits={metrics['hits']} "
            f"avg={metrics['avg_ms']}ms p95={metrics['p95_ms']}ms max={metrics['max_ms']}ms"
        )
        print("  final_resolvers:")
        for name, count in sorted(route["final_resolvers"].items(), key=lambda item: (-item[1], item[0])):
            print(f"    - {name}: {count}")
        print("  age_bucket_vs_unresolved:")
        for bucket, payload in route["age_buckets"].items():
            print(
                f"    - {bucket}: total={payload['total']} unresolved={payload['unresolved']} "
                f"rate={payload['unresolved_rate']}"
            )
        print("")


def main() -> int:
    args = _parse_args()
    snapshot = _load_snapshot(args.jsonl_path)
    candidates = _collect_candidates(snapshot, args.asset_type, args.limit_assets)
    search_roots = discover_qq_media_roots()
    context = _build_media_search_context(
        search_roots,
        candidates,
        snapshot=snapshot,
        media_cache_dir=Path("state") / "media_index",
    )

    direct_local_hits = sum(1 for candidate in candidates if _existing_path(candidate.source_path) is not None)

    downloader = _build_downloader() if args.with_napcat else None
    try:
        routes: list[dict[str, Any]] = []

        if downloader is not None:
            routes.append(
                _run_route(
                    route_name="napcat_context_only",
                    candidates=candidates,
                    runner=lambda candidate: downloader.resolve_via_context_route(_candidate_request(candidate)),
                )
            )
            routes.append(
                _run_route(
                    route_name="napcat_public_token",
                    candidates=candidates,
                    runner=lambda candidate: downloader.resolve_via_public_token_route(_candidate_request(candidate)),
                )
            )
        else:
            routes.extend(
                [
                    {
                        "route": "napcat_context_only",
                        "metrics": _metric_summary(RouteMetric()),
                        "final_resolvers": {},
                        "age_buckets": {},
                        "rows": [],
                    },
                    {
                        "route": "napcat_public_token",
                        "metrics": _metric_summary(RouteMetric()),
                        "final_resolvers": {},
                        "age_buckets": {},
                        "rows": [],
                    },
                ]
            )

        routes.append(
            _run_route(
                route_name="legacy_md5_research_only",
                candidates=candidates,
                runner=lambda candidate: _run_legacy_md5_route(candidate, context=context),
            )
        )
    finally:
        if downloader is not None:
            downloader.close()

    report = {
        "jsonl_path": str(args.jsonl_path.resolve()),
        "message_count": len(snapshot.messages),
        "asset_count": len(candidates),
        "asset_type": args.asset_type,
        "with_napcat": bool(args.with_napcat),
        "search_roots": [str(root) for root in search_roots],
        "direct_local_precheck": {
            "hits": direct_local_hits,
            "hit_rate": round(direct_local_hits / len(candidates), 4) if candidates else 0.0,
        },
        "routes": routes,
    }
    _print_summary(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
        print("")
        print(f"wrote_report: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
