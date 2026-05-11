from __future__ import annotations

import copy
import json
import math
import os
import shutil
import time
from collections import Counter
from concurrent.futures import Future
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any, Callable
from urllib.parse import urljoin

from .fast_history_client import NapCatFastHistoryTimeoutError, NapCatFastHistoryUnavailable
from .http_client import NapCatApiError, NapCatApiTimeoutError
from .media_downloader import NapCatMediaDownloader


class _DummyClient:
    pass


class _SleepingTimeoutPublicFileClient:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = max(0.0, float(delay_s))
        self.get_file_calls = 0

    def get_file(self, *args, **kwargs):
        self.get_file_calls += 1
        if self.delay_s > 0.0:
            time.sleep(self.delay_s)
        raise NapCatApiTimeoutError("NapCat action timed out: get_file")


class _SleepingTimeoutPublicRecordClient:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = max(0.0, float(delay_s))
        self.get_record_calls = 0

    def get_record(self, *args, **kwargs):
        self.get_record_calls += 1
        if self.delay_s > 0.0:
            time.sleep(self.delay_s)
        raise NapCatApiTimeoutError("NapCat action timed out: get_record")


class _SleepingTimeoutForwardClient:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = max(0.0, float(delay_s))
        self.calls: list[dict[str, object]] = []

    def hydrate_forward_media(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay_s > 0.0:
            time.sleep(self.delay_s)
        raise NapCatFastHistoryTimeoutError("timed out")


@dataclass(frozen=True, slots=True)
class AssetSimulationResult:
    route: str
    asset_type: str
    age_days: int
    parents: int
    siblings_per_parent: int
    total_requests: int
    backend_timeout_calls: int
    short_circuited_requests: int
    simulated_elapsed_s: float
    equivalent_live_timeout_s: float
    timeout_budget_s: float
    progress_snapshot: dict[str, Any]
    trace_event_count: int
    trace_status_breakdown: dict[str, int]
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _age_bucket_label(age_days: int) -> str:
    normalized = max(0, int(age_days))
    if normalized >= 180:
        return "old_forward"
    if normalized >= 30:
        return "aged"
    return "recent"


def build_forward_timeout_request(
    *,
    asset_type: str,
    parent_index: int,
    sibling_index: int,
    age_days: int = 20,
) -> dict[str, object]:
    suffix = {
        "video": "mp4",
        "speech": "mp3",
        "file": "bin",
    }.get(asset_type, "dat")
    return {
        "asset_type": asset_type,
        "asset_role": "forward_media",
        "file_name": f"{asset_type}-p{parent_index:04d}-s{sibling_index:04d}.{suffix}",
        "md5": f"{parent_index:04d}{sibling_index:04d}".lower(),
        "timestamp_ms": _timestamp_ms_for_age_days(age_days),
        "download_hint": {
            "_forward_parent": {
                "message_id_raw": f"7617{parent_index:012d}",
                "element_id": f"7617{parent_index:012d}",
                "peer_uid": "u_simulated",
                "chat_type_raw": "2",
            }
        },
    }


def run_forward_timeout_simulation(
    *,
    route: str,
    asset_type: str,
    parents: int,
    siblings_per_parent: int,
    age_days: int = 20,
    delay_s: float = 0.0,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetSimulationResult:
    normalized_route = str(route or "").strip().lower()
    normalized_asset_type = str(asset_type or "").strip().lower()
    if normalized_route not in {"public-token", "forward-materialize", "forward-metadata"}:
        raise ValueError(f"unsupported route: {route}")
    if normalized_asset_type not in {"video", "speech", "file"}:
        raise ValueError(f"unsupported asset_type: {asset_type}")
    parents = max(1, int(parents))
    siblings_per_parent = max(1, int(siblings_per_parent))
    age_days = max(0, int(age_days))
    total_requests = parents * siblings_per_parent

    events: list[dict[str, Any]] = []
    timeout_probe_request = build_forward_timeout_request(
        asset_type=normalized_asset_type,
        parent_index=0,
        sibling_index=0,
        age_days=age_days,
    )
    if normalized_route == "public-token":
        action = "get_record" if normalized_asset_type == "speech" else "get_file"
        timeout_budget_s = NapCatMediaDownloader.PUBLIC_TOKEN_ACTION_TIMEOUT_S
        backend_timeout_calls = total_requests
        substep = f"public_token_{action}"
    else:
        timeout_budget_s = (
            NapCatMediaDownloader.FORWARD_CONTEXT_TIMEOUT_S
            if age_days < NapCatMediaDownloader.FORWARD_TIMEOUT_STORM_GLOBAL_MIN_AGE_DAYS
            else (
                NapCatMediaDownloader.OLD_FORWARD_EXPENSIVE_MATERIALIZE_TIMEOUT_S
                if normalized_route == "forward-materialize"
                else NapCatMediaDownloader.OLD_FORWARD_EXPENSIVE_METADATA_TIMEOUT_S
            )
        )
        backend_timeout_calls = parents
        substep = (
            "forward_context_materialize"
            if normalized_route == "forward-materialize"
            else "forward_context_metadata"
        )
    elapsed_s = round(float(delay_s) * backend_timeout_calls, 6)
    if trace_callback is not None:
        for parent_index in range(parents):
            request = build_forward_timeout_request(
                asset_type=normalized_asset_type,
                parent_index=parent_index,
                sibling_index=0,
                age_days=age_days,
            )
            event = {
                "phase": "materialize_asset_substep",
                "stage": "done",
                "substep": substep,
                "asset_type": normalized_asset_type,
                "status": "timeout",
                "timeout_s": timeout_budget_s,
                "elapsed_s": float(delay_s),
                "file_name": str(request.get("file_name") or ""),
                "message_id_raw": str(
                    (
                        ((request.get("download_hint") or {}).get("_forward_parent") or {})
                    ).get("message_id_raw")
                    or ""
                ),
            }
            events.append(event)
            trace_callback(dict(event))

    short_circuited_requests = max(0, total_requests - backend_timeout_calls)
    equivalent_live_timeout_s = backend_timeout_calls * timeout_budget_s
    trace_status_breakdown: dict[str, int] = {"timeout": backend_timeout_calls} if backend_timeout_calls > 0 else {}
    progress_snapshot = {
        "candidate_total": total_requests,
        "queued": 0,
        "active": 0,
        "completed": 0,
        "failed": backend_timeout_calls,
        "cached": short_circuited_requests,
        "timeout_count": backend_timeout_calls,
        "forward_context_timeout_count": (
            backend_timeout_calls if normalized_route != "public-token" else 0
        ),
        "last_asset_type": normalized_asset_type,
        "last_file_name": build_forward_timeout_request(
            asset_type=normalized_asset_type,
            parent_index=max(0, parents - 1),
            sibling_index=0,
            age_days=age_days,
        ).get("file_name"),
        "last_status": "timeout" if backend_timeout_calls > 0 else None,
    }
    explanation = (
        "Parent-scoped timeout short-circuit is working for siblings under the same forward parent."
        if parents == 1 and siblings_per_parent > 1
        else "Each distinct forward parent still pays one full timeout; short-circuit only helps repeated siblings under the same parent."
    )

    return AssetSimulationResult(
        route=normalized_route,
        asset_type=normalized_asset_type,
        age_days=age_days,
        parents=parents,
        siblings_per_parent=siblings_per_parent,
        total_requests=total_requests,
        backend_timeout_calls=backend_timeout_calls,
            short_circuited_requests=short_circuited_requests,
            simulated_elapsed_s=round(elapsed_s, 6),
            equivalent_live_timeout_s=round(equivalent_live_timeout_s, 3),
            timeout_budget_s=round(timeout_budget_s, 3),
            progress_snapshot=progress_snapshot,
            trace_event_count=len(events),
            trace_status_breakdown=trace_status_breakdown,
            explanation=explanation,
        )


@lru_cache(maxsize=1)
def _default_forward_timeout_matrix_cached() -> tuple[AssetSimulationResult, ...]:
    scenarios = [
        (route, asset_type, parents, siblings_per_parent, age_days)
        for route in ("public-token", "forward-materialize", "forward-metadata")
        for asset_type in ("video", "file", "speech")
        for age_days in (20, 260)
        for parents, siblings_per_parent in ((1, 8), (8, 1), (4, 4))
    ]
    return tuple(
        run_forward_timeout_simulation(
            route=route,
            asset_type=asset_type,
            parents=parents,
            siblings_per_parent=siblings_per_parent,
            age_days=age_days,
            delay_s=0.0,
        )
        for route, asset_type, parents, siblings_per_parent, age_days in scenarios
    )


def default_forward_timeout_matrix(*, delay_s: float = 0.0) -> list[AssetSimulationResult]:
    if float(delay_s) == 0.0:
        return list(_default_forward_timeout_matrix_cached())
    scenarios = [
        (route, asset_type, parents, siblings_per_parent, age_days)
        for route in ("public-token", "forward-materialize", "forward-metadata")
        for asset_type in ("video", "file", "speech")
        for age_days in (20, 260)
        for parents, siblings_per_parent in ((1, 8), (8, 1), (4, 4))
    ]
    return [
        run_forward_timeout_simulation(
            route=route,
            asset_type=asset_type,
            parents=parents,
            siblings_per_parent=siblings_per_parent,
            age_days=age_days,
            delay_s=delay_s,
        )
        for route, asset_type, parents, siblings_per_parent, age_days in scenarios
    ]


def summarize_forward_timeout_results(results: list[AssetSimulationResult]) -> dict[str, Any]:
    route_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    age_bucket_counts: Counter[str] = Counter()
    trace_totals: Counter[str] = Counter()
    trace_by_route: dict[str, Counter[str]] = {}
    total_live_timeout = 0.0
    total_breaker_savings = 0.0
    max_backend_calls = 0
    max_timeout_budget = 0.0
    worst_case: AssetSimulationResult | None = None
    storm_risk_count = 0
    short_circuit_help_count = 0
    threshold_counts = {30: 0, 60: 0, 120: 0}
    for item in results:
        route_counts[item.route] += 1
        asset_counts[item.asset_type] += 1
        age_bucket = _age_bucket_label(item.age_days)
        age_bucket_counts[age_bucket] += 1
        total_live_timeout += float(item.equivalent_live_timeout_s)
        for threshold in threshold_counts:
            if float(item.equivalent_live_timeout_s) > float(threshold):
                threshold_counts[threshold] += 1
        total_breaker_savings += max(
            0.0,
            (float(item.total_requests) * float(item.timeout_budget_s))
            - float(item.equivalent_live_timeout_s),
        )
        max_backend_calls = max(max_backend_calls, int(item.backend_timeout_calls))
        max_timeout_budget = max(max_timeout_budget, float(item.timeout_budget_s))
        if item.short_circuited_requests > 0:
            short_circuit_help_count += 1
        if item.parents > 1 and item.siblings_per_parent == 1 and item.backend_timeout_calls == item.total_requests:
            storm_risk_count += 1
        if worst_case is None or float(item.equivalent_live_timeout_s) > float(worst_case.equivalent_live_timeout_s):
            worst_case = item
        for status, count in item.trace_status_breakdown.items():
            trace_totals[status] += int(count)
            route_counter = trace_by_route.setdefault(
                f"{item.route}:{item.asset_type}:{age_bucket}",
                Counter(),
            )
            route_counter[status] += int(count)
    summary = {
        "total": len(results),
        "route_counts": dict(route_counts),
        "asset_type_counts": dict(asset_counts),
        "age_bucket_counts": dict(age_bucket_counts),
        "trace_status_totals": dict(trace_totals),
        "trace_status_by_route": {
            key: dict(counter) for key, counter in trace_by_route.items()
        },
        "equivalent_live_timeout_total_s": round(total_live_timeout, 3),
        "breaker_savings_total_s": round(total_breaker_savings, 3),
        "max_backend_timeout_calls": max_backend_calls,
        "max_timeout_budget_s": round(max_timeout_budget, 3),
        "storm_risk_count": storm_risk_count,
        "short_circuit_help_count": short_circuit_help_count,
        "threshold_counts": {
            f"over_{threshold}s": count for threshold, count in threshold_counts.items()
        },
    }
    if worst_case is not None:
        summary["worst_case"] = {
            "route": worst_case.route,
            "asset_type": worst_case.asset_type,
            "age_days": worst_case.age_days,
            "parents": worst_case.parents,
            "siblings_per_parent": worst_case.siblings_per_parent,
            "equivalent_live_timeout_s": round(float(worst_case.equivalent_live_timeout_s), 3),
            "backend_timeout_calls": int(worst_case.backend_timeout_calls),
            "timeout_budget_s": round(float(worst_case.timeout_budget_s), 3),
        }
    return summary


@dataclass(frozen=True, slots=True)
class PrefetchPlanningScenario:
    name: str
    profile: str
    request_count: int
    old_forward_ratio: float
    duplicate_ratio: float
    local_hit_ratio: float
    eager_remote_ratio: float
    context_only_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PrefetchPlanningResult:
    name: str
    profile: str
    request_count: int
    total_prefetchable: int
    eager_remote_prefetchable: int
    context_only_prefetchable: int
    local_hit_count: int
    old_forward_count: int
    duplicate_shared_key_count: int
    eager_remote_skip_count: int
    remote_workers: int
    public_token_workers: int
    batch_size: int
    batch_timeout_s: float
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _PrefetchPlanningDownloader(NapCatMediaDownloader):
    def _create_prefetch_executors(self) -> None:
        self._public_token_executor = None
        self._remote_loop = None
        self._remote_loop_thread = None
        self._remote_async_client = None
        self._remote_async_semaphore = None
        self._remote_prefetch_runtime_disabled = True
        self._remote_prefetch_runtime_disable_reason = "simulated"

    def _rebuild_prefetch_executors(self, *, wait: bool, recreate: bool) -> None:
        _ = wait, recreate
        return


def _make_prefetch_local_file(root: Path, *, asset_type: str, index: int) -> str:
    suffix = _asset_suffix(asset_type)
    target = root / "local" / asset_type / f"{asset_type}-{index:05d}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(f"local:{asset_type}:{index}".encode("utf-8"))
    return str(target.resolve())


def _build_prefetch_request(
    *,
    root: Path,
    asset_type: str,
    index: int,
    old_forward: bool,
    local_hit: bool,
    eager_remote: bool,
) -> dict[str, Any]:
    suffix = _asset_suffix(asset_type)
    request: dict[str, Any] = {
        "asset_type": asset_type,
        "asset_role": "forward_media" if old_forward else "",
        "file_name": f"{asset_type}-{index:05d}.{suffix}",
        "md5": f"{asset_type[:2]}{index:030x}"[:32],
        "timestamp_ms": _timestamp_ms_for_age_days(260 if old_forward else 20),
        "download_hint": {},
    }
    if old_forward:
        request["download_hint"] = {
            "_forward_parent": {
                "message_id_raw": f"parent_{index // 3:06d}",
                "element_id": f"el_{index // 3:06d}",
                "peer_uid": "u_prefetch",
                "chat_type_raw": "2",
            }
        }
    if local_hit:
        request["source_path"] = _make_prefetch_local_file(root, asset_type=asset_type, index=index)
    elif eager_remote:
        request["download_hint"]["remote_url"] = (
            f"https://assets.example.invalid/{asset_type}/{index:05d}.{suffix}"
        )
    return request


def default_prefetch_planning_scenarios() -> list[PrefetchPlanningScenario]:
    profile_defaults = {
        "recent_image_heavy": (0.0, 0.45, 0.40, 0.15),
        "old_forward_video_heavy": (0.80, 0.05, 0.10, 0.85),
        "token_heavy_low_yield": (0.45, 0.08, 0.02, 0.90),
        "mixed_realistic_large_window": (0.35, 0.25, 0.30, 0.45),
    }
    scenarios: list[PrefetchPlanningScenario] = []
    for profile, (old_ratio, duplicate_ratio, local_ratio, eager_ratio) in profile_defaults.items():
        context_ratio = max(0.0, 1.0 - local_ratio - eager_ratio)
        for request_count in (32, 256, 1024, 4096, 16384):
            scenarios.append(
                PrefetchPlanningScenario(
                    name=f"{profile}_{request_count}",
                    profile=profile,
                    request_count=request_count,
                    old_forward_ratio=old_ratio,
                    duplicate_ratio=duplicate_ratio,
                    local_hit_ratio=local_ratio,
                    eager_remote_ratio=eager_ratio,
                    context_only_ratio=context_ratio,
                )
            )
    return scenarios


def run_prefetch_planning_scenario(
    scenario: PrefetchPlanningScenario,
) -> PrefetchPlanningResult:
    def _prefix_count(total: int, ratio: float) -> int:
        if total <= 0 or ratio <= 0.0:
            return 0
        if ratio >= 1.0:
            return total
        return min(total, max(0, int(math.ceil((ratio * total) - 1e-12))))

    request_count = max(1, int(scenario.request_count))
    unique_count = max(1, int(round(request_count * (1.0 - scenario.duplicate_ratio))))
    duplicate_count = max(0, request_count - unique_count)

    local_unique = _prefix_count(unique_count, scenario.local_hit_ratio)
    old_unique = _prefix_count(unique_count, scenario.old_forward_ratio)
    eager_unique = max(
        0,
        _prefix_count(unique_count, scenario.local_hit_ratio + scenario.eager_remote_ratio) - local_unique,
    )
    local_duplicates = min(duplicate_count, local_unique)
    old_duplicates = min(duplicate_count, old_unique)
    eager_duplicates = max(0, min(duplicate_count, local_unique + eager_unique) - local_duplicates)

    local_hit_count = local_unique + local_duplicates
    old_forward_count = old_unique + old_duplicates
    eager_remote_prefetchable = eager_unique + eager_duplicates
    total_prefetchable = max(0, request_count - local_hit_count)
    context_only_prefetchable = max(0, total_prefetchable - eager_remote_prefetchable)
    eager_remote_skip_count = (
        eager_remote_prefetchable
        if scenario.old_forward_ratio >= 0.5
        else max(0, min(eager_remote_prefetchable, old_forward_count // 2))
    )
    duplicate_shared_key_count = duplicate_count
    batch_size = NapCatMediaDownloader._prefetch_batch_size_for_request_count(
        NapCatMediaDownloader,
        request_count,
    )
    batch_timeout_s = NapCatMediaDownloader._prefetch_batch_timeout_s(
        NapCatMediaDownloader,
        batch_size,
        request_count,
    )
    remote_workers = NapCatMediaDownloader._compute_remote_media_fetch_workers(
        total_prefetchable=total_prefetchable,
        eager_remote_prefetchable=eager_remote_prefetchable,
        feedback={},
    )
    public_token_workers = NapCatMediaDownloader._compute_public_token_prefetch_workers(
        total_prefetchable=total_prefetchable,
        eager_remote_prefetchable=eager_remote_prefetchable,
        feedback={},
    )
    chunk_count = max(1, int(math.ceil(request_count / float(max(1, batch_size)))))
    return PrefetchPlanningResult(
        name=scenario.name,
        profile=scenario.profile,
        request_count=request_count,
        total_prefetchable=total_prefetchable,
        eager_remote_prefetchable=eager_remote_prefetchable,
        context_only_prefetchable=context_only_prefetchable,
        local_hit_count=local_hit_count,
        old_forward_count=old_forward_count,
        duplicate_shared_key_count=duplicate_shared_key_count,
        eager_remote_skip_count=eager_remote_skip_count,
        remote_workers=remote_workers,
        public_token_workers=public_token_workers,
        batch_size=batch_size,
        batch_timeout_s=batch_timeout_s,
        notes=f"progress_events={chunk_count * 2} profile={scenario.profile}",
    )


@lru_cache(maxsize=1)
def _run_prefetch_planning_matrix_cached() -> tuple[PrefetchPlanningResult, ...]:
    return tuple(run_prefetch_planning_scenario(item) for item in default_prefetch_planning_scenarios())


def run_prefetch_planning_matrix() -> list[PrefetchPlanningResult]:
    return list(_run_prefetch_planning_matrix_cached())


def summarize_prefetch_planning_results(
    results: list[PrefetchPlanningResult],
) -> dict[str, Any]:
    profile_counts: Counter[str] = Counter()
    total_prefetchable = 0
    eager_remote_total = 0
    context_only_total = 0
    local_hits_total = 0
    old_forward_total = 0
    duplicate_shared_key_total = 0
    eager_remote_skip_total = 0
    max_batch_size = 0
    large_window_case_count = 0
    large_window_batch_size_min: int | None = None
    large_window_batch_size_max = 0
    max_remote_workers = 0
    max_public_token_workers = 0
    worst_case: PrefetchPlanningResult | None = None
    for item in results:
        profile_counts[item.profile] += 1
        total_prefetchable += item.total_prefetchable
        eager_remote_total += item.eager_remote_prefetchable
        context_only_total += item.context_only_prefetchable
        local_hits_total += item.local_hit_count
        old_forward_total += item.old_forward_count
        duplicate_shared_key_total += item.duplicate_shared_key_count
        eager_remote_skip_total += item.eager_remote_skip_count
        max_batch_size = max(max_batch_size, item.batch_size)
        if item.request_count >= NapCatMediaDownloader.PREFETCH_LARGE_REQUEST_THRESHOLD:
            large_window_case_count += 1
            large_window_batch_size_max = max(large_window_batch_size_max, item.batch_size)
            if large_window_batch_size_min is None:
                large_window_batch_size_min = item.batch_size
            else:
                large_window_batch_size_min = min(large_window_batch_size_min, item.batch_size)
        max_remote_workers = max(max_remote_workers, item.remote_workers)
        max_public_token_workers = max(max_public_token_workers, item.public_token_workers)
        if worst_case is None or item.total_prefetchable > worst_case.total_prefetchable:
            worst_case = item
    summary = {
        "total": len(results),
        "profile_counts": dict(profile_counts),
        "total_prefetchable": total_prefetchable,
        "eager_remote_total": eager_remote_total,
        "context_only_total": context_only_total,
        "local_hits_total": local_hits_total,
        "old_forward_total": old_forward_total,
        "duplicate_shared_key_total": duplicate_shared_key_total,
        "eager_remote_skip_total": eager_remote_skip_total,
        "max_batch_size": max_batch_size,
        "large_window_case_count": large_window_case_count,
        "large_window_batch_size_min": large_window_batch_size_min,
        "large_window_batch_size_max": large_window_batch_size_max,
        "max_remote_workers": max_remote_workers,
        "max_public_token_workers": max_public_token_workers,
    }
    if worst_case is not None:
        summary["worst_case"] = worst_case.to_dict()
    return summary


@dataclass(frozen=True, slots=True)
class ForwardCandidatePriorityCase:
    name: str
    asset_type: str
    profile: str
    primary_signals: tuple[str, ...]
    primary_recoverability: str
    decoy_signals: tuple[str, ...]
    decoy_recoverability: str
    expected_winner: str = "primary"
    expected_path_kind: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ForwardCandidatePriorityResult:
    name: str
    asset_type: str
    profile: str
    expected_winner: str
    expected_path_kind: str
    actual_winner: str | None
    matched: bool
    resolver: str | None
    path_kind: str
    primary_signals: tuple[str, ...]
    primary_recoverability: str
    decoy_signals: tuple[str, ...]
    decoy_recoverability: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SharedOutcomeScopeCase:
    name: str
    asset_type: str
    topology: str
    identity_mode: str
    expected_same_key: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SharedOutcomeScopeResult:
    name: str
    asset_type: str
    topology: str
    identity_mode: str
    expected_same_key: bool
    actual_same_key: bool
    matched: bool
    key_a: tuple[Any, ...] | None
    key_b: tuple[Any, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PublicTimeoutScopeCase:
    name: str
    asset_type: str
    relationship: str
    expected_same_key: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PublicTimeoutScopeResult:
    name: str
    asset_type: str
    relationship: str
    expected_same_key: bool
    actual_same_key: bool
    matched: bool
    key_a: tuple[str, ...] | None
    key_b: tuple[str, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ForwardParentPublicTimeoutScopeCase:
    name: str
    asset_type: str
    relationship: str
    age_days: int
    expected_same_key: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ForwardParentPublicTimeoutScopeResult:
    name: str
    asset_type: str
    relationship: str
    age_days: int
    expected_same_key: bool
    actual_same_key: bool
    matched: bool
    key_a: tuple[str, ...] | None
    key_b: tuple[str, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _CandidatePriorityDownloader(NapCatMediaDownloader):
    def __init__(
        self,
        *,
        token_paths: dict[tuple[str, str], str],
        remote_paths: dict[str, str],
    ) -> None:
        self._candidate_token_paths = token_paths
        self._candidate_remote_paths = remote_paths
        super().__init__(_DummyClient())

    def _create_prefetch_executors(self) -> None:
        self._public_token_executor = None
        self._remote_loop = None
        self._remote_loop_thread = None
        self._remote_async_client = None
        self._remote_async_semaphore = None

    def _rebuild_prefetch_executors(self, *, wait: bool, recreate: bool) -> None:
        _ = wait, recreate
        return

    def _resolve_from_public_token(  # type: ignore[override]
        self,
        data: dict[str, Any] | None,
        *,
        old_bucket: tuple[str, str] | None = None,
        expired_candidate: bool = False,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None] | None:
        _ = old_bucket, expired_candidate, request, trace_callback
        if not isinstance(data, dict):
            return None
        action = str(data.get("public_action") or "").strip().lower()
        token = str(data.get("public_file_token") or "").strip()
        path_text = self._candidate_token_paths.get((action, token))
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists() or not path.is_file():
            return None
        return path.resolve(), f"napcat_public_token_{action}"

    def _download_remote_media(  # type: ignore[override]
        self,
        *,
        asset_type: str,
        file_name: str | None,
        hint: dict[str, Any],
    ) -> str | None:
        _ = asset_type, file_name
        resolved_remote_url = self._resolve_remote_url(str(hint.get("url") or "").strip())
        if not resolved_remote_url:
            return None
        return self._candidate_remote_paths.get(resolved_remote_url)


def _candidate_suffix(asset_type: str) -> str:
    return {
        "image": "jpg",
        "video": "mp4",
        "file": "bin",
        "speech": "mp3",
    }.get(asset_type, "dat")


def _candidate_action(asset_type: str) -> str:
    return "get_record" if asset_type == "speech" else "get_file" if asset_type in {"video", "file"} else "get_image"


def _recoverability_path_kind(recoverability: str) -> str:
    normalized = str(recoverability or "").strip().lower()
    if normalized in {"local", "remote", "public"}:
        return normalized
    return "missing"


def _candidate_request(asset_type: str) -> dict[str, Any]:
    suffix = _candidate_suffix(asset_type)
    return {
        "asset_type": asset_type,
        "asset_role": "forward_media",
        "file_name": f"target.asset.{suffix}",
        "md5": f"{asset_type}-md5-target",
        "download_hint": {
            "_forward_parent": {
                "message_id_raw": f"parent_{asset_type}",
                "element_id": f"element_{asset_type}",
                "peer_uid": "u_candidate",
                "chat_type_raw": "2",
            },
            "file_id": f"/fileid/{asset_type}/target",
            "remote_url": f"https://assets.example.invalid/{asset_type}/target.asset.{suffix}",
            "file_biz_id": f"biz-{asset_type}-target",
        },
    }


def _candidate_test_file(root: Path, *, name: str) -> str:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(f"candidate:{name}".encode("utf-8"))
    return str(target.resolve())


def _candidate_asset_payload(
    *,
    root: Path,
    asset_type: str,
    label: str,
    signals: tuple[str, ...],
    recoverability: str,
    request: dict[str, Any],
    token_paths: dict[tuple[str, str], str],
    remote_paths: dict[str, str],
) -> dict[str, Any]:
    suffix = _candidate_suffix(asset_type)
    hint = request.get("download_hint") if isinstance(request.get("download_hint"), dict) else {}
    request_file_name = str(request.get("file_name") or "").strip()
    request_md5 = str(request.get("md5") or "").strip()
    request_file_id = str(hint.get("file_id") or "").strip()
    request_remote_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
    request_file_biz_id = str(hint.get("file_biz_id") or request.get("file_biz_id") or "").strip()
    payload: dict[str, Any] = {
        "asset_type": asset_type,
        "asset_role": "forward_media",
        "_candidate_label": label,
        "file_name": f"{label}.{suffix}",
        "md5": f"{asset_type}-md5-{label}",
        "file_id": f"/fileid/{asset_type}/{label}",
        "file_biz_id": f"biz-{asset_type}-{label}",
    }
    if "file_name" in signals:
        payload["file_name"] = request_file_name
    elif "stem" in signals:
        payload["file_name"] = f"target.asset.alt.{suffix}"
    if "md5" in signals:
        payload["md5"] = request_md5
    if "file_id" in signals:
        payload["file_id"] = request_file_id
    if "file_biz_id" in signals:
        payload["file_biz_id"] = request_file_biz_id
    if "url" in signals:
        payload["remote_url"] = request_remote_url
        payload["url"] = request_remote_url
    if recoverability == "local":
        payload["file"] = _candidate_test_file(root, name=f"local/{label}.{suffix}")
    elif recoverability == "remote":
        remote_url = (
            request_remote_url
            if "url" in signals
            else f"https://assets.example.invalid/{asset_type}/{label}.{suffix}"
        )
        payload["remote_url"] = remote_url
        payload["url"] = remote_url
        remote_paths[remote_url] = _candidate_test_file(root, name=f"remote/{label}.{suffix}")
    elif recoverability == "public":
        token = f"token-{asset_type}-{label}"
        payload["public_action"] = _candidate_action(asset_type)
        payload["public_file_token"] = token
        token_paths[(payload["public_action"], token)] = _candidate_test_file(
            root,
            name=f"public/{label}.{suffix}",
        )
    return payload


def default_forward_candidate_priority_cases() -> list[ForwardCandidatePriorityCase]:
    cases: list[ForwardCandidatePriorityCase] = []
    recoverability_order = ("local", "remote", "public", "blank")
    for asset_type in ("image", "video", "file", "speech"):
        for index, primary_recoverability in enumerate(recoverability_order):
            for decoy_recoverability in recoverability_order[index + 1 :]:
                cases.append(
                    ForwardCandidatePriorityCase(
                        name=f"{asset_type}_tiebreak_{primary_recoverability}_over_{decoy_recoverability}",
                        asset_type=asset_type,
                        profile="recoverability_tiebreak",
                        primary_signals=("file_name",),
                        primary_recoverability=primary_recoverability,
                        decoy_signals=("file_name",),
                        decoy_recoverability=decoy_recoverability,
                        expected_path_kind=_recoverability_path_kind(primary_recoverability),
                    )
                )
        cases.extend(
            [
                ForwardCandidatePriorityCase(
                    name=f"{asset_type}_signal_md5_over_filename",
                    asset_type=asset_type,
                    profile="signal_priority",
                    primary_signals=("md5",),
                    primary_recoverability="public",
                    decoy_signals=("file_name",),
                    decoy_recoverability="local",
                    expected_path_kind="public",
                ),
                ForwardCandidatePriorityCase(
                    name=f"{asset_type}_signal_file_id_over_filename",
                    asset_type=asset_type,
                    profile="signal_priority",
                    primary_signals=("file_id",),
                    primary_recoverability="public",
                    decoy_signals=("file_name",),
                    decoy_recoverability="local",
                    expected_path_kind="public",
                ),
                ForwardCandidatePriorityCase(
                    name=f"{asset_type}_signal_url_over_filename",
                    asset_type=asset_type,
                    profile="signal_priority",
                    primary_signals=("url",),
                    primary_recoverability="remote",
                    decoy_signals=("file_name",),
                    decoy_recoverability="local",
                    expected_path_kind="remote",
                ),
                ForwardCandidatePriorityCase(
                    name=f"{asset_type}_signal_filename_over_stem",
                    asset_type=asset_type,
                    profile="signal_priority",
                    primary_signals=("file_name",),
                    primary_recoverability="public",
                    decoy_signals=("stem",),
                    decoy_recoverability="local",
                    expected_path_kind="public",
                ),
            ]
        )
    for asset_type in ("video", "file"):
        cases.append(
            ForwardCandidatePriorityCase(
                name=f"{asset_type}_signal_file_biz_id_over_filename",
                asset_type=asset_type,
                profile="signal_priority",
                primary_signals=("file_biz_id",),
                primary_recoverability="public",
                decoy_signals=("file_name",),
                decoy_recoverability="local",
                expected_path_kind="public",
            )
        )
    return cases


def run_forward_candidate_priority_case(
    case: ForwardCandidatePriorityCase,
) -> ForwardCandidatePriorityResult:
    repo_root = Path(__file__).resolve().parents[3]
    temp_root = repo_root / ".tmp" / "asset_simulator_candidates" / case.name
    shutil.rmtree(temp_root, ignore_errors=True)
    temp_root.mkdir(parents=True, exist_ok=True)
    token_paths: dict[tuple[str, str], str] = {}
    remote_paths: dict[str, str] = {}
    request = _candidate_request(case.asset_type)
    downloader = _CandidatePriorityDownloader(
        token_paths=token_paths,
        remote_paths=remote_paths,
    )
    try:
        primary = _candidate_asset_payload(
            root=temp_root,
            asset_type=case.asset_type,
            label="primary",
            signals=case.primary_signals,
            recoverability=case.primary_recoverability,
            request=request,
            token_paths=token_paths,
            remote_paths=remote_paths,
        )
        decoy = _candidate_asset_payload(
            root=temp_root,
            asset_type=case.asset_type,
            label="decoy",
            signals=case.decoy_signals,
            recoverability=case.decoy_recoverability,
            request=request,
            token_paths=token_paths,
            remote_paths=remote_paths,
        )
        resolved, matched_payload = downloader._pick_forward_asset_match(
            request,
            [decoy, primary],
        )
        actual_winner = (
            str(matched_payload.get("_candidate_label") or "").strip() or None
            if isinstance(matched_payload, dict)
            else None
        )
        path_kind = "missing"
        resolved_tuple = (
            resolved
            if isinstance(resolved, tuple) and len(resolved) == 2
            else (None, None)
        )
        if resolved_tuple[0] is not None:
            resolver = str(resolved_tuple[1] or "").strip() or None
            if resolver == "napcat_forward_hydrated":
                path_kind = "local"
            elif "remote_url" in str(resolver or ""):
                path_kind = "remote"
            elif "public_token" in str(resolver or ""):
                path_kind = "public"
            else:
                path_kind = "local"
        else:
            resolver = None
        expected_path_kind = case.expected_path_kind or _recoverability_path_kind(
            case.primary_recoverability if case.expected_winner == "primary" else case.decoy_recoverability
        )
        return ForwardCandidatePriorityResult(
            name=case.name,
            asset_type=case.asset_type,
            profile=case.profile,
            expected_winner=case.expected_winner,
            expected_path_kind=expected_path_kind,
            actual_winner=actual_winner,
            matched=(
                actual_winner == case.expected_winner
                and path_kind == expected_path_kind
            ),
            resolver=resolver,
            path_kind=path_kind,
            primary_signals=case.primary_signals,
            primary_recoverability=case.primary_recoverability,
            decoy_signals=case.decoy_signals,
            decoy_recoverability=case.decoy_recoverability,
        )
    finally:
        downloader.close()
        shutil.rmtree(temp_root, ignore_errors=True)


def run_forward_candidate_priority_matrix() -> list[ForwardCandidatePriorityResult]:
    return [
        run_forward_candidate_priority_case(item)
        for item in default_forward_candidate_priority_cases()
    ]


def summarize_forward_candidate_priority_results(
    results: list[ForwardCandidatePriorityResult],
) -> dict[str, Any]:
    profile_counts: Counter[str] = Counter()
    asset_type_counts: Counter[str] = Counter()
    resolver_counts: Counter[str] = Counter()
    path_kind_counts: Counter[str] = Counter()
    mismatches: list[str] = []
    for item in results:
        profile_counts[item.profile] += 1
        asset_type_counts[item.asset_type] += 1
        resolver_counts[str(item.resolver or "<none>")] += 1
        path_kind_counts[item.path_kind] += 1
        if not item.matched:
            mismatches.append(item.name)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "profile_counts": dict(profile_counts),
        "asset_type_counts": dict(asset_type_counts),
        "resolver_counts": dict(resolver_counts),
        "path_kind_counts": dict(path_kind_counts),
        "mismatch_names": mismatches,
    }


def _scope_request(
    *,
    asset_type: str,
    topology: str,
    identity_mode: str,
    variant: str,
) -> dict[str, Any]:
    suffix = _candidate_suffix(asset_type)
    request: dict[str, Any] = {
        "asset_type": asset_type,
        "asset_role": "forward_media" if topology == "forward" else "",
        "file_name": "" if identity_mode == "none" else f"scope-target-{asset_type}.{suffix}",
        "md5": "",
        "source_path": "",
    }
    hint: dict[str, Any] = {}
    if topology == "forward":
        hint["_forward_parent"] = {
            "message_id_raw": "parent-shared-scope",
            "element_id": "element-shared-scope",
            "peer_uid": "u-shared-scope",
            "chat_type_raw": "2",
        }
    if identity_mode == "md5":
        request["md5"] = f"scope-md5-{asset_type}"
    elif identity_mode == "remote_url":
        hint["remote_url"] = f"https://assets.example.invalid/shared/{asset_type}/{variant}.bin"
    elif identity_mode == "remote_url_same":
        hint["remote_url"] = f"https://assets.example.invalid/shared/{asset_type}/shared.bin"
    elif identity_mode == "file_id":
        hint["file_id"] = f"/scope/{asset_type}/shared"
    elif identity_mode == "source_leaf":
        request["source_path"] = f"C:\\QQ\\cache\\{asset_type}\\shared-{asset_type}.bin"
    elif identity_mode == "file_name_only":
        pass
    elif identity_mode == "none":
        request["file_name"] = ""
    else:
        raise ValueError(f"unsupported identity_mode: {identity_mode}")
    if hint:
        request["download_hint"] = hint
    return request


def default_shared_outcome_scope_cases() -> list[SharedOutcomeScopeCase]:
    cases: list[SharedOutcomeScopeCase] = []
    for asset_type in ("image", "video", "file", "speech"):
        for topology in ("top_level", "forward"):
            cases.append(
                SharedOutcomeScopeCase(
                    name=f"{asset_type}_{topology}_file_name_only",
                    asset_type=asset_type,
                    topology=topology,
                    identity_mode="file_name_only",
                    expected_same_key=not (
                        topology == "forward" and asset_type in {"video", "file", "speech"}
                    ),
                )
            )
            for identity_mode in ("md5", "file_id", "source_leaf"):
                cases.append(
                    SharedOutcomeScopeCase(
                        name=f"{asset_type}_{topology}_{identity_mode}",
                        asset_type=asset_type,
                        topology=topology,
                        identity_mode=identity_mode,
                        expected_same_key=True,
                    )
                )
            cases.append(
                SharedOutcomeScopeCase(
                    name=f"{asset_type}_{topology}_remote_url_same",
                    asset_type=asset_type,
                    topology=topology,
                    identity_mode="remote_url_same",
                    expected_same_key=True,
                )
            )
            cases.append(
                SharedOutcomeScopeCase(
                    name=f"{asset_type}_{topology}_none",
                    asset_type=asset_type,
                    topology=topology,
                    identity_mode="none",
                    expected_same_key=False,
                )
            )
    return cases


def _shared_request_key_for_request(request: dict[str, Any]) -> tuple[Any, ...] | None:
    hint = NapCatMediaDownloader._request_hint(request)
    asset_type = str(request.get("asset_type") or "").strip()
    file_name = str(request.get("file_name") or "").strip().lower()
    md5 = str(request.get("md5") or "").strip().lower()
    source_leaf = ""
    source_path = str(request.get("source_path") or "").strip()
    if source_path:
        source_leaf = PureWindowsPath(source_path).name.strip().lower()
    file_id = str(hint.get("file_id") or "").strip()
    remote_url = NapCatMediaDownloader._normalized_match_url(
        hint.get("remote_url") or hint.get("url")
    )
    if NapCatMediaDownloader._has_forward_parent_hint(hint) and asset_type in {"file", "video", "speech"}:
        strong_forward_identity = any([md5, source_leaf, file_id, remote_url])
        if not strong_forward_identity:
            return None
    if not any([file_name, md5, source_leaf, file_id, remote_url]):
        return None
    return (
        asset_type,
        str(request.get("asset_role") or "").strip(),
        file_name,
        md5,
        source_leaf,
        file_id,
        remote_url,
    )


def run_shared_outcome_scope_case(
    case: SharedOutcomeScopeCase,
) -> SharedOutcomeScopeResult:
    request_a = _scope_request(
        asset_type=case.asset_type,
        topology=case.topology,
        identity_mode=case.identity_mode,
        variant="a",
    )
    request_b = _scope_request(
        asset_type=case.asset_type,
        topology=case.topology,
        identity_mode=case.identity_mode,
        variant="b",
    )
    if case.identity_mode == "remote_url_same":
        request_a = _scope_request(
            asset_type=case.asset_type,
            topology=case.topology,
            identity_mode="remote_url_same",
            variant="a",
        )
        request_b = _scope_request(
            asset_type=case.asset_type,
            topology=case.topology,
            identity_mode="remote_url_same",
            variant="b",
        )
    key_a = _shared_request_key_for_request(request_a)
    key_b = _shared_request_key_for_request(request_b)
    actual_same_key = bool(key_a and key_b and key_a == key_b)
    return SharedOutcomeScopeResult(
        name=case.name,
        asset_type=case.asset_type,
        topology=case.topology,
        identity_mode=case.identity_mode,
        expected_same_key=case.expected_same_key,
        actual_same_key=actual_same_key,
        matched=actual_same_key == case.expected_same_key,
        key_a=key_a,
        key_b=key_b,
    )


@lru_cache(maxsize=1)
def _run_shared_outcome_scope_matrix_cached() -> tuple[SharedOutcomeScopeResult, ...]:
    return tuple(
        run_shared_outcome_scope_case(case)
        for case in default_shared_outcome_scope_cases()
    )


def run_shared_outcome_scope_matrix() -> list[SharedOutcomeScopeResult]:
    return list(_run_shared_outcome_scope_matrix_cached())


def summarize_shared_outcome_scope_results(
    results: list[SharedOutcomeScopeResult],
) -> dict[str, Any]:
    asset_type_counts: Counter[str] = Counter()
    topology_counts: Counter[str] = Counter()
    identity_mode_counts: Counter[str] = Counter()
    mismatches: list[str] = []
    for item in results:
        asset_type_counts[item.asset_type] += 1
        topology_counts[item.topology] += 1
        identity_mode_counts[item.identity_mode] += 1
        if not item.matched:
            mismatches.append(item.name)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "asset_type_counts": dict(asset_type_counts),
        "topology_counts": dict(topology_counts),
        "identity_mode_counts": dict(identity_mode_counts),
        "mismatch_names": mismatches,
    }


def _timeout_scope_request(
    *,
    asset_type: str,
    parent_id: str,
    token: str,
    file_name: str,
    md5: str,
    file_id: str,
    forward: bool = True,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "asset_type": asset_type,
        "asset_role": "forward_media",
        "file_name": file_name,
        "md5": md5,
        "download_hint": {
            "file_id": file_id,
        },
    }
    if forward:
        request["download_hint"]["_forward_parent"] = {
            "message_id_raw": parent_id,
            "element_id": f"element:{parent_id}",
            "peer_uid": "u-timeout-scope",
            "chat_type_raw": "2",
        }
    request["download_hint"]["public_file_token"] = token
    return request


def default_public_timeout_scope_cases() -> list[PublicTimeoutScopeCase]:
    cases: list[PublicTimeoutScopeCase] = []
    for asset_type in ("image", "video", "file", "speech"):
        cases.extend(
            [
                PublicTimeoutScopeCase(
                    name=f"{asset_type}_same_parent_same_token_same_request",
                    asset_type=asset_type,
                    relationship="same_parent_same_token_same_request",
                    expected_same_key=True,
                ),
                PublicTimeoutScopeCase(
                    name=f"{asset_type}_same_parent_new_token",
                    asset_type=asset_type,
                    relationship="same_parent_new_token",
                    expected_same_key=(asset_type == "image"),
                ),
                PublicTimeoutScopeCase(
                    name=f"{asset_type}_same_parent_same_token_new_file",
                    asset_type=asset_type,
                    relationship="same_parent_same_token_new_file",
                    expected_same_key=(asset_type == "image"),
                ),
                PublicTimeoutScopeCase(
                    name=f"{asset_type}_different_parent_same_token",
                    asset_type=asset_type,
                    relationship="different_parent_same_token",
                    expected_same_key=False,
                ),
                PublicTimeoutScopeCase(
                    name=f"{asset_type}_non_forward_ignored",
                    asset_type=asset_type,
                    relationship="non_forward_ignored",
                    expected_same_key=False,
                ),
            ]
        )
    return cases


def run_public_timeout_scope_case(
    case: PublicTimeoutScopeCase,
) -> PublicTimeoutScopeResult:
    if case.asset_type == "speech":
        action = "get_record"
    elif case.asset_type == "image":
        action = "get_image"
    else:
        action = "get_file"
    request_a = _timeout_scope_request(
        asset_type=case.asset_type,
        parent_id="parent-a",
        token="token-a",
        file_name=f"{case.asset_type}-a.bin",
        md5=f"{case.asset_type}-md5-a",
        file_id=f"/scope/{case.asset_type}/a",
        forward=True,
    )
    request_b = _timeout_scope_request(
        asset_type=case.asset_type,
        parent_id="parent-a",
        token="token-a",
        file_name=f"{case.asset_type}-a.bin",
        md5=f"{case.asset_type}-md5-a",
        file_id=f"/scope/{case.asset_type}/a",
        forward=True,
    )
    if case.relationship == "same_parent_new_token":
        request_b["download_hint"]["public_file_token"] = "token-b"
    elif case.relationship == "same_parent_same_token_new_file":
        request_b["file_name"] = f"{case.asset_type}-b.bin"
        request_b["md5"] = f"{case.asset_type}-md5-b"
        if case.asset_type == "image":
            request_b["download_hint"]["file_id"] = "token-b"
        else:
            request_b["download_hint"]["file_id"] = f"/scope/{case.asset_type}/b"
    elif case.relationship == "different_parent_same_token":
        request_b["download_hint"]["_forward_parent"]["message_id_raw"] = "parent-b"
        request_b["download_hint"]["_forward_parent"]["element_id"] = "element:parent-b"
    elif case.relationship == "non_forward_ignored":
        request_a = _timeout_scope_request(
            asset_type=case.asset_type,
            parent_id="parent-a",
            token="token-a",
            file_name=f"{case.asset_type}-a.bin",
            md5=f"{case.asset_type}-md5-a",
            file_id=f"/scope/{case.asset_type}/a",
            forward=False,
        )
        request_b = dict(request_a)
    key_a = NapCatMediaDownloader._request_scoped_public_action_timeout_key(
        request_a,
        action=action,
        token=str(request_a.get("download_hint", {}).get("public_file_token") or "").strip(),
    )
    key_b = NapCatMediaDownloader._request_scoped_public_action_timeout_key(
        request_b,
        action=action,
        token=str(request_b.get("download_hint", {}).get("public_file_token") or "").strip(),
    )
    actual_same_key = bool(key_a and key_b and key_a == key_b)
    return PublicTimeoutScopeResult(
        name=case.name,
        asset_type=case.asset_type,
        relationship=case.relationship,
        expected_same_key=case.expected_same_key,
        actual_same_key=actual_same_key,
        matched=actual_same_key == case.expected_same_key,
        key_a=key_a,
        key_b=key_b,
    )


@lru_cache(maxsize=1)
def _run_public_timeout_scope_matrix_cached() -> tuple[PublicTimeoutScopeResult, ...]:
    return tuple(
        run_public_timeout_scope_case(case)
        for case in default_public_timeout_scope_cases()
    )


def run_public_timeout_scope_matrix() -> list[PublicTimeoutScopeResult]:
    return list(_run_public_timeout_scope_matrix_cached())


def summarize_public_timeout_scope_results(
    results: list[PublicTimeoutScopeResult],
) -> dict[str, Any]:
    asset_type_counts: Counter[str] = Counter()
    relationship_counts: Counter[str] = Counter()
    mismatches: list[str] = []
    for item in results:
        asset_type_counts[item.asset_type] += 1
        relationship_counts[item.relationship] += 1
        if not item.matched:
            mismatches.append(item.name)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "asset_type_counts": dict(asset_type_counts),
        "relationship_counts": dict(relationship_counts),
        "mismatch_names": mismatches,
    }


def default_forward_parent_public_timeout_scope_cases() -> list[ForwardParentPublicTimeoutScopeCase]:
    cases: list[ForwardParentPublicTimeoutScopeCase] = []
    for asset_type in ("video", "file"):
        cases.extend(
            [
                ForwardParentPublicTimeoutScopeCase(
                    name=f"{asset_type}_aged_same_parent_new_token",
                    asset_type=asset_type,
                    relationship="same_parent_new_token",
                    age_days=90,
                    expected_same_key=True,
                ),
                ForwardParentPublicTimeoutScopeCase(
                    name=f"{asset_type}_aged_same_parent_same_token_new_file",
                    asset_type=asset_type,
                    relationship="same_parent_same_token_new_file",
                    age_days=90,
                    expected_same_key=True,
                ),
                ForwardParentPublicTimeoutScopeCase(
                    name=f"{asset_type}_aged_different_parent_same_token",
                    asset_type=asset_type,
                    relationship="different_parent_same_token",
                    age_days=90,
                    expected_same_key=False,
                ),
                ForwardParentPublicTimeoutScopeCase(
                    name=f"{asset_type}_recent_same_parent_new_token",
                    asset_type=asset_type,
                    relationship="same_parent_new_token",
                    age_days=7,
                    expected_same_key=False,
                ),
            ]
        )
    return cases


def run_forward_parent_public_timeout_scope_case(
    case: ForwardParentPublicTimeoutScopeCase,
) -> ForwardParentPublicTimeoutScopeResult:
    request_a = _timeout_scope_request(
        asset_type=case.asset_type,
        parent_id="parent-a",
        token="token-a",
        file_name=f"{case.asset_type}-a.bin",
        md5=f"{case.asset_type}-md5-a",
        file_id=f"/scope/{case.asset_type}/a",
        forward=True,
    )
    request_b = _timeout_scope_request(
        asset_type=case.asset_type,
        parent_id="parent-a",
        token="token-a",
        file_name=f"{case.asset_type}-a.bin",
        md5=f"{case.asset_type}-md5-a",
        file_id=f"/scope/{case.asset_type}/a",
        forward=True,
    )
    request_a["timestamp_ms"] = _timestamp_ms_for_age_days(case.age_days)
    request_b["timestamp_ms"] = _timestamp_ms_for_age_days(case.age_days)
    if case.relationship == "same_parent_new_token":
        request_b["download_hint"]["public_file_token"] = "token-b"
    elif case.relationship == "same_parent_same_token_new_file":
        request_b["file_name"] = f"{case.asset_type}-b.bin"
        request_b["md5"] = f"{case.asset_type}-md5-b"
        request_b["download_hint"]["file_id"] = f"/scope/{case.asset_type}/b"
    elif case.relationship == "different_parent_same_token":
        request_b["download_hint"]["_forward_parent"]["message_id_raw"] = "parent-b"
        request_b["download_hint"]["_forward_parent"]["element_id"] = "element:parent-b"
    key_a = NapCatMediaDownloader._forward_parent_scoped_public_action_timeout_key(
        request_a,
        action="get_file",
    )
    key_b = NapCatMediaDownloader._forward_parent_scoped_public_action_timeout_key(
        request_b,
        action="get_file",
    )
    actual_same_key = bool(key_a and key_b and key_a == key_b)
    return ForwardParentPublicTimeoutScopeResult(
        name=case.name,
        asset_type=case.asset_type,
        relationship=case.relationship,
        age_days=case.age_days,
        expected_same_key=case.expected_same_key,
        actual_same_key=actual_same_key,
        matched=actual_same_key == case.expected_same_key,
        key_a=key_a,
        key_b=key_b,
    )


@lru_cache(maxsize=1)
def _run_forward_parent_public_timeout_scope_matrix_cached() -> tuple[ForwardParentPublicTimeoutScopeResult, ...]:
    return tuple(
        run_forward_parent_public_timeout_scope_case(case)
        for case in default_forward_parent_public_timeout_scope_cases()
    )


def run_forward_parent_public_timeout_scope_matrix() -> list[ForwardParentPublicTimeoutScopeResult]:
    return list(_run_forward_parent_public_timeout_scope_matrix_cached())


def summarize_forward_parent_public_timeout_scope_results(
    results: list[ForwardParentPublicTimeoutScopeResult],
) -> dict[str, Any]:
    asset_type_counts: Counter[str] = Counter()
    relationship_counts: Counter[str] = Counter()
    age_bucket_counts: Counter[str] = Counter()
    mismatches: list[str] = []
    for item in results:
        asset_type_counts[item.asset_type] += 1
        relationship_counts[item.relationship] += 1
        age_bucket_counts[_age_bucket_label(item.age_days)] += 1
        if not item.matched:
            mismatches.append(item.name)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "asset_type_counts": dict(asset_type_counts),
        "relationship_counts": dict(relationship_counts),
        "age_bucket_counts": dict(age_bucket_counts),
        "mismatch_names": mismatches,
    }


def _retarget_simulation_clients(
    downloader: "_ScenarioAwareDownloader",
    client: "_ScenarioPublicClient",
    fast_client: "_ScenarioFastClient",
    runtime: "_ScenarioRuntimeState",
    scenario: "AssetResolutionScenario",
) -> None:
    downloader._scenario_state = runtime
    client._scenario = scenario
    client._state = runtime
    fast_client._scenario = scenario
    fast_client._state = runtime


def default_asset_resolution_pair_cases() -> list[AssetResolutionPairCase]:
    scenarios = {item.name: item for item in all_asset_resolution_scenarios()}
    cases: list[AssetResolutionPairCase] = []

    shared_image_name = "pair_top_level_image_shared_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_image_placeholder_then_public_remote",
            first=replace(
                scenarios["top_level_image_placeholder_zero_byte"],
                name=shared_image_name,
                age_days=240,
            ),
            second=replace(
                scenarios["top_level_image_public_token_remote"],
                name=shared_image_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_public_token_get_image_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes=(
                "Old placeholder image missing must not poison later recoverable "
                "public-token remote recovery for the same logical asset identity, "
                "even if the first pass spent one cheap fast-client evidence probe."
            ),
        )
    )

    shared_video_name = "pair_top_level_video_shared_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_video_old_timeout_then_direct_remote",
            first=AssetResolutionScenario(
                name=shared_video_name,
                suite="pair_sequence",
                asset_type="video",
                topology="top_level",
                age_days=240,
                context_payload_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=0,
                notes="Synthetic old top-level video timeout miss for cross-scenario cache poisoning checks.",
            ),
            second=replace(
                scenarios["top_level_video_context_timeout_direct_file_id_remote"],
                name=shared_video_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes="Old unresolved top-level video timeout miss must not poison later direct-file-id remote recovery for the same logical asset identity.",
        )
    )

    shared_file_name = "pair_top_level_file_shared_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_file_old_timeout_then_direct_remote",
            first=AssetResolutionScenario(
                name=shared_file_name,
                suite="pair_sequence",
                asset_type="file",
                topology="top_level",
                age_days=240,
                context_payload_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=0,
                notes="Synthetic old top-level file timeout miss for cross-scenario cache poisoning checks.",
            ),
            second=AssetResolutionScenario(
                name=shared_file_name,
                suite="pair_sequence",
                asset_type="file",
                topology="top_level",
                age_days=20,
                context_payload_state="unavailable",
                direct_file_result_state="valid_remote",
                expected_resolver="napcat_segment_file_id_get_file_remote_url",
                expected_path_kind="remote",
                max_client_calls=1,
                max_fast_calls=1,
                max_remote_attempts=1,
                notes="Same logical asset identity later becomes recoverable through direct-file-id remote path.",
            ),
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes="Top-level file timeout miss must not poison later remote direct-file-id recovery.",
        )
    )

    shared_speech_name = "pair_top_level_speech_shared_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_speech_old_timeout_then_public_remote",
            first=AssetResolutionScenario(
                name=shared_speech_name,
                suite="pair_sequence",
                asset_type="speech",
                topology="top_level",
                age_days=240,
                context_payload_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=0,
            ),
            second=replace(
                scenarios["top_level_speech_public_token_remote"],
                name=shared_speech_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_public_token_get_record_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes="Old unresolved top-level speech timeout miss must not poison later public-token remote recovery.",
        )
    )

    shared_forward_name = "pair_forward_video_identity"
    cases.append(
        AssetResolutionPairCase(
            name="forward_old_public_timeout_then_recent_remote",
            first=replace(
                scenarios["forward_old_video_public_token_timeout"],
                name=shared_forward_name,
            ),
            second=AssetResolutionScenario(
                name=shared_forward_name,
                suite="pair_sequence",
                asset_type="video",
                topology="forward",
                age_days=20,
                source_path_state="stale_missing",
                hint_remote_state="live_http",
                forward_payload_state="remote_url",
                expected_resolver="napcat_forward_remote_url",
                expected_path_kind="remote",
                max_client_calls=0,
                max_fast_calls=0,
                max_remote_attempts=1,
                notes="Recent forward remote recovery should not be poisoned by a prior old forward public-token timeout under the same logical asset identity.",
            ),
            expected_second_resolver="napcat_forward_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
        )
    )

    shared_forward_image_terminal_name = "pair_forward_image_terminal_identity"
    cases.append(
        AssetResolutionPairCase(
            name="forward_image_terminal_then_top_level_public_remote",
            first=AssetResolutionScenario(
                name=shared_forward_image_terminal_name,
                suite="pair_sequence",
                asset_type="image",
                topology="forward",
                age_days=20,
                hint_remote_state="expired_pair",
                forward_metadata_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=2,
                notes="Forward image dead-remote evidence plus metadata timeout alone is unresolved; later public evidence must still be allowed to recover the same identity.",
            ),
            second=replace(
                scenarios["top_level_image_public_token_remote"],
                name=shared_forward_image_terminal_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_public_token_get_image_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=3,
            notes="A prior unresolved forward-image miss with strong remote evidence must not poison later top-level public-token recovery under the same logical identity.",
        )
    )

    shared_forward_image_unresolved_name = "pair_forward_image_unresolved_identity"
    cases.append(
        AssetResolutionPairCase(
            name="forward_image_unresolved_then_top_level_public_remote",
            first=AssetResolutionScenario(
                name=shared_forward_image_unresolved_name,
                suite="pair_sequence",
                asset_type="image",
                topology="forward",
                age_days=20,
                hint_remote_state="stale_http",
                forward_metadata_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=1,
                notes="Dead remote plus metadata timeout alone is not terminal proof for forward image.",
            ),
            second=replace(
                scenarios["top_level_image_public_token_remote"],
                name=shared_forward_image_unresolved_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_public_token_get_image_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=3,
            max_remote_attempts=2,
            notes="An unresolved forward-image miss must not poison later top-level public-token recovery under the same logical identity.",
        )
    )

    shared_top_level_to_forward_image_name = "pair_top_level_to_forward_image_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_image_placeholder_then_forward_remote",
            first=replace(
                scenarios["top_level_image_placeholder_zero_byte"],
                name=shared_top_level_to_forward_image_name,
                age_days=240,
            ),
            second=replace(
                scenarios["forward_image_remote_url_hit"],
                name=shared_top_level_to_forward_image_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_forward_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes="A top-level unresolved placeholder image must not poison later forward remote recovery for the same logical identity.",
        )
    )

    shared_forward_to_nested_image_name = "pair_forward_to_nested_image_identity"
    cases.append(
        AssetResolutionPairCase(
            name="forward_image_unresolved_then_nested_forward_remote",
            first=AssetResolutionScenario(
                name=shared_forward_to_nested_image_name,
                suite="pair_sequence",
                asset_type="image",
                topology="forward",
                age_days=20,
                hint_remote_state="stale_http",
                forward_metadata_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=1,
                notes="Forward image with weak dead remote evidence alone remains unresolved.",
            ),
            second=replace(
                scenarios["exhaustive_nested_forward_image_relative_http_unavailable_remote_wins"],
                name=shared_forward_to_nested_image_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_forward_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=2,
            max_remote_attempts=2,
            notes="An unresolved forward image must not poison later nested-forward remote recovery for the same logical identity.",
        )
    )

    shared_top_level_to_forward_video_name = "pair_top_level_to_forward_video_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_video_old_timeout_then_forward_direct_remote",
            first=AssetResolutionScenario(
                name=shared_top_level_to_forward_video_name,
                suite="pair_sequence",
                asset_type="video",
                topology="top_level",
                age_days=240,
                context_payload_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=0,
                notes="Synthetic old top-level video timeout miss for top-level to forward cross-topology checks.",
            ),
            second=AssetResolutionScenario(
                name=shared_top_level_to_forward_video_name,
                suite="pair_sequence",
                asset_type="video",
                topology="forward",
                age_days=20,
                context_payload_state="unavailable",
                direct_file_result_state="valid_remote",
                expected_resolver="napcat_segment_file_id_get_file_remote_url",
                expected_path_kind="remote",
                max_client_calls=1,
                max_fast_calls=1,
                max_remote_attempts=1,
                notes="Forward video becomes recoverable via direct-file-id remote path under the same logical identity.",
            ),
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes="A top-level video timeout miss must not poison later forward direct-file-id remote recovery.",
        )
    )

    shared_top_level_to_nested_file_name = "pair_top_level_to_nested_file_identity"
    cases.append(
        AssetResolutionPairCase(
            name="top_level_file_old_timeout_then_nested_forward_direct_remote",
            first=AssetResolutionScenario(
                name=shared_top_level_to_nested_file_name,
                suite="pair_sequence",
                asset_type="file",
                topology="top_level",
                age_days=240,
                context_payload_state="timeout",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=0,
                notes="Synthetic old top-level file timeout miss for top-level to nested-forward checks.",
            ),
            second=AssetResolutionScenario(
                name=shared_top_level_to_nested_file_name,
                suite="pair_sequence",
                asset_type="file",
                topology="nested_forward",
                age_days=20,
                context_payload_state="unavailable",
                direct_file_result_state="valid_remote",
                expected_resolver="napcat_segment_file_id_get_file_remote_url",
                expected_path_kind="remote",
                max_client_calls=1,
                max_fast_calls=1,
                max_remote_attempts=1,
                notes="Nested-forward file becomes recoverable via direct-file-id remote path under the same logical identity.",
            ),
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=1,
            notes="A top-level file timeout miss must not poison later nested-forward direct-file-id remote recovery.",
        )
    )

    shared_nested_to_top_level_image_name = "pair_nested_to_top_level_image_identity"
    cases.append(
        AssetResolutionPairCase(
            name="nested_forward_terminal_then_top_level_public_remote",
            first=replace(
                scenarios["exhaustive_nested_forward_image_old_stale_missing_dead_remote_metadata_timeout_materialize_empty"],
                name=shared_nested_to_top_level_image_name,
                age_days=260,
            ),
            second=replace(
                scenarios["top_level_image_public_token_remote"],
                name=shared_nested_to_top_level_image_name,
                age_days=20,
            ),
            expected_second_resolver="napcat_public_token_get_image_remote_url",
            expected_second_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=3,
            notes=(
                "A nested-forward terminal image miss must not poison later top-level "
                "public-token recovery for the same logical identity."
            ),
        )
    )

    return cases


def run_asset_resolution_pair_case(
    case: AssetResolutionPairCase,
    *,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionPairResult:
    runtime_first = _ScenarioRuntimeState(case.first)
    runtime_second: _ScenarioRuntimeState | None = None
    events: list[dict[str, Any]] = []
    client = _ScenarioPublicClient(case.first, runtime_first)
    fast_client = _ScenarioFastClient(case.first, runtime_first)
    downloader = _ScenarioAwareDownloader(client, fast_client=fast_client, state=runtime_first)
    try:
        _seed_prefetch_state(downloader, runtime_first, case.first)
        first_request = copy.deepcopy(runtime_first.request)
        first_result = downloader.resolve_for_export(
            copy.deepcopy(first_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        first_path_kind, _ = _path_kind_for_result(first_result, runtime_first)
        actual_first_resolver = _canonicalize_simulated_resolver(first_result[1])
        first_algebra = _derive_result_algebra_projection(
            scenario=case.first,
            actual_resolver=actual_first_resolver,
            actual_path_kind=first_path_kind,
        )
        first_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.first,
            request=first_request,
            algebra=first_algebra,
        )

        runtime_second = _ScenarioRuntimeState(case.second)
        _retarget_simulation_clients(
            downloader,
            client,
            fast_client,
            runtime_second,
            case.second,
        )
        _seed_prefetch_state(downloader, runtime_second, case.second)
        second_request = copy.deepcopy(runtime_second.request)
        second_result = downloader.resolve_for_export(
            copy.deepcopy(second_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        second_path_kind, _ = _path_kind_for_result(second_result, runtime_second)
        actual_second_resolver = _canonicalize_simulated_resolver(second_result[1])
        second_algebra = _derive_result_algebra_projection(
            scenario=case.second,
            actual_resolver=actual_second_resolver,
            actual_path_kind=second_path_kind,
        )
        second_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.second,
            request=second_request,
            algebra=second_algebra,
        )
        cost_matched = True
        if case.max_client_calls is not None and len(client.calls) > case.max_client_calls:
            cost_matched = False
        if case.max_fast_calls is not None and len(fast_client.calls) > case.max_fast_calls:
            cost_matched = False
        total_remote_attempts = len(runtime_first.remote_attempts) + len(runtime_second.remote_attempts)
        if case.max_remote_attempts is not None and total_remote_attempts > case.max_remote_attempts:
            cost_matched = False
        matched = (
            actual_second_resolver == case.expected_second_resolver
            and second_path_kind == case.expected_second_path_kind
            and cost_matched
        )
        trace_status_breakdown: dict[str, int] = {}
        for event in events:
            if str(event.get("phase") or "").strip() != "materialize_asset_substep":
                continue
            status = str(event.get("status") or "").strip()
            if not status:
                continue
            trace_status_breakdown[status] = trace_status_breakdown.get(status, 0) + 1
        return AssetResolutionPairResult(
            name=case.name,
            first_name=case.first.name,
            second_name=case.second.name,
            expected_second_resolver=case.expected_second_resolver,
            expected_second_path_kind=case.expected_second_path_kind,
            actual_first_resolver=actual_first_resolver,
            actual_first_path_kind=first_path_kind,
            actual_second_resolver=actual_second_resolver,
            actual_second_path_kind=second_path_kind,
            matched=matched,
            client_call_count=len(client.calls),
            fast_call_count=len(fast_client.calls),
            remote_attempt_count=total_remote_attempts,
            trace_event_count=len(events),
            trace_status_breakdown=trace_status_breakdown,
            cost_matched=cost_matched,
            first_algebra=first_algebra.to_dict(),
            second_algebra=second_algebra.to_dict(),
            first_join_snapshot=first_join_snapshot.to_dict(),
            second_join_snapshot=second_join_snapshot.to_dict(),
            notes=case.notes,
        )
    finally:
        downloader.close()
        runtime_first.close()
        if runtime_second is not None:
            runtime_second.close()


@lru_cache(maxsize=1)
def _run_asset_resolution_pair_matrix_cached() -> tuple[AssetResolutionPairResult, ...]:
    return tuple(
        run_asset_resolution_pair_case(case)
        for case in default_asset_resolution_pair_cases()
    )


def run_asset_resolution_pair_matrix() -> list[AssetResolutionPairResult]:
    return list(_run_asset_resolution_pair_matrix_cached())


def summarize_asset_resolution_pair_results(
    results: list[AssetResolutionPairResult],
) -> dict[str, Any]:
    mismatches = [item.name for item in results if not item.matched]
    resolver_counts: Counter[str] = Counter(
        str(item.actual_second_resolver or "<none>") for item in results
    )
    path_kind_counts: Counter[str] = Counter(item.actual_second_path_kind for item in results)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "resolver_counts": dict(resolver_counts),
        "path_kind_counts": dict(path_kind_counts),
        "mismatch_names": mismatches,
    }


def default_asset_resolution_triplet_cases() -> list[AssetResolutionTripletCase]:
    scenarios = {item.name: item for item in all_asset_resolution_scenarios()}
    cases: list[AssetResolutionTripletCase] = []

    shared_top_level_image_name = "triplet_top_level_image_shared_identity"
    image_weak = replace(
        scenarios["top_level_image_weak_gchatpic_context_no_path_recent"],
        name=shared_top_level_image_name,
        age_days=20,
    )
    image_strong = replace(
        scenarios["top_level_image_public_token_remote"],
        name=shared_top_level_image_name,
        age_days=20,
    )
    cases.append(
        AssetResolutionTripletCase(
            name="top_level_image_weak_then_strong_then_weak_repeat",
            first=image_weak,
            second=image_strong,
            third=image_weak,
            expected_second_resolver="napcat_public_token_get_image_remote_url",
            expected_second_path_kind="remote",
            expected_third_resolver="qq_not_downloaded_local_placeholder",
            expected_third_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=3,
            max_remote_attempts=1,
            notes=(
                "A later strong recovery must not mutate a repeated weak top-level placeholder "
                "request into a different semantic class inside downloader-only logic."
            ),
        )
    )

    shared_forward_image_name = "triplet_forward_image_shared_identity"
    forward_weak = AssetResolutionScenario(
        name=shared_forward_image_name,
        suite="triplet_sequence",
        asset_type="image",
        topology="forward",
        age_days=20,
        hint_remote_state="stale_http",
        forward_metadata_state="timeout",
        expected_resolver=None,
        expected_path_kind="missing",
        max_client_calls=0,
        max_fast_calls=1,
        max_remote_attempts=1,
        notes="Unresolved weak forward-image remote evidence for cross-topology triplet sequencing.",
    )
    forward_strong = replace(
        scenarios["top_level_image_public_token_remote"],
        name=shared_forward_image_name,
        age_days=20,
    )
    cases.append(
        AssetResolutionTripletCase(
            name="forward_image_weak_then_top_level_strong_then_forward_repeat",
            first=forward_weak,
            second=forward_strong,
            third=forward_weak,
            expected_second_resolver="napcat_public_token_get_image_remote_url",
            expected_second_path_kind="remote",
            expected_third_resolver="qq_expired_after_napcat",
            expected_third_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=3,
            max_remote_attempts=2,
            notes=(
                "A later strong top-level image recovery must not mutate a repeated forward-image "
                "terminal proof into a different class."
            ),
        )
    )

    shared_video_name = "triplet_top_level_video_shared_identity"
    video_weak = AssetResolutionScenario(
        name=shared_video_name,
        suite="triplet_sequence",
        asset_type="video",
        topology="top_level",
        age_days=240,
        context_payload_state="timeout",
        expected_resolver=None,
        expected_path_kind="missing",
        max_client_calls=0,
        max_fast_calls=1,
        max_remote_attempts=0,
        notes="Synthetic weak top-level video timeout miss for promotion sequencing.",
    )
    video_strong = AssetResolutionScenario(
        name=shared_video_name,
        suite="triplet_sequence",
        asset_type="video",
        topology="forward",
        age_days=20,
        context_payload_state="unavailable",
        direct_file_result_state="valid_remote",
        expected_resolver="napcat_segment_file_id_get_file_remote_url",
        expected_path_kind="remote",
        max_client_calls=1,
        max_fast_calls=1,
        max_remote_attempts=1,
        notes="Later strong direct-file-id recovery for the same logical video identity.",
    )
    cases.append(
        AssetResolutionTripletCase(
            name="top_level_video_weak_then_forward_strong_then_top_level_repeat",
            first=video_weak,
            second=video_strong,
            third=video_weak,
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            expected_third_resolver=None,
            expected_third_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=3,
            max_remote_attempts=1,
            notes=(
                "A strong forward direct-file-id recovery must not mutate a repeated weak top-level "
                "video timeout request inside downloader-only logic."
            ),
        )
    )

    join_nonmutation_name = "join_nonmutation_direct_same_asset"
    direct_timeout = AssetResolutionScenario(
        name=join_nonmutation_name,
        suite="triplet_sequence",
        asset_type="video",
        topology="top_level",
        age_days=20,
        context_payload_state="unavailable",
        direct_file_result_state="timeout",
        expected_resolver=None,
        expected_path_kind="missing",
        max_client_calls=1,
        max_fast_calls=1,
        max_remote_attempts=0,
        notes="Top-level direct-file-id timeout should remain unresolved even after later stronger evidence for the same logical asset.",
    )
    direct_remote = AssetResolutionScenario(
        name=join_nonmutation_name,
        suite="triplet_sequence",
        asset_type="video",
        topology="forward",
        age_days=20,
        context_payload_state="unavailable",
        direct_file_result_state="valid_remote",
        expected_resolver="napcat_segment_file_id_get_file_remote_url",
        expected_path_kind="remote",
        max_client_calls=1,
        max_fast_calls=1,
        max_remote_attempts=1,
        notes="Forward direct-file-id remote success should share logical identity while keeping a distinct request key because of forward parent context.",
    )
    cases.append(
        AssetResolutionTripletCase(
            name="join_nonmutation_top_level_timeout_then_forward_direct_then_repeat",
            first=direct_timeout,
            second=direct_remote,
            third=direct_timeout,
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            expected_third_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_third_path_kind="local",
            max_client_calls=2,
            max_fast_calls=3,
            max_remote_attempts=1,
            notes=(
                "Different request keys with the same direct-file-id identity must not let later strong success retroactively rewrite earlier weak semantics."
            ),
        )
    )

    shared_file_name = "triplet_top_level_file_shared_identity"
    file_weak = AssetResolutionScenario(
        name=shared_file_name,
        suite="triplet_sequence",
        asset_type="file",
        topology="top_level",
        age_days=240,
        context_payload_state="timeout",
        expected_resolver=None,
        expected_path_kind="missing",
        max_client_calls=0,
        max_fast_calls=1,
        max_remote_attempts=0,
        notes="Synthetic weak top-level file timeout miss for promotion sequencing.",
    )
    file_strong = AssetResolutionScenario(
        name=shared_file_name,
        suite="triplet_sequence",
        asset_type="file",
        topology="nested_forward",
        age_days=20,
        context_payload_state="unavailable",
        direct_file_result_state="valid_remote",
        expected_resolver="napcat_segment_file_id_get_file_remote_url",
        expected_path_kind="remote",
        max_client_calls=1,
        max_fast_calls=1,
        max_remote_attempts=1,
        notes="Later strong nested-forward direct-file-id recovery for the same logical file identity.",
    )
    cases.append(
        AssetResolutionTripletCase(
            name="top_level_file_weak_then_nested_forward_strong_then_top_level_repeat",
            first=file_weak,
            second=file_strong,
            third=file_weak,
            expected_second_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_second_path_kind="remote",
            expected_third_resolver=None,
            expected_third_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=3,
            max_remote_attempts=1,
            notes=(
                "A strong nested-forward direct-file-id recovery must not mutate a repeated weak "
                "top-level file timeout request inside downloader-only logic."
            ),
        )
    )

    return cases


def run_asset_resolution_triplet_case(
    case: AssetResolutionTripletCase,
    *,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionTripletResult:
    runtime_first = _ScenarioRuntimeState(case.first)
    runtime_second: _ScenarioRuntimeState | None = None
    runtime_third: _ScenarioRuntimeState | None = None
    events: list[dict[str, Any]] = []
    client = _ScenarioPublicClient(case.first, runtime_first)
    fast_client = _ScenarioFastClient(case.first, runtime_first)
    downloader = _ScenarioAwareDownloader(client, fast_client=fast_client, state=runtime_first)
    try:
        _seed_prefetch_state(downloader, runtime_first, case.first)
        first_request = copy.deepcopy(runtime_first.request)
        first_result = downloader.resolve_for_export(
            copy.deepcopy(first_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        actual_first_path_kind, _ = _path_kind_for_result(first_result, runtime_first)
        actual_first_resolver = _canonicalize_simulated_resolver(first_result[1])
        first_algebra = _derive_result_algebra_projection(
            scenario=case.first,
            actual_resolver=actual_first_resolver,
            actual_path_kind=actual_first_path_kind,
        )
        first_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.first,
            request=first_request,
            algebra=first_algebra,
        )

        runtime_second = _ScenarioRuntimeState(case.second)
        _retarget_simulation_clients(downloader, client, fast_client, runtime_second, case.second)
        _seed_prefetch_state(downloader, runtime_second, case.second)
        second_request = copy.deepcopy(runtime_second.request)
        second_result = downloader.resolve_for_export(
            copy.deepcopy(second_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        actual_second_path_kind, _ = _path_kind_for_result(second_result, runtime_second)
        actual_second_resolver = _canonicalize_simulated_resolver(second_result[1])
        second_algebra = _derive_result_algebra_projection(
            scenario=case.second,
            actual_resolver=actual_second_resolver,
            actual_path_kind=actual_second_path_kind,
        )
        second_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.second,
            request=second_request,
            algebra=second_algebra,
        )

        runtime_third = _ScenarioRuntimeState(case.third)
        _retarget_simulation_clients(downloader, client, fast_client, runtime_third, case.third)
        _seed_prefetch_state(downloader, runtime_third, case.third)
        third_request = copy.deepcopy(runtime_third.request)
        third_result = downloader.resolve_for_export(
            copy.deepcopy(third_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        actual_third_path_kind, _ = _path_kind_for_result(third_result, runtime_third)
        actual_third_resolver = _canonicalize_simulated_resolver(third_result[1])
        third_algebra = _derive_result_algebra_projection(
            scenario=case.third,
            actual_resolver=actual_third_resolver,
            actual_path_kind=actual_third_path_kind,
        )
        third_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.third,
            request=third_request,
            algebra=third_algebra,
        )

        cost_matched = True
        if case.max_client_calls is not None and len(client.calls) > case.max_client_calls:
            cost_matched = False
        if case.max_fast_calls is not None and len(fast_client.calls) > case.max_fast_calls:
            cost_matched = False
        total_remote_attempts = (
            len(runtime_first.remote_attempts)
            + len(runtime_second.remote_attempts)
            + len(runtime_third.remote_attempts)
        )
        if case.max_remote_attempts is not None and total_remote_attempts > case.max_remote_attempts:
            cost_matched = False
        matched = (
            actual_second_resolver == case.expected_second_resolver
            and actual_second_path_kind == case.expected_second_path_kind
            and actual_third_resolver == case.expected_third_resolver
            and actual_third_path_kind == case.expected_third_path_kind
            and cost_matched
        )
        trace_status_breakdown: dict[str, int] = {}
        for event in events:
            if str(event.get("phase") or "").strip() != "materialize_asset_substep":
                continue
            status = str(event.get("status") or "").strip()
            if not status:
                continue
            trace_status_breakdown[status] = trace_status_breakdown.get(status, 0) + 1
        return AssetResolutionTripletResult(
            name=case.name,
            first_name=case.first.name,
            second_name=case.second.name,
            third_name=case.third.name,
            expected_second_resolver=case.expected_second_resolver,
            expected_second_path_kind=case.expected_second_path_kind,
            expected_third_resolver=case.expected_third_resolver,
            expected_third_path_kind=case.expected_third_path_kind,
            actual_first_resolver=actual_first_resolver,
            actual_first_path_kind=actual_first_path_kind,
            actual_second_resolver=actual_second_resolver,
            actual_second_path_kind=actual_second_path_kind,
            actual_third_resolver=actual_third_resolver,
            actual_third_path_kind=actual_third_path_kind,
            matched=matched,
            client_call_count=len(client.calls),
            fast_call_count=len(fast_client.calls),
            remote_attempt_count=total_remote_attempts,
            trace_event_count=len(events),
            trace_status_breakdown=trace_status_breakdown,
            cost_matched=cost_matched,
            first_algebra=first_algebra.to_dict(),
            second_algebra=second_algebra.to_dict(),
            third_algebra=third_algebra.to_dict(),
            first_join_snapshot=first_join_snapshot.to_dict(),
            second_join_snapshot=second_join_snapshot.to_dict(),
            third_join_snapshot=third_join_snapshot.to_dict(),
            notes=case.notes,
        )
    finally:
        downloader.close()
        runtime_first.close()
        if runtime_second is not None:
            runtime_second.close()
        if runtime_third is not None:
            runtime_third.close()


@lru_cache(maxsize=1)
def _run_asset_resolution_triplet_matrix_cached() -> tuple[AssetResolutionTripletResult, ...]:
    return tuple(
        run_asset_resolution_triplet_case(case)
        for case in default_asset_resolution_triplet_cases()
    )


def run_asset_resolution_triplet_matrix() -> list[AssetResolutionTripletResult]:
    return list(_run_asset_resolution_triplet_matrix_cached())


def summarize_asset_resolution_triplet_results(
    results: list[AssetResolutionTripletResult],
) -> dict[str, Any]:
    mismatches = [item.name for item in results if not item.matched]
    second_resolver_counts: Counter[str] = Counter(str(item.actual_second_resolver or "<none>") for item in results)
    third_resolver_counts: Counter[str] = Counter(str(item.actual_third_resolver or "<none>") for item in results)
    third_path_kind_counts: Counter[str] = Counter(item.actual_third_path_kind for item in results)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "second_resolver_counts": dict(second_resolver_counts),
        "third_resolver_counts": dict(third_resolver_counts),
        "third_path_kind_counts": dict(third_path_kind_counts),
        "mismatch_names": mismatches,
    }


@dataclass(frozen=True, slots=True)
class FutureLocalPromotionCandidate:
    asset_type: str
    topology: str
    file_name: str | None = None
    source_leaf: str | None = None
    md5: str | None = None
    file_id: str | None = None
    public_token: str | None = None
    public_action: str | None = None
    remote_url: str | None = None
    has_local_evidence: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FutureLocalPromotionCase:
    name: str
    order_mode: str
    first: FutureLocalPromotionCandidate
    second: FutureLocalPromotionCandidate
    third: FutureLocalPromotionCandidate
    expected_first_behavior: str
    expected_second_behavior: str
    expected_third_behavior: str
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "order_mode": self.order_mode,
            "first": self.first.to_dict(),
            "second": self.second.to_dict(),
            "third": self.third.to_dict(),
            "expected_first_behavior": self.expected_first_behavior,
            "expected_second_behavior": self.expected_second_behavior,
            "expected_third_behavior": self.expected_third_behavior,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class FutureLocalPromotionResult:
    name: str
    order_mode: str
    asset_type: str
    first_topology: str
    second_topology: str
    third_topology: str
    expected_first_behavior: str
    expected_second_behavior: str
    expected_third_behavior: str
    actual_first_behavior: str
    actual_second_behavior: str
    actual_third_behavior: str
    matched: bool
    first_algebra: dict[str, Any]
    second_algebra: dict[str, Any]
    third_algebra: dict[str, Any]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _promotion_identity_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _promotion_candidate_keys(
    candidate: FutureLocalPromotionCandidate,
) -> tuple[tuple[Any, ...], ...]:
    asset_type = _promotion_identity_string(candidate.asset_type)
    file_name = _promotion_identity_string(candidate.file_name)
    source_leaf = _promotion_identity_string(candidate.source_leaf)
    md5 = _promotion_identity_string(candidate.md5)
    file_id = _promotion_identity_string(candidate.file_id)
    public_token = _promotion_identity_string(candidate.public_token)
    public_action = _promotion_identity_string(candidate.public_action)
    remote_url = _promotion_identity_string(
        NapCatMediaDownloader._normalized_match_url(candidate.remote_url)
    )
    preferred_names = tuple(name for name in (file_name, source_leaf) if name)
    keys: list[tuple[Any, ...]] = []
    if public_token and public_action:
        keys.append(("public_token", asset_type, public_action, public_token))
    if file_id:
        keys.append(("file_id", asset_type, file_id))
    if remote_url:
        keys.append(("remote_url", asset_type, remote_url))
    if md5:
        for preferred_name in preferred_names:
            keys.append(("md5_named", asset_type, preferred_name, md5))
        if asset_type == "image":
            keys.append(("image_md5_only", asset_type, md5))
    deduped: list[tuple[Any, ...]] = []
    seen: set[tuple[Any, ...]] = set()
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return tuple(deduped)


def _promotion_behavior_for_candidate(
    candidate: FutureLocalPromotionCandidate,
    *,
    current_index: int,
    all_candidates: tuple[FutureLocalPromotionCandidate, ...],
    reuse_map: dict[tuple[Any, ...], str],
) -> str:
    keys = _promotion_candidate_keys(candidate)
    for key in keys:
        if key in reuse_map:
            return "recent_reuse"
    if candidate.has_local_evidence:
        return "copied_local"
    if candidate.asset_type == "image":
        for future_index, future_candidate in enumerate(all_candidates, start=1):
            if future_index <= current_index:
                continue
            if not future_candidate.has_local_evidence:
                continue
            if future_candidate.asset_type != "image":
                continue
            future_keys = _promotion_candidate_keys(future_candidate)
            if any(key in future_keys for key in keys):
                return "future_local_promotion"
    return "missing"


def _promotion_note_behavior(
    behavior: str,
) -> bool:
    return behavior in {"copied_local", "future_local_promotion", "recent_reuse"}


def default_future_local_identity_promotion_cases() -> list[FutureLocalPromotionCase]:
    shared_name = "shared-image-a.jpg"
    shared_md5 = "shared-image-a-md5"
    shared_remote = "https://cdn.example.com/shared-image-a.jpg"
    strong_image = FutureLocalPromotionCandidate(
        asset_type="image",
        topology="top_level",
        file_name=shared_name,
        md5=shared_md5,
        file_id="image-file-id-a",
        public_token="image-public-token-a",
        public_action="get_image",
        remote_url=shared_remote,
        has_local_evidence=True,
    )
    weak_image_top = FutureLocalPromotionCandidate(
        asset_type="image",
        topology="top_level",
        file_name=shared_name,
        md5=shared_md5,
    )
    weak_image_forward = replace(weak_image_top, topology="forward")
    weak_image_nested = replace(weak_image_top, topology="nested_forward")
    strong_image_nested = replace(strong_image, topology="nested_forward")
    strong_image_forward = replace(strong_image, topology="forward")
    mismatched_image = replace(
        strong_image_nested,
        file_name="different-image.jpg",
        md5="different-image-md5",
        file_id="image-file-id-b",
        public_token="image-public-token-b",
        remote_url="https://cdn.example.com/different-image.jpg",
    )
    handle_only_image = replace(
        strong_image_nested,
        has_local_evidence=False,
    )

    shared_video_name = "shared-video-a.mp4"
    shared_video_md5 = "shared-video-a-md5"
    weak_video_top = FutureLocalPromotionCandidate(
        asset_type="video",
        topology="top_level",
        file_name=shared_video_name,
        md5=shared_video_md5,
    )
    strong_video_forward = FutureLocalPromotionCandidate(
        asset_type="video",
        topology="forward",
        file_name=shared_video_name,
        md5=shared_video_md5,
        file_id="video-file-id-a",
        remote_url="https://cdn.example.com/shared-video-a.mp4",
        has_local_evidence=True,
    )

    return [
        FutureLocalPromotionCase(
            name="image_top_level_weak_then_nested_forward_strong_then_repeat",
            order_mode="weak_first_strong_later",
            first=weak_image_top,
            second=strong_image_nested,
            third=weak_image_top,
            expected_first_behavior="future_local_promotion",
            expected_second_behavior="recent_reuse",
            expected_third_behavior="recent_reuse",
            notes="Weak top-level image should future-promote from a later nested-forward local success and remain reusable on repeat.",
        ),
        FutureLocalPromotionCase(
            name="image_forward_weak_then_nested_forward_strong_then_repeat",
            order_mode="weak_first_strong_later",
            first=weak_image_forward,
            second=strong_image_nested,
            third=weak_image_forward,
            expected_first_behavior="future_local_promotion",
            expected_second_behavior="recent_reuse",
            expected_third_behavior="recent_reuse",
            notes="Weak forward image should future-promote from a later nested-forward local success.",
        ),
        FutureLocalPromotionCase(
            name="image_nested_forward_weak_then_top_level_strong_then_repeat",
            order_mode="weak_first_strong_later",
            first=weak_image_nested,
            second=strong_image,
            third=weak_image_nested,
            expected_first_behavior="future_local_promotion",
            expected_second_behavior="recent_reuse",
            expected_third_behavior="recent_reuse",
            notes="Weak nested-forward image should future-promote from a later top-level local success.",
        ),
        FutureLocalPromotionCase(
            name="image_top_level_strong_then_forward_weak_then_repeat",
            order_mode="strong_first_weak_later",
            first=strong_image,
            second=weak_image_forward,
            third=weak_image_forward,
            expected_first_behavior="copied_local",
            expected_second_behavior="recent_reuse",
            expected_third_behavior="recent_reuse",
            notes="A later weak forward image should reuse an earlier strong top-level local image without needing future-promotion.",
        ),
        FutureLocalPromotionCase(
            name="image_nested_forward_strong_then_top_level_weak_then_repeat",
            order_mode="strong_first_weak_later",
            first=strong_image_nested,
            second=weak_image_top,
            third=weak_image_top,
            expected_first_behavior="copied_local",
            expected_second_behavior="recent_reuse",
            expected_third_behavior="recent_reuse",
            notes="A later weak top-level image should reuse an earlier strong nested-forward local image.",
        ),
        FutureLocalPromotionCase(
            name="image_weak_then_handle_only_strong_without_local_does_not_promote",
            order_mode="weak_first_strong_later_no_local",
            first=weak_image_forward,
            second=handle_only_image,
            third=weak_image_forward,
            expected_first_behavior="missing",
            expected_second_behavior="missing",
            expected_third_behavior="missing",
            notes="Strong handles without a real local file must not enter future-local promotion.",
        ),
        FutureLocalPromotionCase(
            name="image_weak_then_mismatched_strong_local_does_not_promote",
            order_mode="weak_first_strong_later_mismatch",
            first=weak_image_top,
            second=mismatched_image,
            third=weak_image_top,
            expected_first_behavior="missing",
            expected_second_behavior="copied_local",
            expected_third_behavior="missing",
            notes="Mismatched image identity must not promote or reuse across later local success.",
        ),
        FutureLocalPromotionCase(
            name="video_weak_then_forward_strong_local_then_repeat",
            order_mode="weak_first_strong_later_non_image",
            first=weak_video_top,
            second=strong_video_forward,
            third=weak_video_top,
            expected_first_behavior="missing",
            expected_second_behavior="copied_local",
            expected_third_behavior="recent_reuse",
            notes="Future-local promotion is image-only, but later strong non-image copies should still allow recent reuse on repeat.",
        ),
    ]


def run_future_local_identity_promotion_case(
    case: FutureLocalPromotionCase,
) -> FutureLocalPromotionResult:
    candidates = (case.first, case.second, case.third)
    reuse_map: dict[tuple[Any, ...], str] = {}
    behaviors: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        behavior = _promotion_behavior_for_candidate(
            candidate,
            current_index=index,
            all_candidates=candidates,
            reuse_map=reuse_map,
        )
        if _promotion_note_behavior(behavior):
            for key in _promotion_candidate_keys(candidate):
                reuse_map.setdefault(key, behavior)
        behaviors.append(behavior)
    matched = behaviors == [
        case.expected_first_behavior,
        case.expected_second_behavior,
        case.expected_third_behavior,
    ]
    return FutureLocalPromotionResult(
        name=case.name,
        order_mode=case.order_mode,
        asset_type=case.first.asset_type,
        first_topology=case.first.topology,
        second_topology=case.second.topology,
        third_topology=case.third.topology,
        expected_first_behavior=case.expected_first_behavior,
        expected_second_behavior=case.expected_second_behavior,
        expected_third_behavior=case.expected_third_behavior,
        actual_first_behavior=behaviors[0],
        actual_second_behavior=behaviors[1],
        actual_third_behavior=behaviors[2],
        matched=matched,
        first_algebra=_promotion_behavior_to_algebra(asset_type=case.first.asset_type, behavior=behaviors[0]),
        second_algebra=_promotion_behavior_to_algebra(asset_type=case.second.asset_type, behavior=behaviors[1]),
        third_algebra=_promotion_behavior_to_algebra(asset_type=case.third.asset_type, behavior=behaviors[2]),
        notes=case.notes,
    )


@lru_cache(maxsize=1)
def _run_future_local_identity_promotion_matrix_cached() -> tuple[FutureLocalPromotionResult, ...]:
    return tuple(
        run_future_local_identity_promotion_case(case)
        for case in default_future_local_identity_promotion_cases()
    )


def run_future_local_identity_promotion_matrix() -> list[FutureLocalPromotionResult]:
    return list(_run_future_local_identity_promotion_matrix_cached())


def summarize_future_local_identity_promotion_results(
    results: list[FutureLocalPromotionResult],
) -> dict[str, Any]:
    mismatches = [item.name for item in results if not item.matched]
    asset_type_counts: Counter[str] = Counter(item.asset_type for item in results)
    order_mode_counts: Counter[str] = Counter(item.order_mode for item in results)
    first_behavior_counts: Counter[str] = Counter(item.actual_first_behavior for item in results)
    second_behavior_counts: Counter[str] = Counter(item.actual_second_behavior for item in results)
    third_behavior_counts: Counter[str] = Counter(item.actual_third_behavior for item in results)
    topology_sequence_counts: Counter[str] = Counter(
        f"{item.first_topology}->{item.second_topology}->{item.third_topology}"
        for item in results
    )
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "asset_type_counts": dict(asset_type_counts),
        "order_mode_counts": dict(order_mode_counts),
        "first_behavior_counts": dict(first_behavior_counts),
        "second_behavior_counts": dict(second_behavior_counts),
        "third_behavior_counts": dict(third_behavior_counts),
        "topology_sequence_counts": dict(topology_sequence_counts),
        "mismatch_names": mismatches,
    }


def default_cross_run_reset_cases() -> list[AssetResolutionPairCase]:
    return [
        replace(
            case,
            name=f"cross_run_reset_{case.name}",
            notes=(
                f"{case.notes} The second step runs after reset_export_state() to prove per-run caches "
                "and breakers do not poison the next run."
            ).strip(),
        )
        for case in default_asset_resolution_pair_cases()
    ]


def run_cross_run_reset_case(
    case: AssetResolutionPairCase,
    *,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionPairResult:
    runtime_first = _ScenarioRuntimeState(case.first)
    runtime_second: _ScenarioRuntimeState | None = None
    events: list[dict[str, Any]] = []
    client = _ScenarioPublicClient(case.first, runtime_first)
    fast_client = _ScenarioFastClient(case.first, runtime_first)
    downloader = _ScenarioAwareDownloader(client, fast_client=fast_client, state=runtime_first)
    try:
        _seed_prefetch_state(downloader, runtime_first, case.first)
        first_request = copy.deepcopy(runtime_first.request)
        first_result = downloader.resolve_for_export(
            copy.deepcopy(first_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        first_path_kind, _ = _path_kind_for_result(first_result, runtime_first)
        actual_first_resolver = _canonicalize_simulated_resolver(first_result[1])
        first_algebra = _derive_result_algebra_projection(
            scenario=case.first,
            actual_resolver=actual_first_resolver,
            actual_path_kind=first_path_kind,
        )
        first_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.first,
            request=first_request,
            algebra=first_algebra,
        )

        downloader.reset_export_state()

        runtime_second = _ScenarioRuntimeState(case.second)
        _retarget_simulation_clients(
            downloader,
            client,
            fast_client,
            runtime_second,
            case.second,
        )
        _seed_prefetch_state(downloader, runtime_second, case.second)
        second_request = copy.deepcopy(runtime_second.request)
        second_result = downloader.resolve_for_export(
            copy.deepcopy(second_request),
            trace_callback=(
                (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                if trace_callback is not None
                else events.append
            ),
        )
        second_path_kind, _ = _path_kind_for_result(second_result, runtime_second)
        actual_second_resolver = _canonicalize_simulated_resolver(second_result[1])
        second_algebra = _derive_result_algebra_projection(
            scenario=case.second,
            actual_resolver=actual_second_resolver,
            actual_path_kind=second_path_kind,
        )
        second_join_snapshot = _derive_join_composition_snapshot(
            scenario=case.second,
            request=second_request,
            algebra=second_algebra,
        )
        cost_matched = True
        if case.max_client_calls is not None and len(client.calls) > case.max_client_calls:
            cost_matched = False
        if case.max_fast_calls is not None and len(fast_client.calls) > case.max_fast_calls:
            cost_matched = False
        total_remote_attempts = len(runtime_first.remote_attempts) + len(runtime_second.remote_attempts)
        if case.max_remote_attempts is not None and total_remote_attempts > case.max_remote_attempts:
            cost_matched = False
        matched = (
            actual_second_resolver == case.expected_second_resolver
            and second_path_kind == case.expected_second_path_kind
            and cost_matched
        )
        trace_status_breakdown: dict[str, int] = {}
        for event in events:
            if str(event.get("phase") or "").strip() != "materialize_asset_substep":
                continue
            status = str(event.get("status") or "").strip()
            if not status:
                continue
            trace_status_breakdown[status] = trace_status_breakdown.get(status, 0) + 1
        return AssetResolutionPairResult(
            name=case.name,
            first_name=case.first.name,
            second_name=case.second.name,
            expected_second_resolver=case.expected_second_resolver,
            expected_second_path_kind=case.expected_second_path_kind,
            actual_first_resolver=actual_first_resolver,
            actual_first_path_kind=first_path_kind,
            actual_second_resolver=actual_second_resolver,
            actual_second_path_kind=second_path_kind,
            matched=matched,
            client_call_count=len(client.calls),
            fast_call_count=len(fast_client.calls),
            remote_attempt_count=total_remote_attempts,
            trace_event_count=len(events),
            trace_status_breakdown=trace_status_breakdown,
            cost_matched=cost_matched,
            first_algebra=first_algebra.to_dict(),
            second_algebra=second_algebra.to_dict(),
            first_join_snapshot=first_join_snapshot.to_dict(),
            second_join_snapshot=second_join_snapshot.to_dict(),
            notes=case.notes,
        )
    finally:
        downloader.close()
        runtime_first.close()
        if runtime_second is not None:
            runtime_second.close()


@lru_cache(maxsize=1)
def _run_cross_run_reset_matrix_cached() -> tuple[AssetResolutionPairResult, ...]:
    return tuple(
        run_cross_run_reset_case(case)
        for case in default_cross_run_reset_cases()
    )


def run_cross_run_reset_matrix() -> list[AssetResolutionPairResult]:
    return list(_run_cross_run_reset_matrix_cached())


def summarize_cross_run_reset_results(
    results: list[AssetResolutionPairResult],
) -> dict[str, Any]:
    return summarize_asset_resolution_pair_results(results)


def default_direct_file_id_scope_cases() -> list[DirectFileIdScopeCase]:
    cases: list[DirectFileIdScopeCase] = []
    for asset_type in ("video", "file", "speech"):
        cases.extend(
            [
                DirectFileIdScopeCase(
                    name=f"{asset_type}_same_parent_same_file_id",
                    asset_type=asset_type,
                    relationship="same_parent_same_file_id",
                    expected_same_key=True,
                ),
                DirectFileIdScopeCase(
                    name=f"{asset_type}_same_parent_different_file_id",
                    asset_type=asset_type,
                    relationship="same_parent_different_file_id",
                    expected_same_key=False,
                ),
                DirectFileIdScopeCase(
                    name=f"{asset_type}_same_parent_same_file_id_different_remote",
                    asset_type=asset_type,
                    relationship="same_parent_same_file_id_different_remote",
                    expected_same_key=False,
                ),
                DirectFileIdScopeCase(
                    name=f"{asset_type}_different_parent_same_file_id",
                    asset_type=asset_type,
                    relationship="different_parent_same_file_id",
                    expected_same_key=False,
                ),
            ]
        )
    return cases


def run_direct_file_id_scope_case(
    case: DirectFileIdScopeCase,
) -> DirectFileIdScopeResult:
    request_a = _timeout_scope_request(
        asset_type=case.asset_type,
        parent_id="parent-a",
        token="unused-token-a",
        file_name=f"{case.asset_type}-a.bin",
        md5=f"{case.asset_type}-md5-a",
        file_id=f"/scope/{case.asset_type}/a",
        forward=True,
    )
    request_b = copy.deepcopy(request_a)
    if case.relationship == "same_parent_different_file_id":
        request_b["download_hint"]["file_id"] = f"/scope/{case.asset_type}/b"
    elif case.relationship == "same_parent_same_file_id_different_remote":
        request_a["download_hint"]["remote_url"] = f"https://assets.example.invalid/{case.asset_type}/a.bin"
        request_b["download_hint"]["remote_url"] = f"https://assets.example.invalid/{case.asset_type}/b.bin"
    elif case.relationship == "different_parent_same_file_id":
        request_b["download_hint"]["_forward_parent"]["message_id_raw"] = "parent-b"
        request_b["download_hint"]["_forward_parent"]["element_id"] = "element:parent-b"
    key_a = NapCatMediaDownloader._request_key(request_a)
    key_b = NapCatMediaDownloader._request_key(request_b)
    actual_same_key = key_a == key_b
    return DirectFileIdScopeResult(
        name=case.name,
        asset_type=case.asset_type,
        relationship=case.relationship,
        expected_same_key=case.expected_same_key,
        actual_same_key=actual_same_key,
        matched=actual_same_key == case.expected_same_key,
        key_a=key_a,
        key_b=key_b,
    )


def run_direct_file_id_scope_matrix() -> list[DirectFileIdScopeResult]:
    return [
        run_direct_file_id_scope_case(case)
        for case in default_direct_file_id_scope_cases()
    ]


def summarize_direct_file_id_scope_results(
    results: list[DirectFileIdScopeResult],
) -> dict[str, Any]:
    asset_type_counts: Counter[str] = Counter()
    relationship_counts: Counter[str] = Counter()
    mismatches: list[str] = []
    for item in results:
        asset_type_counts[item.asset_type] += 1
        relationship_counts[item.relationship] += 1
        if not item.matched:
            mismatches.append(item.name)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "asset_type_counts": dict(asset_type_counts),
        "relationship_counts": dict(relationship_counts),
        "mismatch_names": mismatches,
    }


def default_second_pass_gate_cases() -> list[SecondPassGateCase]:
    cases: list[SecondPassGateCase] = [
        SecondPassGateCase(
            name="top_level_image_no_prefetch_direct_public_token",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_no_prefetch_direct_public_token",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="live_http",
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="Direct top-level public-token handles are already primary-path evidence and should not trigger second-pass retry by default.",
        ),
        SecondPassGateCase(
            name="top_level_image_pending_future_payload_only",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_pending_future_payload_only",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="live_http",
                prefetch_seed=PrefetchSeed(public_prefetch_state="pending_future_payload_only"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=True,
            notes="An inflight public-token prefetch future should keep second-pass retry waiting.",
        ),
        SecondPassGateCase(
            name="top_level_image_done_not_finalized_payload_only",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_done_not_finalized_payload_only",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="live_http",
                prefetch_seed=PrefetchSeed(public_prefetch_state="done_not_finalized_payload_only"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=True,
            notes="A completed-but-unfinalized future must behave like inflight work until cached into terminal/shared state.",
        ),
        SecondPassGateCase(
            name="top_level_image_cached_payload_only",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_cached_payload_only",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="live_http",
                prefetch_seed=PrefetchSeed(public_prefetch_state="payload_only"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="Once cached payload-only prefetch state exists, second pass should not spin another public retry immediately.",
        ),
        SecondPassGateCase(
            name="top_level_image_cached_remote_attempted_failed",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_cached_remote_attempted_failed",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="expired_pair",
                prefetch_seed=PrefetchSeed(public_prefetch_state="remote_attempted_failed"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="A cached remote-attempted-failed prefetch should suppress second-pass retry even though it is not terminal by itself.",
        ),
        SecondPassGateCase(
            name="top_level_image_cached_terminal_result",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_cached_terminal_result",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="expired_pair",
                prefetch_seed=PrefetchSeed(public_prefetch_state="terminal_cached"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="A cached terminal public outcome must disable second-pass retry.",
        ),
        SecondPassGateCase(
            name="top_level_image_request_state_terminal_context_placeholder",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_request_state_terminal_context_placeholder",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                source_path_state="placeholder_zero",
                hint_remote_state="relative_gchatpic",
                hint_file_id_state="public_token",
                prefetch_seed=PrefetchSeed(request_context_payload_state="empty_local"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="Request-state terminal placeholder proof should short-circuit second-pass retry.",
        ),
        SecondPassGateCase(
            name="top_level_video_request_state_terminal_blank_public_payload",
            scenario=AssetResolutionScenario(
                name="gate_top_level_video_request_state_terminal_blank_public_payload",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="video",
                topology="top_level",
                source_path_state="stale_missing",
                hint_file_id_state="public_token",
                prefetch_seed=PrefetchSeed(request_context_payload_state="blank_public_payload"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="File-like blank public payload already in request-state should be treated as terminal for second-pass gating.",
        ),
        SecondPassGateCase(
            name="top_level_image_request_state_public_token",
            scenario=AssetResolutionScenario(
                name="gate_top_level_image_request_state_public_token",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="top_level",
                hint_file_id_state="public_token",
                hint_remote_state="live_http",
                prefetch_seed=PrefetchSeed(request_context_payload_state="public_token"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="Request-state public token alone should not force second-pass retry.",
        ),
        SecondPassGateCase(
            name="forward_video_pending_future_payload_only",
            scenario=AssetResolutionScenario(
                name="gate_forward_video_pending_future_payload_only",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="video",
                topology="forward",
                source_path_state="stale_missing",
                hint_file_id_state="public_token",
                prefetch_seed=PrefetchSeed(public_prefetch_state="pending_future_payload_only"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=True,
            notes="Forward media should keep second pass alive while its direct public-token future is still inflight.",
        ),
        SecondPassGateCase(
            name="nested_forward_image_cached_terminal_result",
            scenario=AssetResolutionScenario(
                name="gate_nested_forward_image_cached_terminal_result",
                suite="second_pass_gate_stability_under_prefetch_variants",
                asset_type="image",
                topology="nested_forward",
                source_path_state="stale_missing",
                hint_file_id_state="public_token",
                hint_remote_state="expired_pair",
                prefetch_seed=PrefetchSeed(public_prefetch_state="terminal_cached"),
                expected_resolver=None,
                expected_path_kind="missing",
            ),
            expected_should_retry=False,
            notes="Nested-forward image should skip second pass when public prefetch is already terminally classified.",
        ),
    ]
    return cases


def run_second_pass_gate_case(case: SecondPassGateCase) -> SecondPassGateResult:
    runtime = _ScenarioRuntimeState(case.scenario)
    client = _ScenarioPublicClient(case.scenario, runtime)
    fast_client = _ScenarioFastClient(case.scenario, runtime)
    downloader = _ScenarioAwareDownloader(client, fast_client=fast_client, state=runtime)
    try:
        _seed_prefetch_state(downloader, runtime, case.scenario)
        actual_should_retry = downloader.should_attempt_second_pass_public_retry(runtime.request)
        return SecondPassGateResult(
            name=case.name,
            suite=case.scenario.suite,
            topology=case.scenario.topology,
            asset_type=case.scenario.asset_type,
            public_prefetch_state=(
                case.scenario.prefetch_seed.public_prefetch_state
                if isinstance(case.scenario.prefetch_seed, PrefetchSeed)
                else "none"
            ),
            expected_should_retry=case.expected_should_retry,
            actual_should_retry=actual_should_retry,
            matched=actual_should_retry == case.expected_should_retry,
            client_call_count=len(client.calls),
            fast_call_count=len(fast_client.calls),
            trace_event_count=0,
            notes=case.notes,
        )
    finally:
        downloader.close()
        runtime.close()


@lru_cache(maxsize=1)
def _run_second_pass_gate_matrix_cached() -> tuple[SecondPassGateResult, ...]:
    return tuple(run_second_pass_gate_case(case) for case in default_second_pass_gate_cases())


def run_second_pass_gate_matrix() -> list[SecondPassGateResult]:
    return list(_run_second_pass_gate_matrix_cached())


def summarize_second_pass_gate_results(
    results: list[SecondPassGateResult],
) -> dict[str, Any]:
    mismatches = [item.name for item in results if not item.matched]
    topology_counts: Counter[str] = Counter(item.topology for item in results)
    asset_type_counts: Counter[str] = Counter(item.asset_type for item in results)
    state_counts: Counter[str] = Counter(item.public_prefetch_state for item in results)
    retry_counts: Counter[str] = Counter("retry" if item.actual_should_retry else "skip" for item in results)
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "topology_counts": dict(topology_counts),
        "asset_type_counts": dict(asset_type_counts),
        "public_prefetch_state_counts": dict(state_counts),
        "retry_counts": dict(retry_counts),
        "mismatch_names": mismatches,
    }


def write_simulation_trace(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


@dataclass(frozen=True, slots=True)
class PrefetchSeed:
    request_context_payload_state: str = "none"
    prefetched_media_state: str = "none"
    prefetched_forward_state: str = "none"
    public_prefetch_state: str = "none"
    forward_timeout_cache_state: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AssetResolutionScenario:
    name: str
    asset_type: str
    suite: str = "core"
    topology: str = "top_level"
    age_days: int = 7
    asset_role: str | None = None
    chat_provenance: str = "group"
    forward_recursive_family: str = "none"
    forward_expansion_state: str = "none"
    depth_semantics: str = "exact"
    forward_parent_state: str = "valid"
    source_path_state: str = "none"
    segment_path_provenance: str = "none"
    filesystem_family: str = "ntqq"
    ntqq_neighbor_class: str = "none"
    month_relation: str = "same_month"
    placeholder_shell_profile: str = "none"
    hint_local_state: str = "none"
    hint_remote_state: str = "none"
    hint_file_id_state: str = "none"
    context_payload_state: str = "none"
    forward_payload_state: str = "none"
    forward_metadata_state: str = "inherit"
    forward_materialize_state: str = "inherit"
    public_result_state: str = "none"
    public_fallback_result_state: str = "inherit"
    direct_file_result_state: str = "none"
    speech_identity_profile: str = "none"
    speech_md5_state: str = "none"
    speech_original_format: str = "unknown"
    speech_requested_out_format: str = "default"
    prefetch_seed: PrefetchSeed | None = None
    expected_resolver: str | None = None
    expected_path_kind: str = "missing"
    max_client_calls: int | None = None
    max_fast_calls: int | None = None
    max_remote_attempts: int | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


EVIDENCE_DIMENSION_DOMAINS: dict[str, tuple[str, ...]] = {
    "asset_type": ("image", "video", "file", "speech", "sticker"),
    "topology": ("top_level", "forward", "nested_forward", "forward_missing_parent"),
    "chat_provenance": ("group", "private"),
    "forward_recursive_family": (
        "none",
        "forward_leaf",
        "forward_chain_transition",
        "forward_chain_parent_partial",
        "forward_chain_alias_repeat",
        "forward_chain_budget_cut",
        "forward_chain_terminal_proof",
    ),
    "forward_expansion_state": (
        "none",
        "exact",
        "alias_repeat",
        "preview_only",
        "parent_partial",
        "unavailable",
        "budget_cut",
    ),
    "depth_semantics": ("exact", "lower_bound"),
    "forward_parent_state": (
        "valid",
        "missing_element_id",
        "missing_message_id_raw",
        "missing_peer_uid",
        "blank_parent_bundle",
    ),
    "source_path_state": ("none", "stale_missing", "placeholder_zero", "existing", "existing_zero"),
    "segment_path_provenance": (
        "none",
        "sourcePath",
        "filePath",
        "staticFacePath",
        "dynamicFacePath",
        "payload_path",
        "payload_file",
        "payload_url",
        "hint_path",
        "hint_file",
        "hint_url",
    ),
    "filesystem_family": ("unknown", "ntqq", "legacy", "mixed"),
    "ntqq_neighbor_class": ("none", "ori", "oritemp", "thumb", "cross_tree_pic", "thumb_variant"),
    "month_relation": ("none", "same_month", "neighbor_month", "cross_month_drift"),
    "placeholder_shell_profile": (
        "none",
        "source_zero",
        "sibling_zero_only",
        "stale_no_positive_neighbor",
        "dead_download_hint",
        "context_no_local_path",
    ),
    "hint_local_state": ("none", "path_existing", "file_existing", "path_zero", "file_zero", "stale_local_url"),
    "hint_remote_state": (
        "none",
        "live_http",
        "relative_http",
        "relative_gchatpic",
        "relative_download_dead",
        "stale_http",
        "expired_pair",
    ),
    "hint_file_id_state": ("none", "public_token", "direct_file_id"),
    "context_payload_state": (
        "none",
        "timeout",
        "unavailable",
        "error",
        "local_path",
        "zero_local",
        "empty_local",
        "stale_local",
        "public_token",
        "blank_public_payload",
        "zero_public_payload",
        "payload_file_id_only",
        "remote_url",
        "blank_payload",
    ),
    "forward_payload_state": (
        "none",
        "timeout",
        "unavailable",
        "error",
        "empty",
        "local_path",
        "zero_local",
        "empty_local",
        "stale_local",
        "public_token",
        "blank_public_payload",
        "zero_public_payload",
        "payload_file_id_only",
        "remote_url",
        "blank_payload",
    ),
    "forward_metadata_state": (
        "inherit",
        "none",
        "timeout",
        "unavailable",
        "error",
        "empty",
        "local_path",
        "zero_local",
        "empty_local",
        "stale_local",
        "public_token",
        "blank_public_payload",
        "zero_public_payload",
        "payload_file_id_only",
        "remote_url",
        "blank_payload",
    ),
    "forward_materialize_state": (
        "inherit",
        "none",
        "timeout",
        "unavailable",
        "error",
        "empty",
        "local_path",
        "zero_local",
        "empty_local",
        "stale_local",
        "public_token",
        "blank_public_payload",
        "zero_public_payload",
        "payload_file_id_only",
        "remote_url",
        "blank_payload",
    ),
    "public_result_state": (
        "none",
        "valid_local",
        "valid_zero_local",
        "valid_remote",
        "valid_remote_only",
        "expired_remote",
        "blank_payload",
        "known_bad_video",
        "known_bad_file",
        "known_bad_record",
        "timeout",
        "not_found",
        "opaque_error",
    ),
    "public_fallback_result_state": (
        "inherit",
        "none",
        "valid_local",
        "valid_zero_local",
        "valid_remote",
        "valid_remote_only",
        "expired_remote",
        "blank_payload",
        "known_bad_video",
        "known_bad_file",
        "known_bad_record",
        "timeout",
        "not_found",
        "opaque_error",
    ),
    "direct_file_result_state": (
        "none",
        "valid_local",
        "valid_remote",
        "blank_payload",
        "timeout",
        "not_found",
    ),
    "speech_identity_profile": (
        "none",
        "top_level_full",
        "forward_token_url_only",
        "forward_md5_capable",
        "name_only",
    ),
    "speech_md5_state": ("none", "present", "absent"),
    "speech_original_format": ("unknown", "amr", "silk", "ogg", "wav", "mp3", "m4a"),
    "speech_requested_out_format": ("default", "mp3"),
    "prefetch_request_context_payload_state": (
        "none",
        "empty_local",
        "blank_public_payload",
        "public_token",
    ),
    "prefetch_media_state": ("none", "payload_only"),
    "prefetch_forward_state": ("none", "payload_only"),
    "prefetch_public_state": (
        "none",
        "pending_future_payload_only",
        "done_not_finalized_payload_only",
        "payload_only",
        "remote_attempted_failed",
        "terminal_cached",
    ),
    "prefetch_forward_timeout_cache_state": ("none", "metadata_timeout"),
}


EVIDENCE_DIMENSION_METADATA: dict[str, dict[str, Any]] = {
    "asset_type": {
        "owner_track": "coverage_reachability_surface",
        "join_group": "asset_identity",
        "source_classes": ["exporter_logic"],
        "description": "Primary exportable asset family used by resolution and terminal classification.",
    },
    "topology": {
        "owner_track": "forward_recursive_surface",
        "join_group": "topology",
        "source_classes": ["exporter_logic", "napcat_plugin_contract"],
        "description": "Top-level vs forward/nested-forward structural placement.",
    },
    "chat_provenance": {
        "owner_track": "provider_history_surface",
        "join_group": "provider_message_provenance",
        "source_classes": ["exporter_logic", "napcat_public_interface", "napcat_plugin_contract"],
        "description": "Group/private provenance that changes provider fetch routes and some raw context semantics.",
    },
    "forward_recursive_family": {
        "owner_track": "forward_recursive_surface",
        "join_group": "forward_handle",
        "source_classes": ["exporter_logic"],
        "description": "Symbolic recursive family used to represent theoretically unbounded forward-chain structure finitely.",
    },
    "forward_expansion_state": {
        "owner_track": "forward_recursive_surface",
        "join_group": "forward_handle",
        "source_classes": ["exporter_logic"],
        "description": "Expansion-state class for recursive forward symbolic modeling.",
    },
    "depth_semantics": {
        "owner_track": "forward_recursive_surface",
        "join_group": "forward_handle",
        "source_classes": ["exporter_logic"],
        "description": "Whether recorded forward depth is exact or only a lower bound.",
    },
    "forward_parent_state": {
        "owner_track": "forward_recursive_surface",
        "join_group": "topology",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Immediate parent context completeness for forward-scoped routes.",
    },
    "source_path_state": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "State of direct source-path evidence carried by normalized segments.",
    },
    "segment_path_provenance": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "napcat_public_interface", "napcat_internal_explanatory"],
        "description": "Which field carried local/path evidence into normalization and later materialization.",
    },
    "filesystem_family": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "repo_docs"],
        "description": "NTQQ vs legacy cache family assumptions that affect path search and provenance semantics.",
    },
    "ntqq_neighbor_class": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "repo_docs"],
        "description": "Ori/OriTemp/Thumb/cross-tree neighbor class for NTQQ image evidence.",
    },
    "month_relation": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "repo_docs"],
        "description": "Relation between recorded month and recovered month/bucket.",
    },
    "placeholder_shell_profile": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "repo_docs"],
        "description": "Shape of placeholder-shell evidence before terminal classification.",
    },
    "hint_local_state": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "path_provenance",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Local-path hints carried in download hint fields.",
    },
    "hint_remote_state": {
        "owner_track": "filesystem_materialization_surface",
        "join_group": "remote_handle",
        "source_classes": ["exporter_logic", "napcat_public_interface", "napcat_plugin_contract"],
        "description": "Remote URL handle class before route ordering and failure classification.",
    },
    "hint_file_id_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "handle_identity",
        "source_classes": ["exporter_logic", "napcat_public_interface", "napcat_plugin_contract"],
        "description": "Public-token vs slash file-id handle shape exposed to the exporter.",
    },
    "context_payload_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "hydration_payload",
        "source_classes": ["exporter_logic", "napcat_plugin_contract"],
        "description": "Top-level context-hydration payload shape and result quality.",
    },
    "forward_payload_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "hydration_payload",
        "source_classes": ["exporter_logic", "napcat_plugin_contract"],
        "description": "Forward metadata payload shape before targeted materialization.",
    },
    "forward_metadata_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "hydration_payload",
        "source_classes": ["exporter_logic", "napcat_plugin_contract"],
        "description": "Metadata-only forward hydration result state.",
    },
    "forward_materialize_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "hydration_payload",
        "source_classes": ["exporter_logic", "napcat_plugin_contract"],
        "description": "Targeted/materializing forward hydration result state.",
    },
    "public_result_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "public_action",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Primary public media action result shape.",
    },
    "public_fallback_result_state": {
        "owner_track": "speech_output_surface",
        "join_group": "public_action",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Fallback public action result shape after primary failure or alternate call branch.",
    },
    "direct_file_result_state": {
        "owner_track": "downloader_decision_surface",
        "join_group": "direct_file_id",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Slash file-id direct route result state for file/video families.",
    },
    "speech_identity_profile": {
        "owner_track": "speech_output_surface",
        "join_group": "asset_identity",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Strength/profile of speech identity evidence after normalization.",
    },
    "speech_md5_state": {
        "owner_track": "speech_output_surface",
        "join_group": "asset_identity",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Whether speech identity includes md5 evidence.",
    },
    "speech_original_format": {
        "owner_track": "speech_output_surface",
        "join_group": "materialization_outcome",
        "source_classes": ["exporter_logic", "napcat_public_interface", "napcat_internal_explanatory"],
        "description": "Original speech payload/container format before any public-token conversion.",
    },
    "speech_requested_out_format": {
        "owner_track": "speech_output_surface",
        "join_group": "materialization_outcome",
        "source_classes": ["exporter_logic", "napcat_public_interface"],
        "description": "Requested `get_record` output format for the current route branch.",
    },
    "prefetch_request_context_payload_state": {
        "owner_track": "coverage_reachability_surface",
        "join_group": "prefetch",
        "source_classes": ["exporter_logic"],
        "description": "Seeded request-state evidence used by second-pass and prefetch gating tests.",
    },
    "prefetch_media_state": {
        "owner_track": "coverage_reachability_surface",
        "join_group": "prefetch",
        "source_classes": ["exporter_logic"],
        "description": "Top-level prefetch payload state before final resolution.",
    },
    "prefetch_forward_state": {
        "owner_track": "coverage_reachability_surface",
        "join_group": "prefetch",
        "source_classes": ["exporter_logic"],
        "description": "Forward prefetch payload state before final resolution.",
    },
    "prefetch_public_state": {
        "owner_track": "coverage_reachability_surface",
        "join_group": "prefetch",
        "source_classes": ["exporter_logic"],
        "description": "Public-token prefetch cache/future state.",
    },
    "prefetch_forward_timeout_cache_state": {
        "owner_track": "coverage_reachability_surface",
        "join_group": "timeout_scope",
        "source_classes": ["exporter_logic"],
        "description": "Forward metadata timeout-cache seed state.",
    },
}


EVIDENCE_JOIN_SCHEMA: dict[str, dict[str, Any]] = {
    "provider_message_provenance": {
        "owner_track": "provider_history_surface",
        "description": "Provider-side message identity and source provenance before downloader resolution begins.",
        "fields": (
            "chat_type",
            "chat_id",
            "history_fetch_source",
            "message_id_raw",
            "element_id",
            "peer_uid",
            "chat_type_raw",
            "forward_detail_route_state",
        ),
        "joins_to": ("forward_handle", "request_key"),
        "invariants": (
            "History source and raw message context must be sufficient to explain why a downloader route was reachable.",
            "Provider provenance must not silently collapse malformed forward detail into resolved state.",
        ),
    },
    "forward_handle": {
        "owner_track": "forward_recursive_surface",
        "description": "Structural identity of a forward container or symbolic recursive chain node.",
        "fields": (
            "forward_id_or_synthetic_handle",
            "parent_forward_handle",
            "root_forward_handle",
            "depth_lower_bound",
            "expansion_state",
            "forward_parent_state",
        ),
        "joins_to": ("request_key", "asset_identity_key"),
        "invariants": (
            "A recursive expansion step must discover a new forward handle or terminate as alias/budget-cut/unavailable.",
            "Partial parent state may remove parent-dependent routes without destroying parent-independent handles.",
        ),
    },
    "request_key": {
        "owner_track": "downloader_decision_surface",
        "description": "Operational identity for one concrete resolution attempt, including route-safe parent context.",
        "fields": (
            "asset_type",
            "asset_role",
            "file_name",
            "md5",
            "hint_file_id",
            "hint_file_biz_id",
            "normalized_remote_url",
            "message_id_raw",
            "element_id",
            "peer_uid",
            "chat_type_raw",
            "forward_parent_message_id_raw",
        ),
        "joins_to": ("asset_identity_key", "timeout_scope_key", "materialization_outcome"),
        "invariants": (
            "RequestKey stays parent-aware and route-safe.",
            "Later strong success must not mutate the semantic class of an earlier weak request through RequestKey reuse.",
        ),
    },
    "timeout_scope_key": {
        "owner_track": "downloader_decision_surface",
        "description": "Operational suppression scope for repeated timeout fallout and breaker behavior.",
        "fields": (
            "route_group",
            "asset_type",
            "asset_role",
            "forward_parent_scope",
            "token_or_parent_scope_discriminator",
            "age_bucket_or_month",
        ),
        "joins_to": ("request_key",),
        "invariants": (
            "Timeout suppression scope may be broader than RequestKey for some families, such as forward image parent-scoped suppression.",
            "Timeout scope must never merge unrelated parents.",
        ),
    },
    "asset_identity_key": {
        "owner_track": "coverage_reachability_surface",
        "description": "Parent-agnostic logical identity used for reuse, promotion, and shared missing where policy permits.",
        "fields": (
            "asset_type",
            "asset_role",
            "file_name",
            "md5",
            "source_leaf",
            "hint_file_id",
            "normalized_remote_url",
        ),
        "joins_to": ("materialization_outcome", "bundle_identity"),
        "invariants": (
            "Reuse/promotion may only happen on AssetIdentityKey, not by mutating RequestKey semantics.",
            "Forward file/video/speech require strong identity before shared outcome reuse is allowed.",
        ),
    },
    "bundle_identity": {
        "owner_track": "filesystem_materialization_surface",
        "description": "Bundle-stage identity used for copied/reused/future-local promotion decisions.",
        "fields": (
            "asset_identity_key",
            "target_relative_path",
            "copy_volume_relation",
            "future_local_evidence_state",
        ),
        "joins_to": ("materialization_outcome",),
        "invariants": (
            "Bundle reuse can be looser than downloader operational safety, but must remain explainable from logical identity and real local evidence.",
        ),
    },
    "materialization_outcome": {
        "owner_track": "filesystem_materialization_surface",
        "description": "Final bundle/manifest-facing outcome of one asset after resolution and second-pass behavior.",
        "fields": (
            "resolution_result",
            "terminality_class",
            "materialization_status",
            "missing_kind",
            "missing_bucket",
            "bundle_behavior",
            "requested_output_format",
            "materialized_output_format",
        ),
        "joins_to": (),
        "invariants": (
            "Background vs actionable missing is derived from missing_kind, not a free-standing status.",
            "Copied/reused/missing/error are downstream of resolution, not alternate resolver truths.",
        ),
    },
}


SIMULATOR_RESULT_ALGEBRA: dict[str, dict[str, Any]] = {
    "resolution_result": {
        "domain": ("resolved", "missing", "error"),
        "description": "Raw resolution-layer outcome before bundle/materialization semantics.",
    },
    "terminality_class": {
        "domain": ("recovered", "background_terminal", "actionable", "unresolved"),
        "description": "Semantic terminality after evidence classification.",
    },
    "materialization_status": {
        "domain": ("copied", "reused", "missing", "error"),
        "description": "Bundle-stage asset materialization status.",
    },
    "missing_kind": {
        "domain": (
            "none",
            "missing_after_napcat",
            "qq_expired_after_napcat",
            "qq_not_downloaded_local_placeholder",
            "napcat_video_url_unavailable",
            "napcat_file_url_unavailable",
            "napcat_record_url_unavailable",
            "napcat_media_decode_failed",
        ),
        "description": "Manifest-facing missing taxonomy.",
    },
    "missing_bucket": {
        "domain": ("none", "actionable", "background"),
        "description": "Derived summary bucket from missing_kind.",
    },
    "bundle_behavior": {
        "domain": (
            "immediate_copy",
            "recent_reuse",
            "future_local_promotion",
            "second_pass_public_retry",
            "skip_no_new_evidence",
            "unresolved",
        ),
        "description": "How the bundle stage treated the asset after resolution.",
    },
    "requested_output_format": {
        "domain": ("default", "mp3"),
        "description": "Requested output format for public record retrieval branches.",
    },
    "original_input_format": {
        "domain": ("unknown", "amr", "silk", "ogg", "wav", "mp3", "m4a"),
        "description": "Original payload/container format before any conversion.",
    },
    "materialized_output_format": {
        "domain": ("unknown", "amr", "silk", "ogg", "wav", "mp3", "m4a"),
        "description": "Output/container format visible at bundle output time.",
    },
    "format_relation": {
        "domain": ("preserved", "converted", "unknown"),
        "description": "Relation between original input format and materialized output format.",
    },
    "output_name_relation": {
        "domain": ("kept_suffix", "rewritten_suffix_from_bytes", "generated_name", "unknown"),
        "description": "How the final output name relates to the original hinted name.",
    },
}


UNREACHABLE_VALUE_REASONS: dict[str, dict[str, str]] = {
    "context_payload_state": {
        "payload_file_id_only": "ignored_by_current_route",
        "remote_url": "ignored_by_current_route",
    },
    "forward_materialize_state": {
        "none": "alias_of_more_specific_dimension",
        "local_path": "ignored_by_current_route",
        "empty_local": "ignored_by_current_route",
        "stale_local": "ignored_by_current_route",
        "public_token": "alias_of_more_specific_dimension",
        "blank_public_payload": "alias_of_more_specific_dimension",
        "zero_public_payload": "alias_of_more_specific_dimension",
        "payload_file_id_only": "alias_of_more_specific_dimension",
        "remote_url": "ignored_by_current_route",
        "blank_payload": "alias_of_more_specific_dimension",
    },
    "chat_provenance": {
        "private": "needs_new_carrier",
    },
    "filesystem_family": {
        "unknown": "needs_new_carrier",
        "legacy": "needs_new_carrier",
        "mixed": "needs_new_carrier",
    },
    "month_relation": {
        "none": "needs_new_carrier",
        "neighbor_month": "needs_new_carrier",
        "cross_month_drift": "needs_new_carrier",
    },
    "ntqq_neighbor_class": {
        "ori": "needs_new_carrier",
        "oritemp": "needs_new_carrier",
        "thumb": "needs_new_carrier",
        "cross_tree_pic": "needs_new_carrier",
        "thumb_variant": "needs_new_carrier",
    },
    "placeholder_shell_profile": {
        "source_zero": "needs_new_carrier",
        "sibling_zero_only": "needs_new_carrier",
        "stale_no_positive_neighbor": "needs_new_carrier",
        "dead_download_hint": "needs_new_carrier",
        "context_no_local_path": "needs_new_carrier",
    },
    "segment_path_provenance": {
        "sourcePath": "needs_new_carrier",
        "filePath": "needs_new_carrier",
        "staticFacePath": "needs_new_carrier",
        "dynamicFacePath": "needs_new_carrier",
        "payload_path": "needs_new_carrier",
        "payload_file": "needs_new_carrier",
        "payload_url": "needs_new_carrier",
        "hint_path": "needs_new_carrier",
        "hint_file": "needs_new_carrier",
        "hint_url": "needs_new_carrier",
    },
    "speech_identity_profile": {
        "top_level_full": "needs_new_carrier",
        "forward_token_url_only": "needs_new_carrier",
        "forward_md5_capable": "needs_new_carrier",
        "name_only": "needs_new_carrier",
    },
    "speech_md5_state": {
        "present": "needs_new_carrier",
        "absent": "needs_new_carrier",
    },
    "speech_original_format": {
        "amr": "future_format_placeholder",
        "silk": "future_format_placeholder",
        "ogg": "future_format_placeholder",
        "wav": "future_format_placeholder",
        "mp3": "future_format_placeholder",
        "m4a": "future_format_placeholder",
    },
    "speech_requested_out_format": {
        "mp3": "needs_new_carrier",
    },
}


LEDGER_STATUS_BY_REASON: dict[str, str] = {
    "ignored_by_current_route": "route_irrelevant",
    "alias_of_more_specific_dimension": "alias_of_other_dimension",
    "needs_new_carrier": "deferred_needs_carrier",
    "future_format_placeholder": "reserved_future_placeholder",
    "contract_unreachable": "contract_unreachable",
}


def _scenario_dimension_value_map(
    scenario: AssetResolutionScenario,
) -> dict[str, str]:
    seed = scenario.prefetch_seed if isinstance(scenario.prefetch_seed, PrefetchSeed) else None
    return {
        "asset_type": str(scenario.asset_type or "none"),
        "topology": str(scenario.topology or "none"),
        "chat_provenance": str(scenario.chat_provenance or "none"),
        "forward_recursive_family": str(scenario.forward_recursive_family or "none"),
        "forward_expansion_state": str(scenario.forward_expansion_state or "none"),
        "depth_semantics": str(scenario.depth_semantics or "exact"),
        "forward_parent_state": str(scenario.forward_parent_state or "none"),
        "source_path_state": str(scenario.source_path_state or "none"),
        "segment_path_provenance": str(scenario.segment_path_provenance or "none"),
        "filesystem_family": str(scenario.filesystem_family or "none"),
        "ntqq_neighbor_class": str(scenario.ntqq_neighbor_class or "none"),
        "month_relation": str(scenario.month_relation or "none"),
        "placeholder_shell_profile": str(scenario.placeholder_shell_profile or "none"),
        "hint_local_state": str(scenario.hint_local_state or "none"),
        "hint_remote_state": str(scenario.hint_remote_state or "none"),
        "hint_file_id_state": str(scenario.hint_file_id_state or "none"),
        "context_payload_state": str(scenario.context_payload_state or "none"),
        "forward_payload_state": str(scenario.forward_payload_state or "none"),
        "forward_metadata_state": str(scenario.forward_metadata_state or "none"),
        "forward_materialize_state": str(scenario.forward_materialize_state or "none"),
        "public_result_state": str(scenario.public_result_state or "none"),
        "public_fallback_result_state": str(scenario.public_fallback_result_state or "none"),
        "direct_file_result_state": str(scenario.direct_file_result_state or "none"),
        "speech_identity_profile": str(scenario.speech_identity_profile or "none"),
        "speech_md5_state": str(scenario.speech_md5_state or "none"),
        "speech_original_format": str(scenario.speech_original_format or "unknown"),
        "speech_requested_out_format": str(scenario.speech_requested_out_format or "default"),
        "prefetch_request_context_payload_state": (
            str(seed.request_context_payload_state or "none") if seed is not None else "none"
        ),
        "prefetch_media_state": (
            str(seed.prefetched_media_state or "none") if seed is not None else "none"
        ),
        "prefetch_forward_state": (
            str(seed.prefetched_forward_state or "none") if seed is not None else "none"
        ),
        "prefetch_public_state": (
            str(seed.public_prefetch_state or "none") if seed is not None else "none"
        ),
        "prefetch_forward_timeout_cache_state": (
            str(seed.forward_timeout_cache_state or "none") if seed is not None else "none"
        ),
    }


@dataclass(frozen=True, slots=True)
class ResultAlgebraProjection:
    resolution_result: str
    terminality_class: str
    materialization_status: str
    missing_kind: str
    missing_bucket: str
    bundle_behavior: str
    requested_output_format: str
    original_input_format: str
    materialized_output_format: str
    format_relation: str
    output_name_relation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class JoinCompositionSnapshot:
    provider_message_provenance: dict[str, Any]
    forward_handle: dict[str, Any] | None
    request_key: tuple[Any, ...] | None
    asset_identity_key: tuple[Any, ...] | None
    bundle_identity: tuple[Any, ...] | None
    materialization_outcome: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _derive_join_composition_snapshot(
    *,
    scenario: AssetResolutionScenario,
    request: dict[str, Any],
    algebra: ResultAlgebraProjection,
) -> JoinCompositionSnapshot:
    hint = NapCatMediaDownloader._request_hint(request)
    parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else None
    request_key = NapCatMediaDownloader._request_key(request)
    asset_identity_key = _shared_request_key_for_request(request)
    bundle_identity = (
        ("bundle", *asset_identity_key)
        if asset_identity_key is not None and algebra.materialization_status in {"copied", "reused"}
        else None
    )
    provider_message_provenance = {
        "chat_type": "group" if scenario.chat_provenance == "group" else "private",
        "chat_provenance": str(scenario.chat_provenance or "group"),
        "chat_id": f"{scenario.chat_provenance}_{scenario.name}",
        "history_fetch_source": (
            "simulated_forward_detail"
            if scenario.topology in {"forward", "nested_forward", "forward_missing_parent"}
            else "simulated_history"
        ),
        "message_id_raw": str(hint.get("message_id_raw") or ""),
        "element_id": str(hint.get("element_id") or ""),
        "peer_uid": str(hint.get("peer_uid") or ""),
        "chat_type_raw": str(hint.get("chat_type_raw") or ""),
        "forward_detail_route_state": (
            "required"
            if scenario.topology in {"forward", "nested_forward", "forward_missing_parent"}
            else "not_required"
        ),
    }
    forward_handle = None
    if scenario.topology in {"forward", "nested_forward", "forward_missing_parent"} or str(scenario.forward_recursive_family or "none") != "none":
        forward_handle = {
            "forward_recursive_family": str(scenario.forward_recursive_family or "none"),
            "forward_expansion_state": str(scenario.forward_expansion_state or "none"),
            "depth_semantics": str(scenario.depth_semantics or "exact"),
            "forward_parent_state": str(scenario.forward_parent_state or "valid"),
            "message_id_raw": str((parent or {}).get("message_id_raw") or ""),
            "element_id": str((parent or {}).get("element_id") or ""),
            "peer_uid": str((parent or {}).get("peer_uid") or ""),
            "chat_type_raw": str((parent or {}).get("chat_type_raw") or ""),
        }
    return JoinCompositionSnapshot(
        provider_message_provenance=provider_message_provenance,
        forward_handle=forward_handle,
        request_key=request_key,
        asset_identity_key=asset_identity_key,
        bundle_identity=bundle_identity,
        materialization_outcome=algebra.to_dict(),
    )


def _derive_result_algebra_projection(
    *,
    scenario: AssetResolutionScenario,
    actual_resolver: str | None,
    actual_path_kind: str,
    bundle_behavior: str | None = None,
) -> ResultAlgebraProjection:
    requested_output_format = str(scenario.speech_requested_out_format or "default")
    original_input_format = str(scenario.speech_original_format or "unknown")
    materialized_output_format = "unknown"
    format_relation = "unknown"
    output_name_relation = "unknown"

    if actual_path_kind in {"local", "remote", "public"}:
        resolution_result = "resolved"
        terminality_class = "recovered"
        materialization_status = "reused" if bundle_behavior == "recent_reuse" else "copied"
        missing_kind = "none"
        missing_bucket = "none"
        normalized_bundle_behavior = bundle_behavior or "immediate_copy"
        if scenario.asset_type == "speech":
            if requested_output_format == "mp3":
                materialized_output_format = "mp3"
                if original_input_format in {"unknown", "mp3"}:
                    format_relation = "unknown" if original_input_format == "unknown" else "preserved"
                else:
                    format_relation = "converted"
                output_name_relation = "rewritten_suffix_from_bytes"
            else:
                materialized_output_format = original_input_format
                format_relation = "preserved" if original_input_format != "unknown" else "unknown"
                output_name_relation = "kept_suffix" if original_input_format != "unknown" else "unknown"
        else:
            output_name_relation = "kept_suffix" if actual_path_kind in {"local", "remote", "public"} else "unknown"
        return ResultAlgebraProjection(
            resolution_result=resolution_result,
            terminality_class=terminality_class,
            materialization_status=materialization_status,
            missing_kind=missing_kind,
            missing_bucket=missing_bucket,
            bundle_behavior=normalized_bundle_behavior,
            requested_output_format=requested_output_format,
            original_input_format=original_input_format,
            materialized_output_format=materialized_output_format,
            format_relation=format_relation,
            output_name_relation=output_name_relation,
        )

    resolution_result = "missing"
    if actual_resolver in {None, ""}:
        terminality_class = "unresolved"
        missing_kind = "none"
        missing_bucket = "none"
    elif actual_resolver in {"qq_expired_after_napcat", "qq_not_downloaded_local_placeholder"}:
        terminality_class = "background_terminal"
        missing_kind = actual_resolver
        missing_bucket = "background"
    else:
        terminality_class = "actionable"
        missing_kind = actual_resolver
        missing_bucket = "actionable"

    return ResultAlgebraProjection(
        resolution_result=resolution_result,
        terminality_class=terminality_class,
        materialization_status="missing",
        missing_kind=missing_kind,
        missing_bucket=missing_bucket,
        bundle_behavior=bundle_behavior or "unresolved",
        requested_output_format=requested_output_format,
        original_input_format=original_input_format,
        materialized_output_format=materialized_output_format,
        format_relation=format_relation,
        output_name_relation=output_name_relation,
    )


@dataclass(frozen=True, slots=True)
class AssetResolutionResult:
    name: str
    suite: str
    asset_type: str
    topology: str
    age_days: int
    expected_resolver: str | None
    actual_resolver: str | None
    expected_path_kind: str
    actual_path_kind: str
    matched: bool
    resolved_path: str | None
    client_call_count: int
    fast_call_count: int
    remote_attempt_count: int
    trace_event_count: int
    trace_status_breakdown: dict[str, int]
    cost_matched: bool
    algebra: dict[str, Any]
    join_snapshot: dict[str, Any]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AssetResolutionSequenceResult:
    name: str
    suite: str
    repeats: int
    expected_resolver: str | None
    expected_path_kind: str
    actual_resolver: str | None
    actual_path_kind: str
    matched: bool
    unique_resolvers: tuple[str | None, ...]
    unique_path_kinds: tuple[str, ...]
    client_call_count: int
    fast_call_count: int
    remote_attempt_count: int
    trace_event_count: int
    trace_status_breakdown: dict[str, int]
    cost_matched: bool
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AssetResolutionPairCase:
    name: str
    first: AssetResolutionScenario
    second: AssetResolutionScenario
    expected_second_resolver: str | None
    expected_second_path_kind: str
    max_client_calls: int | None = None
    max_fast_calls: int | None = None
    max_remote_attempts: int | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "first": self.first.to_dict(),
            "second": self.second.to_dict(),
            "expected_second_resolver": self.expected_second_resolver,
            "expected_second_path_kind": self.expected_second_path_kind,
            "max_client_calls": self.max_client_calls,
            "max_fast_calls": self.max_fast_calls,
            "max_remote_attempts": self.max_remote_attempts,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class AssetResolutionPairResult:
    name: str
    first_name: str
    second_name: str
    expected_second_resolver: str | None
    expected_second_path_kind: str
    actual_first_resolver: str | None
    actual_first_path_kind: str
    actual_second_resolver: str | None
    actual_second_path_kind: str
    matched: bool
    client_call_count: int
    fast_call_count: int
    remote_attempt_count: int
    trace_event_count: int
    trace_status_breakdown: dict[str, int]
    cost_matched: bool
    first_algebra: dict[str, Any]
    second_algebra: dict[str, Any]
    first_join_snapshot: dict[str, Any]
    second_join_snapshot: dict[str, Any]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AssetResolutionTripletCase:
    name: str
    first: AssetResolutionScenario
    second: AssetResolutionScenario
    third: AssetResolutionScenario
    expected_second_resolver: str | None
    expected_second_path_kind: str
    expected_third_resolver: str | None
    expected_third_path_kind: str
    max_client_calls: int | None = None
    max_fast_calls: int | None = None
    max_remote_attempts: int | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "first": self.first.to_dict(),
            "second": self.second.to_dict(),
            "third": self.third.to_dict(),
            "expected_second_resolver": self.expected_second_resolver,
            "expected_second_path_kind": self.expected_second_path_kind,
            "expected_third_resolver": self.expected_third_resolver,
            "expected_third_path_kind": self.expected_third_path_kind,
            "max_client_calls": self.max_client_calls,
            "max_fast_calls": self.max_fast_calls,
            "max_remote_attempts": self.max_remote_attempts,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class AssetResolutionTripletResult:
    name: str
    first_name: str
    second_name: str
    third_name: str
    expected_second_resolver: str | None
    expected_second_path_kind: str
    expected_third_resolver: str | None
    expected_third_path_kind: str
    actual_first_resolver: str | None
    actual_first_path_kind: str
    actual_second_resolver: str | None
    actual_second_path_kind: str
    actual_third_resolver: str | None
    actual_third_path_kind: str
    matched: bool
    client_call_count: int
    fast_call_count: int
    remote_attempt_count: int
    trace_event_count: int
    trace_status_breakdown: dict[str, int]
    cost_matched: bool
    first_algebra: dict[str, Any]
    second_algebra: dict[str, Any]
    third_algebra: dict[str, Any]
    first_join_snapshot: dict[str, Any]
    second_join_snapshot: dict[str, Any]
    third_join_snapshot: dict[str, Any]
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DirectFileIdScopeCase:
    name: str
    asset_type: str
    relationship: str
    expected_same_key: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DirectFileIdScopeResult:
    name: str
    asset_type: str
    relationship: str
    expected_same_key: bool
    actual_same_key: bool
    matched: bool
    key_a: tuple[Any, ...] | None
    key_b: tuple[Any, ...] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SecondPassGateCase:
    name: str
    scenario: AssetResolutionScenario
    expected_should_retry: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scenario": self.scenario.to_dict(),
            "expected_should_retry": self.expected_should_retry,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class SecondPassGateResult:
    name: str
    suite: str
    topology: str
    asset_type: str
    public_prefetch_state: str
    expected_should_retry: bool
    actual_should_retry: bool
    matched: bool
    client_call_count: int
    fast_call_count: int
    trace_event_count: int
    notes: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize_asset_resolution_results(
    results: list[AssetResolutionResult],
) -> dict[str, Any]:
    suite_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    topology_counts: Counter[str] = Counter()
    age_bucket_counts: Counter[str] = Counter()
    resolver_counts: Counter[str] = Counter()
    path_kind_counts: Counter[str] = Counter()
    trace_totals: Counter[str] = Counter()
    call_cost_totals: dict[str, dict[str, float]] = {}
    terminal_missing_quality: dict[str, int] = {
        "classified_missing_count": 0,
        "unresolved_missing_count": 0,
        "resolver_none_and_missing_count": 0,
    }
    cost_vs_result_cross_tab: Counter[str] = Counter()
    mismatches: list[str] = []
    cost_overruns: list[str] = []

    def _bump_cost_totals(key: str, item: AssetResolutionResult) -> None:
        bucket = call_cost_totals.setdefault(
            key,
            {
                "cases": 0.0,
                "public_calls_total": 0.0,
                "fast_calls_total": 0.0,
                "remote_attempts_total": 0.0,
                "max_public_calls": 0.0,
                "max_fast_calls": 0.0,
                "max_remote_attempts": 0.0,
            },
        )
        bucket["cases"] += 1.0
        bucket["public_calls_total"] += float(item.client_call_count)
        bucket["fast_calls_total"] += float(item.fast_call_count)
        bucket["remote_attempts_total"] += float(item.remote_attempt_count)
        bucket["max_public_calls"] = max(bucket["max_public_calls"], float(item.client_call_count))
        bucket["max_fast_calls"] = max(bucket["max_fast_calls"], float(item.fast_call_count))
        bucket["max_remote_attempts"] = max(bucket["max_remote_attempts"], float(item.remote_attempt_count))

    for item in results:
        suite_counts[item.suite] += 1
        asset_counts[item.asset_type] += 1
        topology_counts[item.topology] += 1
        age_bucket = _age_bucket_label(item.age_days)
        age_bucket_counts[age_bucket] += 1
        resolver_counts[str(item.actual_resolver or "<none>")] += 1
        path_kind_counts[item.actual_path_kind] += 1
        if not item.matched:
            mismatches.append(item.name)
        if not item.cost_matched:
            cost_overruns.append(item.name)
        if item.actual_path_kind == "missing":
            if item.actual_resolver is None:
                terminal_missing_quality["resolver_none_and_missing_count"] += 1
                terminal_missing_quality["unresolved_missing_count"] += 1
            else:
                terminal_missing_quality["classified_missing_count"] += 1
        cross_tab_key = (
            "matched_and_cheap"
            if item.matched and item.cost_matched
            else "matched_but_expensive"
            if item.matched and not item.cost_matched
            else "mismatched_and_cheap"
            if (not item.matched and item.cost_matched)
            else "mismatched_and_expensive"
        )
        cost_vs_result_cross_tab[cross_tab_key] += 1
        _bump_cost_totals(f"suite:{item.suite}", item)
        _bump_cost_totals(f"asset_type:{item.asset_type}", item)
        _bump_cost_totals(f"topology:{item.topology}", item)
        _bump_cost_totals(f"age_bucket:{age_bucket}", item)
        for status, count in item.trace_status_breakdown.items():
            trace_totals[status] += int(count)
    normalized_call_cost_totals: dict[str, dict[str, float]] = {}
    for key, raw in call_cost_totals.items():
        cases = max(1.0, raw["cases"])
        normalized_call_cost_totals[key] = {
            "cases": int(raw["cases"]),
            "public_calls_total": int(raw["public_calls_total"]),
            "fast_calls_total": int(raw["fast_calls_total"]),
            "remote_attempts_total": int(raw["remote_attempts_total"]),
            "avg_public_calls_per_case": round(raw["public_calls_total"] / cases, 3),
            "avg_fast_calls_per_case": round(raw["fast_calls_total"] / cases, 3),
            "avg_remote_attempts_per_case": round(raw["remote_attempts_total"] / cases, 3),
            "max_public_calls": int(raw["max_public_calls"]),
            "max_fast_calls": int(raw["max_fast_calls"]),
            "max_remote_attempts": int(raw["max_remote_attempts"]),
        }
    return {
        "total": len(results),
        "matched": len(results) - len(mismatches),
        "mismatched": len(mismatches),
        "cost_overruns": len(cost_overruns),
        "suite_counts": dict(suite_counts),
        "asset_type_counts": dict(asset_counts),
        "topology_counts": dict(topology_counts),
        "age_bucket_counts": dict(age_bucket_counts),
        "resolver_counts": dict(resolver_counts),
        "path_kind_counts": dict(path_kind_counts),
        "trace_status_totals": dict(trace_totals),
        "call_cost_totals": normalized_call_cost_totals,
        "terminal_missing_quality": terminal_missing_quality,
        "cost_vs_result_cross_tab": dict(cost_vs_result_cross_tab),
        "mismatch_names": mismatches,
        "cost_overrun_names": cost_overruns,
    }


def summarize_asset_resolution_catalog(
    scenarios: list["AssetResolutionScenario"] | None = None,
) -> dict[str, Any]:
    active = list(all_asset_resolution_scenarios() if scenarios is None else scenarios)
    suite_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    topology_counts: Counter[str] = Counter()
    age_bucket_counts: Counter[str] = Counter()
    asset_role_counts: Counter[str] = Counter()
    terminality_flags: Counter[str] = Counter()
    route_signal_flags: Counter[str] = Counter()
    shared_cache_risk_flags: Counter[str] = Counter()
    payload_shape_counts: dict[str, Counter[str]] = {
        "hint_remote_state": Counter(),
        "hint_file_id_state": Counter(),
        "context_payload_state": Counter(),
        "forward_payload_state": Counter(),
        "public_result_state": Counter(),
        "public_fallback_result_state": Counter(),
        "direct_file_result_state": Counter(),
        "speech_requested_out_format": Counter(),
    }
    state_field_names = tuple(EVIDENCE_DIMENSION_DOMAINS.keys())
    state_field_counts: dict[str, Counter[str]] = {
        field_name: Counter() for field_name in state_field_names
    }
    for item in active:
        dimension_values = _scenario_dimension_value_map(item)
        suite_counts[item.suite] += 1
        asset_counts[item.asset_type] += 1
        topology_counts[item.topology] += 1
        age_bucket = _age_bucket_label(item.age_days)
        age_bucket_counts[age_bucket] += 1
        asset_role_counts[str(item.asset_role or "<none>")] += 1
        if item.expected_path_kind == "missing" and item.expected_resolver is not None:
            terminality_flags["expected_terminal_missing"] += 1
        elif item.expected_path_kind == "missing":
            terminality_flags["expected_unresolved"] += 1
        elif item.expected_path_kind == "remote":
            terminality_flags["expected_recoverable_remote"] += 1
        elif item.expected_path_kind == "local":
            terminality_flags["expected_recoverable_local"] += 1
        if item.topology in {"forward", "nested_forward", "forward_missing_parent"}:
            route_signal_flags["has_forward_parent"] += 1
        if item.chat_provenance != "group":
            route_signal_flags["has_non_group_chat_provenance"] += 1
        if item.hint_local_state in {"path_existing", "file_existing", "path_zero", "file_zero"}:
            route_signal_flags["has_hint_local_path"] += 1
        if item.source_path_state in {"existing", "existing_zero", "stale_missing", "placeholder_zero"}:
            route_signal_flags["has_source_path"] += 1
        if item.segment_path_provenance != "none":
            route_signal_flags["has_segment_path_provenance"] += 1
        if item.filesystem_family in {"legacy", "mixed"}:
            route_signal_flags["has_non_ntqq_filesystem_family"] += 1
        if item.placeholder_shell_profile != "none":
            route_signal_flags["has_placeholder_shell_profile"] += 1
        if item.hint_remote_state in {"live_http", "relative_http", "stale_http", "expired_pair"}:
            route_signal_flags["has_hint_remote_url"] += 1
        if item.public_result_state != "none" or item.public_fallback_result_state not in {"", "inherit"}:
            route_signal_flags["has_public_token_shape"] += 1
        if item.direct_file_result_state != "none" or item.forward_metadata_state == "payload_file_id_only":
            route_signal_flags["has_direct_file_id_shape"] += 1
        if item.asset_type == "speech":
            route_signal_flags["has_speech_family"] += 1
            if item.speech_identity_profile != "none":
                route_signal_flags["has_speech_identity_profile"] += 1
            if item.speech_original_format != "unknown":
                route_signal_flags["has_speech_original_format"] += 1
        if item.source_path_state in {"existing_zero", "placeholder_zero"} or item.hint_local_state in {"path_zero", "file_zero"}:
            route_signal_flags["has_zero_byte_local"] += 1
        if item.topology in {"forward", "nested_forward"} and item.asset_type in {"file", "video"}:
            shared_cache_risk_flags[f"{age_bucket}_forward_{item.asset_type}"] += 1
            if item.expected_path_kind == "missing":
                shared_cache_risk_flags["shared_miss_eligible_shape"] += 1
        for field_name in state_field_names:
            normalized = dimension_values.get(field_name, "<none>")
            state_field_counts[field_name][normalized] += 1
        for field_name in payload_shape_counts:
            normalized = dimension_values.get(field_name, "<none>")
            payload_shape_counts[field_name][normalized] += 1
    return {
        "total": len(active),
        "suite_counts": dict(suite_counts),
        "asset_type_counts": dict(asset_counts),
        "topology_counts": dict(topology_counts),
        "age_bucket_counts": dict(age_bucket_counts),
        "asset_role_counts": dict(asset_role_counts),
        "terminality_flags": dict(terminality_flags),
        "route_signal_flags": dict(route_signal_flags),
        "shared_cache_risk_flags": dict(shared_cache_risk_flags),
        "payload_shape_counts": {
            field_name: dict(counter) for field_name, counter in payload_shape_counts.items()
        },
        "state_field_counts": {
            field_name: dict(counter)
            for field_name, counter in state_field_counts.items()
        },
    }


def summarize_simulator_coverage_manifest() -> dict[str, Any]:
    scenarios = all_asset_resolution_scenarios()
    catalog = summarize_asset_resolution_catalog(scenarios)
    second_pass_cases = default_second_pass_gate_cases()
    pair_cases = default_asset_resolution_pair_cases()
    triplet_cases = default_asset_resolution_triplet_cases()
    promotion_cases = default_future_local_identity_promotion_cases()

    asset_types = ("image", "video", "file", "speech", "sticker")
    topologies = ("top_level", "forward", "nested_forward")
    asset_topology_matrix: dict[str, dict[str, int]] = {
        asset_type: {topology: 0 for topology in topologies}
        for asset_type in asset_types
    }
    prefetch_seed_shape_counts: Counter[str] = Counter()
    parent_state_by_topology: dict[str, Counter[str]] = {
        topology: Counter() for topology in topologies
    }
    optimization_seam_counts: Counter[str] = Counter()
    sequence_family_counts: Counter[str] = Counter()

    def _seed_shape(seed: PrefetchSeed | None) -> str:
        if seed is None:
            return "<none>"
        parts = [
            f"request={seed.request_context_payload_state or 'none'}",
            f"media={seed.prefetched_media_state or 'none'}",
            f"forward={seed.prefetched_forward_state or 'none'}",
            f"public={seed.public_prefetch_state or 'none'}",
            f"timeout={seed.forward_timeout_cache_state or 'none'}",
        ]
        return "|".join(parts)

    for scenario in scenarios:
        if scenario.asset_type in asset_topology_matrix and scenario.topology in asset_topology_matrix[scenario.asset_type]:
            asset_topology_matrix[scenario.asset_type][scenario.topology] += 1
        if scenario.topology in parent_state_by_topology:
            parent_state_by_topology[scenario.topology][str(scenario.forward_parent_state or "<none>")] += 1
        prefetch_seed_shape_counts[_seed_shape(scenario.prefetch_seed)] += 1
        suite = str(scenario.suite or "").strip()
        if suite in {
            "request_state_payload_state_terminal_equivalence",
            "terminal_evidence_age_invariance",
            "exhaustive_forward_image_terminal",
        }:
            optimization_seam_counts["terminal_classifier"] += 1
        if suite in {
            "prefetch_seeded_image_interactions",
            "prefetch_seeded_forward_media_interactions",
        }:
            optimization_seam_counts["prefetch_seeded_routes"] += 1
        if suite == "partial_parent_handle_sufficient":
            optimization_seam_counts["partial_parent_handle_sufficient"] += 1

    for case in second_pass_cases:
        prefetch_seed_shape_counts[_seed_shape(case.scenario.prefetch_seed)] += 1
        optimization_seam_counts["second_pass_gate"] += 1
    for case in pair_cases:
        sequence_family_counts[f"pair:{case.first.asset_type}"] += 1
        optimization_seam_counts["repeated_identity_pair"] += 1
    for case in triplet_cases:
        sequence_family_counts[f"triplet:{case.first.asset_type}"] += 1
        optimization_seam_counts["repeated_identity_triplet"] += 1
    for case in promotion_cases:
        sequence_family_counts[f"promotion:{case.first.asset_type}"] += 1
        optimization_seam_counts["future_local_identity_promotion"] += 1

    family_topology_missing = [
        f"{asset_type}:{topology}"
        for asset_type in asset_types
        for topology in topologies
        if asset_topology_matrix[asset_type][topology] <= 0
    ]
    promotion_image_topology_coverage: dict[str, int] = {topology: 0 for topology in topologies}
    for case in promotion_cases:
        for topology in (case.first.topology, case.second.topology, case.third.topology):
            if case.first.asset_type == "image" and topology in promotion_image_topology_coverage:
                promotion_image_topology_coverage[topology] += 1
    promotion_image_topology_missing = [
        topology for topology, count in promotion_image_topology_coverage.items() if count <= 0
    ]
    return {
        "catalog": catalog,
        "case_family_counts": {
            "single_scenarios": len(scenarios),
            "second_pass_gate": len(second_pass_cases),
            "pair_sequence": len(pair_cases),
            "triplet_sequence": len(triplet_cases),
            "future_local_identity_promotion": len(promotion_cases),
        },
        "asset_topology_matrix": asset_topology_matrix,
        "prefetch_seed_shape_counts": dict(prefetch_seed_shape_counts),
        "parent_state_by_topology": {
            topology: dict(counter) for topology, counter in parent_state_by_topology.items()
        },
        "optimization_seam_counts": dict(optimization_seam_counts),
        "sequence_family_counts": dict(sequence_family_counts),
        "coverage_gaps": {
            "single_scenario_family_topology_missing": family_topology_missing,
            "promotion_image_topology_missing": promotion_image_topology_missing,
        },
    }


def summarize_simulator_evidence_dimension_manifest() -> dict[str, Any]:
    scenarios = all_asset_resolution_scenarios()
    second_pass_cases = default_second_pass_gate_cases()
    covered_values: dict[str, set[str]] = {name: set() for name in EVIDENCE_DIMENSION_DOMAINS}

    def _bump(field_name: str, raw_value: str | None) -> None:
        if field_name not in covered_values:
            return
        covered_values[field_name].add(str(raw_value or "none"))

    for scenario in scenarios:
        for field_name, value in _scenario_dimension_value_map(scenario).items():
            _bump(field_name, value)

    for case in second_pass_cases:
        for field_name, value in _scenario_dimension_value_map(case.scenario).items():
            if field_name.startswith("prefetch_"):
                _bump(field_name, value)

    domains = {name: list(values) for name, values in EVIDENCE_DIMENSION_DOMAINS.items()}
    covered = {name: sorted(values) for name, values in covered_values.items()}
    uncovered = {
        name: [value for value in values if value not in covered_values[name]]
        for name, values in EVIDENCE_DIMENSION_DOMAINS.items()
    }
    fully_covered = sorted(name for name, values in uncovered.items() if not values)
    partially_covered = sorted(name for name, values in uncovered.items() if values and len(values) < len(EVIDENCE_DIMENSION_DOMAINS[name]))
    untouched = sorted(name for name, values in uncovered.items() if len(values) == len(EVIDENCE_DIMENSION_DOMAINS[name]))
    return {
        "dimension_count": len(EVIDENCE_DIMENSION_DOMAINS),
        "domains": domains,
        "covered_values": covered,
        "uncovered_values": uncovered,
        "fully_covered_dimensions": fully_covered,
        "partially_covered_dimensions": partially_covered,
        "untouched_dimensions": untouched,
    }


def summarize_simulator_global_evidence_registry() -> dict[str, Any]:
    manifest = summarize_simulator_evidence_dimension_manifest()
    registry: dict[str, dict[str, Any]] = {}
    for name, domain in EVIDENCE_DIMENSION_DOMAINS.items():
        metadata = dict(EVIDENCE_DIMENSION_METADATA.get(name) or {})
        registry[name] = {
            "domain": list(domain),
            "owner_track": metadata.get("owner_track"),
            "join_group": metadata.get("join_group"),
            "source_classes": list(metadata.get("source_classes") or []),
            "description": metadata.get("description"),
            "covered_values": list(manifest["covered_values"].get(name, [])),
            "uncovered_values": list(manifest["uncovered_values"].get(name, [])),
        }
    return {
        "dimension_count": len(registry),
        "dimensions": registry,
    }


def summarize_simulator_value_witness_ledger() -> dict[str, Any]:
    scenarios = all_asset_resolution_scenarios()
    second_pass_cases = default_second_pass_gate_cases()
    witness_map: dict[str, dict[str, set[str]]] = {
        dimension: {value: set() for value in values}
        for dimension, values in EVIDENCE_DIMENSION_DOMAINS.items()
    }

    def _record(dimension: str, value: str | None, witness: str) -> None:
        if dimension not in witness_map:
            return
        normalized = str(value or "none")
        if normalized not in witness_map[dimension]:
            return
        witness_map[dimension][normalized].add(witness)

    for scenario in scenarios:
        for field_name, value in _scenario_dimension_value_map(scenario).items():
            _record(field_name, value, scenario.name)

    for case in second_pass_cases:
        witness = case.name
        for field_name, value in _scenario_dimension_value_map(case.scenario).items():
            if field_name.startswith("prefetch_"):
                _record(field_name, value, witness)

    ledger: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    for dimension, values in witness_map.items():
        ledger[dimension] = {}
        for value, witnesses in values.items():
            sorted_witnesses = sorted(witnesses)
            unreachable_reason = UNREACHABLE_VALUE_REASONS.get(dimension, {}).get(value)
            status = (
                "covered"
                if sorted_witnesses
                else LEDGER_STATUS_BY_REASON.get(unreachable_reason or "", "unresolved")
            )
            if status == "covered":
                unreachable_reason = None
            status_counts[status] += 1
            ledger[dimension][value] = {
                "status": status,
                "witness_count": len(sorted_witnesses),
                "witness_examples": sorted_witnesses[:8],
                "unreachable_reason": unreachable_reason,
            }
    unresolved_dimensions = sorted(
        dimension
        for dimension, values in ledger.items()
        if any(item["status"] == "unresolved" for item in values.values())
    )
    adjudicated_noncovered_dimensions = sorted(
        dimension
        for dimension, values in ledger.items()
        if any(item["status"] not in {"covered", "unresolved"} for item in values.values())
    )
    return {
        "dimension_count": len(ledger),
        "dimensions": ledger,
        "status_counts": dict(status_counts),
        "unresolved_dimensions": unresolved_dimensions,
        "adjudicated_noncovered_dimensions": adjudicated_noncovered_dimensions,
    }


def summarize_simulator_cross_track_join_schema() -> dict[str, Any]:
    return {
        "join_group_count": len(EVIDENCE_JOIN_SCHEMA),
        "join_groups": EVIDENCE_JOIN_SCHEMA,
    }


def summarize_simulator_result_algebra_spec() -> dict[str, Any]:
    return {
        "field_count": len(SIMULATOR_RESULT_ALGEBRA),
        "fields": SIMULATOR_RESULT_ALGEBRA,
    }


def _asset_suffix(asset_type: str) -> str:
    return {
        "image": "jpg",
        "video": "mp4",
        "file": "bin",
        "speech": "mp3",
        "sticker": "gif",
    }.get(asset_type, "dat")


def _timestamp_ms_for_age_days(age_days: int) -> int:
    target = datetime.now(timezone.utc) - timedelta(days=max(0, int(age_days)))
    return int(target.timestamp() * 1000)


def _context_hint(seed: str) -> dict[str, str]:
    return {
        "message_id_raw": f"msg_{seed}",
        "element_id": f"el_{seed}",
        "peer_uid": f"peer_{seed}",
        "chat_type_raw": "2",
    }


class _ScenarioPublicClient:
    def __init__(self, scenario: AssetResolutionScenario, state: "_ScenarioRuntimeState") -> None:
        self._scenario = scenario
        self._state = state
        self.calls: list[dict[str, Any]] = []

    def get_image(self, *args, **kwargs):
        return self._dispatch("get_image", **kwargs)

    def get_file(self, *args, **kwargs):
        return self._dispatch("get_file", **kwargs)

    def get_record(self, *args, **kwargs):
        return self._dispatch("get_record", **kwargs)

    def _dispatch(self, action: str, **kwargs):
        self.calls.append(
            {
                "action": action,
                "file": kwargs.get("file"),
                "file_id": kwargs.get("file_id"),
                "timeout": kwargs.get("timeout"),
                "out_format": kwargs.get("out_format"),
            }
        )
        file_token = str(kwargs.get("file") or "").strip()
        file_id = str(kwargs.get("file_id") or "").strip()
        if file_id.startswith("/"):
            mode = self._scenario.direct_file_result_state
        elif file_id:
            fallback_mode = str(self._scenario.public_fallback_result_state or "").strip()
            mode = (
                self._scenario.public_result_state
                if fallback_mode in {"", "inherit"}
                else fallback_mode
            )
        else:
            mode = self._scenario.public_result_state
        return self._state.public_action_payload(
            scenario=self._scenario,
            action=action,
            mode=mode,
            token=file_token,
            file_id=file_id,
        )


class _ScenarioFastClient:
    def __init__(self, scenario: AssetResolutionScenario, state: "_ScenarioRuntimeState") -> None:
        self._scenario = scenario
        self._state = state
        self.calls: list[dict[str, Any]] = []

    def hydrate_media(self, **kwargs):
        self.calls.append({"method": "hydrate_media", **kwargs})
        return self._state.context_payload(self._scenario)

    def hydrate_forward_media(self, **kwargs):
        self.calls.append({"method": "hydrate_forward_media", **kwargs})
        return self._state.forward_payload(self._scenario, materialize=bool(kwargs.get("materialize")))


class _ScenarioAwareDownloader(NapCatMediaDownloader):
    def __init__(
        self,
        client: _ScenarioPublicClient,
        *,
        fast_client: _ScenarioFastClient | None,
        state: "_ScenarioRuntimeState",
    ) -> None:
        self._scenario_state = state
        super().__init__(
            client,
            fast_client=fast_client,
            remote_cache_dir=state.cache_root,
            remote_base_url=state.remote_base_url,
        )

    def _create_prefetch_executors(self) -> None:
        self._public_token_executor = None
        self._remote_loop = None
        self._remote_loop_thread = None
        self._remote_async_client = None
        self._remote_async_semaphore = None

    def _rebuild_prefetch_executors(self, *, wait: bool, recreate: bool) -> None:
        _ = wait, recreate
        return

    def _download_remote_media(
        self,
        *,
        asset_type: str,
        file_name: str | None,
        hint: dict[str, Any],
    ) -> str | None:
        remote_url = str(hint.get("url") or "").strip()
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        self._scenario_state.remote_attempts.append(
            {
                "asset_type": asset_type,
                "file_name": file_name,
                "remote_url": resolved_remote_url,
            }
        )
        payload_path = self._scenario_state.remote_payload_path(resolved_remote_url)
        if payload_path:
            self._remember_remote_media_failure_reason(resolved_remote_url, None)
            return payload_path
        self._remember_remote_media_failure_reason(
            resolved_remote_url,
            self._scenario_state.remote_failure_reason(resolved_remote_url),
        )
        return None

    def _resolved_path_from_payload(self, data: dict[str, Any] | None) -> Path | None:
        return self._scenario_state.resolve_virtual_path_from_payload(data)

    def _find_stale_image_neighbor(self, source_path: str) -> Path | None:
        return self._scenario_state.find_stale_neighbor(source_path)

    def _zero_byte_local_payload_path(self, data: dict[str, Any] | None) -> Path | None:
        return self._scenario_state.resolve_virtual_path_from_payload(data, allow_zero=True, require_zero=True)

    def _looks_like_stale_local_media_path(self, value: object) -> bool:
        return self._scenario_state.is_stale_virtual_path(value)

    def _looks_like_zero_byte_local_media_path(self, value: object) -> bool:
        return self._scenario_state.is_zero_byte_virtual_path(value)

    def _classify_image_local_placeholder_missing(
        self,
        request: dict[str, Any] | None,
    ) -> str | None:
        if not isinstance(request, dict):
            return None
        return self._scenario_state.classify_placeholder_source(
            str(request.get("source_path") or "").strip()
        )

    def _download_remote_sticker(
        self,
        hint: dict[str, Any],
        *,
        asset_role: str | None,
        file_name: str | None,
    ) -> str | None:
        _ = asset_role, file_name
        remote_url = str(hint.get("remote_url") or hint.get("url") or "").strip()
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        self._scenario_state.remote_attempts.append(
            {
                "asset_type": "sticker",
                "file_name": file_name,
                "remote_url": resolved_remote_url,
            }
        )
        payload_path = self._scenario_state.remote_payload_path(resolved_remote_url)
        if payload_path:
            self._remember_remote_media_failure_reason(resolved_remote_url, None)
            return payload_path
        self._remember_remote_media_failure_reason(
            resolved_remote_url,
            self._scenario_state.remote_failure_reason(resolved_remote_url),
        )
        return None

    def _resolve_from_sticker_remote_url(
        self,
        hint: dict[str, Any] | None,
        *,
        asset_role: str | None,
        file_name: str | None,
    ) -> tuple[Path | None, str | None]:
        if not isinstance(hint, dict):
            return None, None
        resolved = self._download_remote_sticker(
            hint,
            asset_role=asset_role,
            file_name=file_name,
        )
        path = self._scenario_state.resolve_virtual_path(resolved)
        if path is None:
            return None, None
        return path, "sticker_remote_download"

    def _resolve_prefetched_remote_url(
        self,
        request_data: dict[str, Any],
        *,
        substep: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, bool]:
        asset_type = str(request_data.get("asset_type") or "").strip()
        if asset_type not in self.REMOTE_PREFETCHABLE_ASSET_TYPES:
            return None, False
        remote_url = str(request_data.get("remote_url") or request_data.get("url") or "").strip()
        if not remote_url:
            return None, False
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None, False
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        prefetched_resolution = self._peek_remote_media_prefetch(cache_key)
        if prefetched_resolution is ...:
            return None, False
        if request is not None:
            self._emit_asset_substep_trace(
                trace_callback,
                request,
                stage="done",
                substep=substep,
                timeout_s=self.REMOTE_MEDIA_FETCH_TIMEOUT_S,
                status="cached_ok" if prefetched_resolution else "cached_error",
                detail=resolved_remote_url,
            )
        path = self._scenario_state.resolve_virtual_path(prefetched_resolution)
        if path is None:
            if prefetched_resolution:
                self._drop_remote_prefetch_result(cache_key)
            return None, True
        return path, True

    def _resolve_remote_from_public_payload(
        self,
        request_data: dict[str, Any],
        payload: dict[str, Any] | None,
        *,
        action: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path | None:
        path = super()._resolve_remote_from_public_payload(
            request_data,
            payload,
            action=action,
            request=request,
            trace_callback=trace_callback,
        )
        if path is not None:
            return path
        remote_url = self._public_payload_remote_url(payload)
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        asset_type = str(request_data.get("asset_type") or "").strip()
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        cached_resolution = self._peek_remote_media_prefetch(cache_key)
        if cached_resolution in {None, ...}:
            return None
        return self._scenario_state.resolve_virtual_path(cached_resolution)

    def _resolve_prefetched_remote_from_payload(
        self,
        request_data: dict[str, Any],
        *,
        action: str,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> Path | None:
        path = super()._resolve_prefetched_remote_from_payload(
            request_data,
            action=action,
            request=request,
            trace_callback=trace_callback,
        )
        if path is not None:
            return path
        remote_url = str(request_data.get("remote_url") or request_data.get("url") or "").strip()
        resolved_remote_url = self._resolve_remote_url(remote_url)
        if not resolved_remote_url:
            return None
        asset_type = str(request_data.get("asset_type") or "").strip()
        cache_key = (asset_type, self._normalized_match_url(resolved_remote_url))
        prefetched_resolution = self._peek_remote_media_prefetch(cache_key)
        if prefetched_resolution in {None, ...}:
            return None
        return self._scenario_state.resolve_virtual_path(prefetched_resolution)

    def _resolve_from_forward_remote_url(
        self,
        data: dict[str, Any] | None,
        *,
        request: dict[str, Any] | None = None,
        trace_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[Path | None, str | None]:
        path, resolver = super()._resolve_from_forward_remote_url(
            data,
            request=request,
            trace_callback=trace_callback,
        )
        if path is not None:
            return path, resolver
        if not isinstance(data, dict):
            return None, None
        asset_type = str(data.get("asset_type") or "").strip()
        if asset_type not in {"image", "file", "speech", "video"}:
            return None, None
        prefetched, had_prefetched_result = self._resolve_prefetched_remote_url(
            data,
            substep="forward_remote_url_prefetch",
            request=request,
            trace_callback=trace_callback,
        )
        if prefetched is not None:
            return prefetched, "napcat_forward_remote_url"
        if had_prefetched_result:
            return None, None
        file_name = str(data.get("file_name") or "").strip() or None
        for _label, candidate_url in self._iter_forward_remote_url_candidates(
            request,
            payload=data,
        ):
            if not candidate_url:
                continue
            cache_key = (asset_type, self._normalized_match_url(candidate_url))
            cached_resolution = self._consume_remote_media_prefetch(cache_key)
            if cached_resolution is not ...:
                path = self._scenario_state.resolve_virtual_path(cached_resolution)
                if path is not None:
                    return path, "napcat_forward_remote_url"
            resolved = self._download_remote_media(
                asset_type=asset_type,
                file_name=file_name,
                hint={"url": candidate_url},
            )
            self._store_remote_prefetch_result(cache_key, resolved)
            path = self._scenario_state.resolve_virtual_path(resolved)
            if path is not None:
                return path, "napcat_forward_remote_url"
        return None, None


class _ScenarioRuntimeState:
    def __init__(self, scenario: AssetResolutionScenario) -> None:
        self.scenario = scenario
        repo_root = Path(__file__).resolve().parents[3]
        temp_root = repo_root / ".tmp" / "asset_simulator_virtual"
        self.root = temp_root / (
            f"asset-sim-{int(time.time() * 1_000_000)}-{os.getpid()}-{abs(hash(scenario.name)) % 100000}"
        )
        self.cache_root = self.root / "cache"
        self.remote_root = self.root / "remote"
        self.remote_base_url = "http://napcat.local/api"
        self.remote_map: dict[str, str] = {}
        self.remote_failure_map: dict[str, str] = {}
        self.kind_map: dict[str, str] = {}
        self._virtual_paths: dict[str, tuple[str, int]] = {}
        self._placeholder_missing_sources: set[str] = set()
        self.remote_attempts: list[dict[str, Any]] = []
        self.file_name = f"{scenario.name}.{_asset_suffix(scenario.asset_type)}"
        self.local_path = self._make_file("local", self.file_name, kind="local")
        self.zero_local_path = self._make_file("local_zero", self.file_name, kind="local_zero", zero=True)
        self.remote_path = self._make_file("remote", self.file_name, kind="remote")
        self.sticker_remote_path = self._make_file("remote_sticker", self.file_name, kind="remote")
        self.request = self._build_request()

    def close(self) -> None:
        return

    def _make_file(self, folder: str, name: str, *, kind: str, zero: bool = False) -> str:
        target = self.root / folder / name
        target_text = str(target)
        normalized = self._normalize_virtual_path(target_text)
        self._virtual_paths[normalized] = (kind, 0 if zero else max(1, len(f"{folder}:{name}")))
        self.kind_map[target_text] = kind
        return target_text

    @staticmethod
    def _normalize_virtual_path(path_text: object) -> str:
        value = str(path_text or "").strip()
        if not value:
            return ""
        if value.lower().startswith("file://"):
            value = value[7:]
        return str(PureWindowsPath(value)).casefold()

    def _build_request(self) -> dict[str, Any]:
        request: dict[str, Any] = {
            "asset_type": self.scenario.asset_type,
            "asset_role": self.scenario.asset_role or "",
            "file_name": self.file_name,
            "md5": f"{self.scenario.name[:16]:0<16}",
            "timestamp_ms": _timestamp_ms_for_age_days(self.scenario.age_days),
            "download_hint": {},
        }
        hint: dict[str, Any] = {}
        if self.scenario.topology == "top_level":
            hint.update(_context_hint(f"{self.scenario.name}_top"))
        elif self.scenario.topology in {"forward", "nested_forward", "forward_missing_parent"}:
            hint.update(_context_hint(f"{self.scenario.name}_asset"))
            hint["_forward_parent"] = _context_hint(f"{self.scenario.name}_parent")
        if self.scenario.topology in {"forward", "nested_forward", "forward_missing_parent"}:
            broken_parent = hint.get("_forward_parent") if isinstance(hint.get("_forward_parent"), dict) else {}
            parent_state = self.scenario.forward_parent_state
            if self.scenario.topology == "forward_missing_parent" and parent_state == "valid":
                parent_state = "missing_element_id"
            if parent_state == "missing_element_id":
                broken_parent["element_id"] = ""
            elif parent_state == "missing_message_id_raw":
                broken_parent["message_id_raw"] = ""
            elif parent_state == "missing_peer_uid":
                broken_parent["peer_uid"] = ""
            elif parent_state == "blank_parent_bundle":
                broken_parent.clear()
            elif parent_state != "valid":
                raise ValueError(f"unsupported forward_parent_state: {parent_state}")
            if broken_parent:
                hint["_forward_parent"] = broken_parent

        if self.scenario.hint_local_state == "path_existing":
            hint["path"] = self.local_path
        elif self.scenario.hint_local_state == "file_existing":
            hint["file"] = self.local_path
        elif self.scenario.hint_local_state == "path_zero":
            hint["path"] = self.zero_local_path
        elif self.scenario.hint_local_state == "file_zero":
            hint["file"] = self.zero_local_path
        elif self.scenario.hint_local_state == "stale_local_url":
            hint["url"] = str((self.root / "stale" / self.file_name).resolve())

        if self.scenario.hint_remote_state != "none":
            hint["remote_url"] = self._remote_url_for_state(self.scenario.hint_remote_state)
        hint_file_id_state = str(self.scenario.hint_file_id_state or "").strip()
        if hint_file_id_state == "public_token":
            hint["file_id"] = f"token-{self.scenario.name}"
        elif hint_file_id_state == "direct_file_id":
            hint["file_id"] = f"/fileid/{self.scenario.name}"
        elif hint_file_id_state not in {"", "none"}:
            raise ValueError(f"unsupported hint_file_id_state: {hint_file_id_state}")
        elif self.scenario.direct_file_result_state != "none":
            hint["file_id"] = f"/fileid/{self.scenario.name}"

        request["download_hint"] = hint
        source_path = self._source_path_for_state(self.scenario.source_path_state)
        if source_path:
            request["source_path"] = source_path
        return request

    def _source_path_for_state(self, state: str) -> str | None:
        if state == "none":
            return None
        if state == "stale_missing":
            target = self.root / "stale" / self.file_name
            return str(target)
        if state == "placeholder_zero":
            month_root = self.root / "Pic" / "2025-09"
            missing = month_root / "Ori" / self.file_name
            self._placeholder_missing_sources.add(self._normalize_virtual_path(str(missing)))
            for folder in ("OriTemp", "Thumb"):
                candidate = month_root / folder / self.file_name
                self._virtual_paths[self._normalize_virtual_path(str(candidate))] = ("placeholder_zero", 0)
            return str(missing)
        if state == "existing":
            return self.local_path
        if state == "existing_zero":
            return self.zero_local_path
        raise ValueError(f"unsupported source_path_state: {state}")

    def _remote_url_for_state(self, state: str) -> str:
        if state == "live_http":
            url = f"https://assets.example.invalid/{self.scenario.name}/{self.file_name}"
            self.remote_map[url] = self.remote_path
            return url
        if state == "relative_http":
            relative = f"/download/{self.scenario.name}/{self.file_name}"
            resolved = urljoin(self.remote_base_url.rstrip("/") + "/", relative.lstrip("/"))
            self.remote_map[resolved] = self.remote_path
            return relative
        if state == "relative_gchatpic":
            return (
                f"/gchatpic_new/3348513412/"
                f"{self.scenario.name}-{self.file_name}/0?term=255&is_origin=0"
            )
        if state == "relative_download_dead":
            relative = f"/download?appid=1407&fileid=dead-{self.scenario.name}&spec=0"
            resolved = urljoin(self.remote_base_url.rstrip("/") + "/", relative.lstrip("/"))
            self.remote_failure_map[resolved] = "unsupported_local_download"
            return relative
        if state == "stale_http":
            return f"https://assets.example.invalid/stale/{self.scenario.name}/{self.file_name}"
        if state == "expired_pair":
            url = f"https://assets.example.invalid/download/{self.scenario.name}/{self.file_name}"
            self.remote_failure_map[url] = "expired_remote"
            projected = (
                f"{self.remote_base_url.rstrip('/')}/download/{self.scenario.name}/{self.file_name}"
            )
            self.remote_failure_map[projected] = "unsupported_local_download"
            return url
        raise ValueError(f"unsupported hint_remote_state: {state}")

    def remote_payload_path(self, resolved_remote_url: str) -> str | None:
        return self.remote_map.get(str(resolved_remote_url))

    def remote_failure_reason(self, resolved_remote_url: str) -> str | None:
        return self.remote_failure_map.get(str(resolved_remote_url))

    def resolve_virtual_path(
        self,
        value: object,
        *,
        allow_zero: bool = False,
        require_zero: bool = False,
    ) -> Path | None:
        normalized = self._normalize_virtual_path(value)
        if not normalized:
            return None
        entry = self._virtual_paths.get(normalized)
        if entry is None:
            return None
        _kind, size = entry
        if require_zero and size > 0:
            return None
        if not allow_zero and size <= 0:
            return None
        return Path(str(value).replace("file://", "", 1))

    def resolve_virtual_path_from_payload(
        self,
        data: dict[str, Any] | None,
        *,
        allow_zero: bool = False,
        require_zero: bool = False,
    ) -> Path | None:
        if not isinstance(data, dict):
            return None
        candidate = data.get("file") or data.get("path") or data.get("url")
        path_text = str(candidate or "").strip()
        if not path_text:
            return None
        lowered = path_text.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            return None
        return self.resolve_virtual_path(
            path_text,
            allow_zero=allow_zero,
            require_zero=require_zero,
        )

    def is_zero_byte_virtual_path(self, value: object) -> bool:
        normalized = self._normalize_virtual_path(value)
        if not normalized:
            return False
        entry = self._virtual_paths.get(normalized)
        return entry is not None and entry[1] <= 0

    def is_stale_virtual_path(self, value: object) -> bool:
        candidate = str(value or "").strip()
        if not candidate:
            return False
        lowered = candidate.lower()
        if lowered.startswith("http://") or lowered.startswith("https://"):
            return False
        normalized = self._normalize_virtual_path(candidate)
        return bool(normalized) and normalized not in self._virtual_paths

    def find_stale_neighbor(self, source_path: str) -> Path | None:
        path = self.resolve_virtual_path(source_path)
        if path is not None:
            return path
        return None

    def classify_placeholder_source(self, source_path: str) -> str | None:
        normalized = self._normalize_virtual_path(source_path)
        if not normalized:
            return None
        if normalized in self._placeholder_missing_sources:
            return "qq_not_downloaded_local_placeholder"
        return None

    def _public_payload(self, *, action: str, mode: str) -> dict[str, Any] | None:
        if mode == "none":
            return None
        if mode == "valid_local":
            return {"file": self.local_path, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if mode == "valid_zero_local":
            return {"file": self.zero_local_path, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if mode == "valid_remote":
            remote_url = f"https://cdn.example.invalid/{self.scenario.name}/{self.file_name}"
            self.remote_map[remote_url] = self.remote_path
            return {"url": remote_url, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if mode == "valid_remote_only":
            remote_url = f"https://cdn.example.invalid/{self.scenario.name}/{self.file_name}"
            self.remote_map[remote_url] = self.remote_path
            return {"remote_url": remote_url, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if mode == "expired_remote":
            remote_url = f"https://cdn.example.invalid/expired/{self.scenario.name}/{self.file_name}"
            self.remote_failure_map[remote_url] = "expired_remote"
            return {"url": remote_url, "remote_url": remote_url, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if mode == "blank_payload":
            return {
                "file": "",
                "url": "",
                "file_name": self.file_name,
                "file_size": "1024",
                "asset_type": self.scenario.asset_type,
            }
        if mode == "known_bad_video":
            raise NapCatApiError("获取视频url失败")
        if mode == "known_bad_file":
            raise NapCatApiError("获取文件url失败")
        if mode == "known_bad_record":
            raise NapCatApiError("获取音频url失败")
        if mode == "timeout":
            raise NapCatApiTimeoutError(f"NapCat action timed out: {action}")
        if mode == "not_found":
            raise NapCatApiError("file not found")
        if mode == "opaque_error":
            raise NapCatApiError("simulated opaque public action error")
        raise ValueError(f"unsupported public result state: {mode}")

    def public_action_payload(
        self,
        *,
        scenario: AssetResolutionScenario,
        action: str,
        mode: str,
        token: str,
        file_id: str,
    ) -> dict[str, Any] | None:
        _ = scenario, token, file_id
        return self._public_payload(action=action, mode=mode)

    def context_payload(self, scenario: AssetResolutionScenario) -> dict[str, Any] | None:
        return self._top_level_payload_for_state(scenario.context_payload_state)

    def forward_payload(
        self,
        scenario: AssetResolutionScenario,
        *,
        materialize: bool,
    ) -> dict[str, Any] | None:
        state = (
            scenario.forward_materialize_state
            if materialize and scenario.forward_materialize_state != "inherit"
            else scenario.forward_metadata_state
            if not materialize and scenario.forward_metadata_state != "inherit"
            else scenario.forward_payload_state
        )
        if state == "none":
            return None
        if state == "timeout":
            raise NapCatFastHistoryTimeoutError("timed out")
        if state == "unavailable":
            raise NapCatFastHistoryUnavailable("route unavailable")
        if state == "error":
            raise RuntimeError("simulated forward route error")
        if state == "empty":
            return {"assets": [], "targeted_mode": "metadata_only"}
        asset_payload = self._asset_payload_for_state(state)
        return {"assets": [asset_payload], "targeted_mode": "single_target_download"}

    def _top_level_payload_for_state(self, state: str) -> dict[str, Any] | None:
        if state == "none":
            return None
        if state == "timeout":
            raise NapCatFastHistoryTimeoutError("timed out")
        if state == "unavailable":
            raise NapCatFastHistoryUnavailable("route unavailable")
        if state == "error":
            raise RuntimeError("simulated context route error")
        return self._asset_payload_for_state(state)

    def _asset_payload_for_state(self, state: str) -> dict[str, Any]:
        action = {
            "image": "get_image",
            "video": "get_file",
            "file": "get_file",
            "speech": "get_record",
        }.get(self.scenario.asset_type, "")
        if state == "local_path":
            return {"file": self.local_path, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if state == "zero_local":
            return {"file": self.zero_local_path, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if state == "empty_local":
            return {
                "file": "",
                "url": "",
                "file_name": self.file_name,
                "asset_type": self.scenario.asset_type,
            }
        if state == "stale_local":
            stale_path = str((self.root / "stale" / self.file_name).resolve())
            return {
                "file": stale_path,
                "url": stale_path,
                "file_name": self.file_name,
                "asset_type": self.scenario.asset_type,
            }
        if state == "public_token":
            return {
                "public_action": action,
                "public_file_token": f"token-{self.scenario.name}",
                "file_name": self.file_name,
                "asset_type": self.scenario.asset_type,
            }
        if state == "blank_public_payload":
            return {
                "public_action": action,
                "public_file_token": f"token-{self.scenario.name}",
                "file": "",
                "url": "",
                "file_name": self.file_name,
                "file_size": "2048",
                "asset_type": self.scenario.asset_type,
            }
        if state == "zero_public_payload":
            return {
                "public_action": action,
                "public_file_token": f"token-{self.scenario.name}",
                "file": self.zero_local_path,
                "file_name": self.file_name,
                "file_size": "2048",
                "asset_type": self.scenario.asset_type,
            }
        if state == "payload_file_id_only":
            return {
                "file_id": f"/payload-fileid/{self.scenario.name}",
                "file_name": self.file_name,
                "file_size": "2048",
                "asset_type": self.scenario.asset_type,
            }
        if state == "remote_url":
            remote_state = self.scenario.hint_remote_state if self.scenario.hint_remote_state != "none" else "live_http"
            remote_url = self._remote_url_for_state(remote_state)
            return {"url": remote_url, "remote_url": remote_url, "file_name": self.file_name, "asset_type": self.scenario.asset_type}
        if state == "blank_payload":
            return {
                "public_action": action,
                "public_file_token": f"token-{self.scenario.name}",
                "file_name": self.file_name,
                "file_size": "2048",
                "asset_type": self.scenario.asset_type,
            }
        raise ValueError(f"unsupported payload state: {state}")


def _prefetch_seed_for_scenario(scenario: AssetResolutionScenario) -> PrefetchSeed | None:
    return scenario.prefetch_seed if isinstance(scenario.prefetch_seed, PrefetchSeed) else None


def _seed_top_level_prefetch_payload(
    downloader: "_ScenarioAwareDownloader",
    runtime: _ScenarioRuntimeState,
    scenario: AssetResolutionScenario,
    *,
    payload_state: str,
) -> None:
    if payload_state in {"", "none"}:
        return
    payload = runtime._top_level_payload_for_state(payload_state)
    key = downloader._request_key(runtime.request)
    downloader._prefetched_media[key] = (None, None)
    downloader._prefetched_media_payloads[key] = copy.deepcopy(payload) if isinstance(payload, dict) else None


def _seed_forward_prefetch_payload(
    downloader: "_ScenarioAwareDownloader",
    runtime: _ScenarioRuntimeState,
    scenario: AssetResolutionScenario,
    *,
    payload_state: str,
) -> None:
    if payload_state in {"", "none"}:
        return
    payload = runtime._asset_payload_for_state(payload_state)
    key = downloader._request_key(runtime.request)
    downloader._prefetched_forward_media[key] = (None, None)
    downloader._prefetched_forward_media_payloads[key] = copy.deepcopy(payload) if isinstance(payload, dict) else None


def _seed_public_prefetch_result(
    downloader: "_ScenarioAwareDownloader",
    runtime: _ScenarioRuntimeState,
    scenario: AssetResolutionScenario,
    *,
    seed_state: str,
) -> None:
    if seed_state in {"", "none"}:
        return
    payload = downloader._direct_public_token_payload_for_request(runtime.request)
    if not isinstance(payload, dict):
        raise ValueError(f"scenario {scenario.name} cannot seed public prefetch without direct public token hint")
    action = str(payload.get("public_action") or "").strip()
    token = str(payload.get("public_file_token") or "").strip()
    if not action or not token:
        raise ValueError(f"scenario {scenario.name} produced incomplete public prefetch seed payload")
    cached_result: dict[str, Any] = {
        "payload": copy.deepcopy(payload),
        "resolved_path": None,
        "resolver": None,
        "remote_attempted": False,
    }
    cache_key = downloader._public_token_prefetch_key(
        request_data=runtime.request,
        action=action,
        token=token,
    )
    if seed_state == "remote_attempted_failed":
        cached_result["remote_attempted"] = True
        resolved_remote_url = downloader._resolve_remote_url(
            str(payload.get("remote_url") or payload.get("url") or "").strip()
        )
        if resolved_remote_url:
            cache_key_remote = (
                str(scenario.asset_type or "").strip(),
                downloader._normalized_match_url(resolved_remote_url),
            )
            downloader._store_remote_prefetch_result(
                cache_key_remote,
                None,
                generation=downloader._transient_state_generation,
            )
            downloader._remember_remote_media_failure_reason(
                resolved_remote_url,
                runtime.remote_failure_reason(resolved_remote_url),
            )
    elif seed_state == "terminal_cached":
        cached_result["resolver"] = "qq_expired_after_napcat"
    elif seed_state == "pending_future_payload_only":
        future: Future[dict[str, Any] | None] = Future()
        with downloader._prefetch_state_lock:
            downloader._public_token_prefetch_futures[cache_key] = future
        return
    elif seed_state == "done_not_finalized_payload_only":
        future = Future()
        future.set_result(cached_result)
        with downloader._prefetch_state_lock:
            downloader._public_token_prefetch_futures[cache_key] = future
        return
    elif seed_state != "payload_only":
        raise ValueError(f"unsupported public_prefetch_state: {seed_state}")
    downloader._store_public_token_prefetch_result(
        cache_key,
        cached_result,
        generation=downloader._transient_state_generation,
    )


def _seed_request_context_payload(
    runtime: _ScenarioRuntimeState,
    *,
    payload_state: str,
) -> None:
    if payload_state in {"", "none"}:
        return
    payload = runtime._top_level_payload_for_state(payload_state)
    runtime.request["_context_payload"] = copy.deepcopy(payload) if isinstance(payload, dict) else None
    if payload_state in {"empty_local", "stale_local"}:
        runtime.request["_context_hydration_yielded_no_local_path"] = True


def _seed_forward_timeout_cache(
    downloader: "_ScenarioAwareDownloader",
    runtime: _ScenarioRuntimeState,
    scenario: AssetResolutionScenario,
    *,
    cache_state: str,
) -> None:
    if cache_state in {"", "none"}:
        return
    if cache_state != "metadata_timeout":
        raise ValueError(f"unsupported forward_timeout_cache_state: {cache_state}")
    timeout_cache_key = downloader._forward_context_timeout_key(runtime.request, materialize=False)
    if timeout_cache_key is None:
        raise ValueError(f"scenario {scenario.name} cannot seed forward timeout cache without forward context hint")
    downloader._forward_context_timeout_cache.add(timeout_cache_key)


def _seed_prefetch_state(
    downloader: "_ScenarioAwareDownloader",
    runtime: _ScenarioRuntimeState,
    scenario: AssetResolutionScenario,
) -> None:
    seed = _prefetch_seed_for_scenario(scenario)
    if seed is None:
        return
    request_context_state = str(seed.request_context_payload_state or "").strip()
    if request_context_state not in {"", "none"}:
        _seed_request_context_payload(runtime, payload_state=request_context_state)
    prefetched_media_state = str(seed.prefetched_media_state or "").strip()
    if prefetched_media_state not in {"", "none"}:
        if prefetched_media_state != "payload_only":
            raise ValueError(f"unsupported prefetched_media_state: {prefetched_media_state}")
        payload_state = request_context_state or str(scenario.context_payload_state or "").strip()
        if payload_state in {"", "none"}:
            raise ValueError(f"scenario {scenario.name} cannot seed top-level prefetch without a payload state")
        _seed_top_level_prefetch_payload(
            downloader,
            runtime,
            scenario,
            payload_state=payload_state,
        )
    prefetched_forward_state = str(seed.prefetched_forward_state or "").strip()
    if prefetched_forward_state not in {"", "none"}:
        if prefetched_forward_state != "payload_only":
            raise ValueError(f"unsupported prefetched_forward_state: {prefetched_forward_state}")
        payload_state = str(scenario.forward_payload_state or "").strip()
        if payload_state in {"", "none"}:
            raise ValueError(f"scenario {scenario.name} cannot seed forward prefetch without forward_payload_state")
        _seed_forward_prefetch_payload(
            downloader,
            runtime,
            scenario,
            payload_state=payload_state,
        )
    public_prefetch_state = str(seed.public_prefetch_state or "").strip()
    if public_prefetch_state not in {"", "none"}:
        _seed_public_prefetch_result(
            downloader,
            runtime,
            scenario,
            seed_state=public_prefetch_state,
        )
    forward_timeout_cache_state = str(seed.forward_timeout_cache_state or "").strip()
    if forward_timeout_cache_state not in {"", "none"}:
        _seed_forward_timeout_cache(
            downloader,
            runtime,
            scenario,
            cache_state=forward_timeout_cache_state,
        )


def _path_kind_for_result(result: tuple[Path | None, str | None], state: _ScenarioRuntimeState) -> tuple[str, str | None]:
    resolved_path, _resolver = result
    if resolved_path is None:
        return "missing", None
    text = str(Path(resolved_path).resolve())
    return state.kind_map.get(text, "local"), text


def _canonicalize_simulated_resolver(resolver: str | None) -> str | None:
    value = str(resolver or "").strip()
    if not value:
        return None
    if value.endswith("_prefetched"):
        return value[: -len("_prefetched")]
    return value


def _promotion_behavior_to_algebra(
    *,
    asset_type: str,
    behavior: str,
) -> dict[str, Any]:
    if behavior in {"copied_local", "future_local_promotion", "recent_reuse"}:
        return {
            "resolution_result": "resolved",
            "terminality_class": "recovered",
            "materialization_status": "reused" if behavior == "recent_reuse" else "copied",
            "missing_kind": "none",
            "missing_bucket": "none",
            "bundle_behavior": (
                "future_local_promotion"
                if behavior == "future_local_promotion"
                else "recent_reuse"
                if behavior == "recent_reuse"
                else "immediate_copy"
            ),
            "requested_output_format": "default",
            "original_input_format": "unknown" if asset_type != "speech" else "unknown",
            "materialized_output_format": "unknown" if asset_type != "speech" else "unknown",
            "format_relation": "unknown",
            "output_name_relation": "kept_suffix",
        }
    return {
        "resolution_result": "missing",
        "terminality_class": "unresolved",
        "materialization_status": "missing",
        "missing_kind": "none",
        "missing_bucket": "none",
        "bundle_behavior": "unresolved",
        "requested_output_format": "default",
        "original_input_format": "unknown",
        "materialized_output_format": "unknown",
        "format_relation": "unknown",
        "output_name_relation": "unknown",
    }


def _run_asset_resolution_scenario_uncached(
    scenario: AssetResolutionScenario,
    *,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionResult:
    runtime = _ScenarioRuntimeState(scenario)
    events: list[dict[str, Any]] = []
    client = _ScenarioPublicClient(scenario, runtime)
    fast_client = _ScenarioFastClient(scenario, runtime)
    downloader = _ScenarioAwareDownloader(client, fast_client=fast_client, state=runtime)
    try:
        _seed_prefetch_state(downloader, runtime, scenario)
        request = copy.deepcopy(runtime.request)
        result = downloader.resolve_for_export(
            request,
            trace_callback=(lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event)))) if trace_callback is not None else events.append,
        )
        actual_path_kind, resolved_path = _path_kind_for_result(result, runtime)
        actual_resolver = _canonicalize_simulated_resolver(result[1])
        cost_matched = True
        if scenario.max_client_calls is not None and len(client.calls) > scenario.max_client_calls:
            cost_matched = False
        if scenario.max_fast_calls is not None and len(fast_client.calls) > scenario.max_fast_calls:
            cost_matched = False
        if scenario.max_remote_attempts is not None and len(runtime.remote_attempts) > scenario.max_remote_attempts:
            cost_matched = False
        matched = (
            actual_resolver == scenario.expected_resolver
            and actual_path_kind == scenario.expected_path_kind
            and cost_matched
        )
        trace_status_breakdown: dict[str, int] = {}
        for event in events:
            if str(event.get("phase") or "").strip() != "materialize_asset_substep":
                continue
            status = str(event.get("status") or "").strip()
            if not status:
                continue
            trace_status_breakdown[status] = trace_status_breakdown.get(status, 0) + 1
        algebra = _derive_result_algebra_projection(
            scenario=scenario,
            actual_resolver=actual_resolver,
            actual_path_kind=actual_path_kind,
        )
        join_snapshot = _derive_join_composition_snapshot(
            scenario=scenario,
            request=request,
            algebra=algebra,
        )
        return AssetResolutionResult(
            name=scenario.name,
            suite=scenario.suite,
            asset_type=scenario.asset_type,
            topology=scenario.topology,
            age_days=scenario.age_days,
            expected_resolver=scenario.expected_resolver,
            actual_resolver=actual_resolver,
            expected_path_kind=scenario.expected_path_kind,
            actual_path_kind=actual_path_kind,
            matched=matched,
            resolved_path=resolved_path,
            client_call_count=len(client.calls),
            fast_call_count=len(fast_client.calls),
            remote_attempt_count=len(runtime.remote_attempts),
            trace_event_count=len(events),
            trace_status_breakdown=trace_status_breakdown,
            cost_matched=cost_matched,
            algebra=algebra,
            join_snapshot=join_snapshot.to_dict(),
            notes=scenario.notes,
        )
    finally:
        downloader.close()
        runtime.close()


@lru_cache(maxsize=None)
def _run_asset_resolution_scenario_cached(
    scenario: AssetResolutionScenario,
) -> AssetResolutionResult:
    return _run_asset_resolution_scenario_uncached(scenario)


def run_asset_resolution_scenario(
    scenario: AssetResolutionScenario,
    *,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionResult:
    if trace_callback is None:
        return _run_asset_resolution_scenario_cached(scenario)
    return _run_asset_resolution_scenario_uncached(
        scenario,
        trace_callback=trace_callback,
    )


def _run_asset_resolution_sequence_uncached(
    scenario: AssetResolutionScenario,
    *,
    repeats: int = 3,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionSequenceResult:
    runtime = _ScenarioRuntimeState(scenario)
    events: list[dict[str, Any]] = []
    client = _ScenarioPublicClient(scenario, runtime)
    fast_client = _ScenarioFastClient(scenario, runtime)
    downloader = _ScenarioAwareDownloader(client, fast_client=fast_client, state=runtime)
    try:
        _seed_prefetch_state(downloader, runtime, scenario)
        sequence_results: list[tuple[str | None, str]] = []
        repeats = max(1, int(repeats))
        for _ in range(repeats):
            request = copy.deepcopy(runtime.request)
            result = downloader.resolve_for_export(
                request,
                trace_callback=(
                    (lambda event: (events.append(dict(event)), trace_callback and trace_callback(dict(event))))
                    if trace_callback is not None
                    else events.append
                ),
            )
            actual_path_kind, _resolved_path = _path_kind_for_result(result, runtime)
            sequence_results.append((_canonicalize_simulated_resolver(result[1]), actual_path_kind))
        unique_resolvers = tuple(dict.fromkeys(item[0] for item in sequence_results))
        unique_path_kinds = tuple(dict.fromkeys(item[1] for item in sequence_results))
        actual_resolver = sequence_results[-1][0]
        actual_path_kind = sequence_results[-1][1]
        cost_matched = True
        if scenario.max_client_calls is not None and len(client.calls) > scenario.max_client_calls:
            cost_matched = False
        if scenario.max_fast_calls is not None and len(fast_client.calls) > scenario.max_fast_calls:
            cost_matched = False
        if scenario.max_remote_attempts is not None and len(runtime.remote_attempts) > scenario.max_remote_attempts:
            cost_matched = False
        matched = (
            all(resolver == scenario.expected_resolver for resolver, _ in sequence_results)
            and all(path_kind == scenario.expected_path_kind for _, path_kind in sequence_results)
            and cost_matched
        )
        trace_status_breakdown: dict[str, int] = {}
        for event in events:
            if str(event.get("phase") or "").strip() != "materialize_asset_substep":
                continue
            status = str(event.get("status") or "").strip()
            if not status:
                continue
            trace_status_breakdown[status] = trace_status_breakdown.get(status, 0) + 1
        return AssetResolutionSequenceResult(
            name=scenario.name,
            suite=scenario.suite,
            repeats=repeats,
            expected_resolver=scenario.expected_resolver,
            expected_path_kind=scenario.expected_path_kind,
            actual_resolver=actual_resolver,
            actual_path_kind=actual_path_kind,
            matched=matched,
            unique_resolvers=unique_resolvers,
            unique_path_kinds=unique_path_kinds,
            client_call_count=len(client.calls),
            fast_call_count=len(fast_client.calls),
            remote_attempt_count=len(runtime.remote_attempts),
            trace_event_count=len(events),
            trace_status_breakdown=trace_status_breakdown,
            cost_matched=cost_matched,
            notes=scenario.notes,
        )
    finally:
        downloader.close()
        runtime.close()


@lru_cache(maxsize=None)
def _run_asset_resolution_sequence_cached(
    scenario: AssetResolutionScenario,
    repeats: int,
) -> AssetResolutionSequenceResult:
    return _run_asset_resolution_sequence_uncached(scenario, repeats=repeats)


def run_asset_resolution_sequence(
    scenario: AssetResolutionScenario,
    *,
    repeats: int = 3,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> AssetResolutionSequenceResult:
    normalized_repeats = max(1, int(repeats))
    if trace_callback is None:
        return _run_asset_resolution_sequence_cached(scenario, normalized_repeats)
    return _run_asset_resolution_sequence_uncached(
        scenario,
        repeats=normalized_repeats,
        trace_callback=trace_callback,
    )


def default_asset_resolution_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="top_level_image_hint_local_path",
            asset_type="image",
            suite="live_recovery_paths",
            hint_local_state="file_existing",
            expected_resolver="hint_local_path",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
            notes="Direct local hint should bypass NapCat calls.",
        ),
        AssetResolutionScenario(
            name="top_level_image_placeholder_zero_byte",
            asset_type="image",
            suite="classification_fast_fail",
            age_days=240,
            source_path_state="placeholder_zero",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes=(
                "Placeholder-only local shape is not terminal evidence by itself; "
                "without authoritative route failure proof it must remain unresolved, "
                "even if one cheap NapCat evidence probe is required."
            ),
        ),
        AssetResolutionScenario(
            name="top_level_image_public_token_remote",
            asset_type="image",
            suite="live_recovery_paths",
            context_payload_state="public_token",
            public_result_state="valid_remote",
            expected_resolver="napcat_public_token_get_image_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="top_level_video_public_token_local",
            asset_type="video",
            suite="live_recovery_paths",
            context_payload_state="public_token",
            public_result_state="valid_local",
            expected_resolver="napcat_public_token_get_file",
            expected_path_kind="local",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_video_old_blank_public_payload",
            asset_type="video",
            suite="classification_fast_fail",
            age_days=240,
            context_payload_state="public_token",
            public_result_state="blank_payload",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_file_direct_file_id_local",
            asset_type="file",
            suite="live_recovery_paths",
            direct_file_result_state="valid_local",
            expected_resolver="napcat_segment_file_id_get_file",
            expected_path_kind="local",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_speech_public_token_remote",
            asset_type="speech",
            suite="live_recovery_paths",
            context_payload_state="public_token",
            public_result_state="valid_remote",
            expected_resolver="napcat_public_token_get_record_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="top_level_sticker_remote_gif",
            asset_type="sticker",
            suite="live_recovery_paths",
            hint_remote_state="live_http",
            expected_resolver="sticker_remote_download",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="top_level_sticker_relative_remote_gif",
            asset_type="sticker",
            suite="live_recovery_paths",
            hint_remote_state="relative_http",
            expected_resolver="sticker_remote_download",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_image_remote_url_hit",
            asset_type="image",
            suite="live_recovery_paths",
            topology="forward",
            age_days=45,
            forward_payload_state="remote_url",
            hint_remote_state="live_http",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_old_image_terminal_without_payload",
            asset_type="image",
            suite="classification_fast_fail",
            topology="forward",
            age_days=240,
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes="Current downloader semantics classify forward image with no surviving payload or handle as terminally expired.",
        ),
        AssetResolutionScenario(
            name="forward_recent_video_public_token_local",
            asset_type="video",
            suite="live_recovery_paths",
            topology="forward",
            age_days=20,
            forward_payload_state="public_token",
            public_result_state="valid_local",
            expected_resolver="napcat_public_token_get_file",
            expected_path_kind="local",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_video_public_token_timeout",
            asset_type="video",
            suite="classification_fast_fail",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="public_token",
            public_result_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_video_metadata_timeout",
            asset_type="video",
            suite="classification_fast_fail",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_video_materialize_timeout",
            asset_type="video",
            suite="classification_fast_fail",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_metadata_state="none",
            forward_materialize_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=2,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_file_public_token_timeout",
            asset_type="file",
            suite="classification_fast_fail",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="public_token",
            public_result_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_speech_public_token_timeout",
            asset_type="speech",
            suite="classification_fast_fail",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="public_token",
            public_result_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_video_direct_file_id_local",
            asset_type="video",
            suite="live_recovery_paths",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            direct_file_result_state="valid_local",
            expected_resolver="napcat_segment_file_id_get_file",
            expected_path_kind="local",
            max_client_calls=1,
            max_fast_calls=2,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_video_known_bad_public_token",
            asset_type="video",
            suite="classification_fast_fail",
            topology="forward",
            age_days=30,
            forward_payload_state="public_token",
            public_result_state="known_bad_video",
            expected_resolver="napcat_video_url_unavailable",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_file_known_bad_public_token",
            asset_type="file",
            suite="classification_fast_fail",
            topology="forward",
            age_days=30,
            forward_payload_state="public_token",
            public_result_state="known_bad_file",
            expected_resolver="napcat_file_url_unavailable",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_speech_known_bad_public_token",
            asset_type="speech",
            suite="classification_fast_fail",
            topology="forward",
            age_days=30,
            forward_payload_state="public_token",
            public_result_state="known_bad_record",
            expected_resolver="napcat_record_url_unavailable",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_video_relative_remote_url",
            asset_type="video",
            suite="live_recovery_paths",
            topology="forward",
            age_days=20,
            forward_payload_state="remote_url",
            hint_remote_state="relative_http",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="top_level_video_context_timeout_direct_file_id_remote",
            asset_type="video",
            suite="live_recovery_paths",
            age_days=20,
            context_payload_state="timeout",
            direct_file_result_state="valid_remote",
            expected_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_old_video_route_unavailable",
            asset_type="video",
            suite="route_health",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="unavailable",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes="Very old forward video should degrade quickly when the forward route itself is unavailable.",
        ),
        AssetResolutionScenario(
            name="forward_old_file_route_unavailable",
            asset_type="file",
            suite="route_health",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="unavailable",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_old_speech_route_unavailable",
            asset_type="speech",
            suite="route_health",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="unavailable",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_video_missing_parent_element_id",
            asset_type="video",
            suite="forward_parent_shape",
            topology="forward_missing_parent",
            age_days=260,
            source_path_state="stale_missing",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes="Malformed forward parent should skip forward route and avoid repeated retries.",
        ),
        AssetResolutionScenario(
            name="forward_video_missing_parent_message_id",
            asset_type="video",
            suite="forward_parent_shape",
            topology="forward",
            forward_parent_state="missing_message_id_raw",
            age_days=260,
            source_path_state="stale_missing",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_video_stale_path_live_remote_url",
            asset_type="video",
            suite="live_recovery_paths",
            topology="forward",
            age_days=260,
            source_path_state="stale_missing",
            forward_payload_state="remote_url",
            hint_remote_state="live_http",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="top_level_video_old_context_route_unavailable",
            asset_type="video",
            suite="route_health",
            age_days=240,
            source_path_state="stale_missing",
            context_payload_state="unavailable",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
    ]


def _exhaustive_old_forward_terminal_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    signal_specs: dict[str, dict[str, Any]] = {
        "payload_timeout": {
            "forward_payload_state": "timeout",
            "max_client_calls": 0,
            "max_fast_calls": 1,
            "max_remote_attempts": 0,
        },
        "payload_unavailable": {
            "forward_payload_state": "unavailable",
            "max_client_calls": 0,
            "max_fast_calls": 1,
            "max_remote_attempts": 0,
        },
        "materialize_empty": {
            "forward_metadata_state": "none",
            "forward_materialize_state": "empty",
            "max_client_calls": 0,
            "max_fast_calls": 2,
            "max_remote_attempts": 0,
        },
        "materialize_error": {
            "forward_metadata_state": "none",
            "forward_materialize_state": "error",
            "max_client_calls": 0,
            "max_fast_calls": 2,
            "max_remote_attempts": 0,
        },
        "materialize_zero_local": {
            "forward_metadata_state": "none",
            "forward_materialize_state": "zero_local",
            "max_client_calls": None,
            "max_fast_calls": 2,
            "max_remote_attempts": 0,
        },
        "public_timeout": {
            "forward_payload_state": "public_token",
            "public_result_state": "timeout",
            "max_client_calls": 1,
            "max_fast_calls": 2,
            "max_remote_attempts": 0,
        },
        "public_blank_payload": {
            "forward_payload_state": "public_token",
            "public_result_state": "blank_payload",
            "max_client_calls": 1,
            "max_fast_calls": 2,
            "max_remote_attempts": 0,
        },
        "public_not_found": {
            "forward_payload_state": "public_token",
            "public_result_state": "not_found",
            "max_client_calls": 1,
            "max_fast_calls": 2,
            "max_remote_attempts": 0,
        },
    }
    for topology in ("forward", "nested_forward"):
        for asset_type in ("video", "file", "speech"):
            for source_state in ("none", "stale_missing", "existing_zero"):
                for signal_name, spec in signal_specs.items():
                    max_client_calls = spec["max_client_calls"]
                    if signal_name == "materialize_zero_local" and asset_type in {"video", "file"}:
                        max_client_calls = 1
                    scenarios.append(
                        AssetResolutionScenario(
                            name=f"exhaustive_{topology}_{asset_type}_{source_state}_{signal_name}",
                            suite="exhaustive_old_forward_terminal",
                            asset_type=asset_type,
                            topology=topology,
                            age_days=260,
                            source_path_state=source_state,
                            expected_resolver="qq_expired_after_napcat",
                            expected_path_kind="missing",
                            max_client_calls=max_client_calls,
                            max_fast_calls=spec["max_fast_calls"],
                            max_remote_attempts=spec["max_remote_attempts"],
                            notes=(
                                "Bounded exhaustive old-forward terminal audit over source-state and "
                                "terminal failure signal combinations."
                            ),
                            **{
                                key: value
                                for key, value in spec.items()
                                if key
                                not in {"max_client_calls", "max_fast_calls", "max_remote_attempts"}
                            },
                        )
                    )
    return scenarios


def _terminal_evidence_age_invariance_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for age_label, age_days in (("recent", 7), ("old", 260)):
        scenarios.extend(
            [
                AssetResolutionScenario(
                    name=f"top_level_image_public_token_dead_remote_{age_label}",
                    suite="terminal_evidence_age_invariance",
                    asset_type="image",
                    topology="top_level",
                    age_days=age_days,
                    source_path_state="stale_missing",
                    context_payload_state="public_token",
                    public_result_state="expired_remote",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=1,
                    max_remote_attempts=1,
                    notes="Dead public-token remote for a top-level image should classify terminally regardless of age.",
                ),
                AssetResolutionScenario(
                    name=f"forward_image_dead_remote_public_timeout_{age_label}",
                    suite="terminal_evidence_age_invariance",
                    asset_type="image",
                    topology="forward",
                    age_days=age_days,
                    hint_remote_state="expired_pair",
                    forward_payload_state="public_token",
                    public_result_state="timeout",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=1,
                    max_remote_attempts=2,
                    notes="Dead remote URL plus failed public token should classify terminally regardless of age.",
                ),
                AssetResolutionScenario(
                    name=f"forward_image_no_payload_terminal_{age_label}",
                    suite="terminal_evidence_age_invariance",
                    asset_type="image",
                    topology="forward",
                    age_days=age_days,
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes="Forward image with no surviving payload now classifies terminally regardless of age.",
                ),
                AssetResolutionScenario(
                    name=f"forward_video_blank_public_payload_{age_label}",
                    suite="terminal_evidence_age_invariance",
                    asset_type="video",
                    topology="forward",
                    age_days=age_days,
                    source_path_state="stale_missing",
                    forward_payload_state="public_token",
                    public_result_state="blank_payload",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes="Blank public get_file payload should classify terminally regardless of age.",
                ),
                AssetResolutionScenario(
                    name=f"forward_video_direct_not_found_{age_label}",
                    suite="terminal_evidence_age_invariance",
                    asset_type="video",
                    topology="forward",
                    age_days=age_days,
                    source_path_state="stale_missing",
                    direct_file_result_state="not_found",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes="Direct file-id not-found should classify terminally regardless of age.",
                ),
                AssetResolutionScenario(
                    name=f"forward_speech_blank_public_payload_{age_label}",
                    suite="terminal_evidence_age_invariance",
                    asset_type="speech",
                    topology="forward",
                    age_days=age_days,
                    source_path_state="stale_missing",
                    forward_payload_state="public_token",
                    public_result_state="blank_payload",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes="Blank public get_record payload should classify terminally regardless of age.",
                ),
            ]
        )
        for asset_type in ("video", "file", "speech"):
            token_action = "get_record" if asset_type == "speech" else "get_file"
            scenarios.extend(
                [
                    AssetResolutionScenario(
                        name=f"forward_{asset_type}_public_timeout_{age_label}",
                        suite="terminal_evidence_age_invariance",
                        asset_type=asset_type,
                        topology="forward",
                        age_days=age_days,
                        source_path_state="stale_missing",
                        forward_payload_state="public_token",
                        public_result_state="timeout",
                        expected_resolver="qq_expired_after_napcat",
                        expected_path_kind="missing",
                        max_client_calls=1,
                        max_fast_calls=1,
                        max_remote_attempts=0,
                        notes=f"{token_action} timeout should classify terminally regardless of age.",
                    ),
                    AssetResolutionScenario(
                        name=f"forward_{asset_type}_metadata_timeout_{age_label}",
                        suite="terminal_evidence_age_invariance",
                        asset_type=asset_type,
                        topology="forward",
                        age_days=age_days,
                        source_path_state="stale_missing",
                        forward_metadata_state="timeout",
                        expected_resolver="qq_expired_after_napcat",
                        expected_path_kind="missing",
                        max_client_calls=0,
                        max_fast_calls=1,
                        max_remote_attempts=0,
                        notes="Forward metadata timeout should classify terminally regardless of age.",
                    ),
                ]
            )
    return scenarios


def _request_state_payload_state_terminal_equivalence_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for age_label, age_days in (("recent", 7), ("old", 260)):
        scenarios.extend(
            [
                AssetResolutionScenario(
                    name=f"top_level_image_weak_gchatpic_context_no_path_{age_label}",
                    suite="request_state_payload_state_terminal_equivalence",
                    asset_type="image",
                    topology="top_level",
                    age_days=age_days,
                    source_path_state="placeholder_zero",
                    hint_remote_state="relative_gchatpic",
                    context_payload_state="empty_local",
                    direct_file_result_state="valid_local",
                    expected_resolver="qq_not_downloaded_local_placeholder",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes=(
                        "Weak relative gchatpic plus placeholder local evidence and context "
                        "hydration yielding no local path should settle as placeholder "
                        "background missing regardless of age."
                    ),
                ),
                AssetResolutionScenario(
                    name=f"top_level_image_weak_gchatpic_context_stale_local_{age_label}",
                    suite="request_state_payload_state_terminal_equivalence",
                    asset_type="image",
                    topology="top_level",
                    age_days=age_days,
                    source_path_state="placeholder_zero",
                    hint_remote_state="relative_gchatpic",
                    context_payload_state="stale_local",
                    direct_file_result_state="valid_local",
                    expected_resolver="qq_not_downloaded_local_placeholder",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes=(
                        "A stale local path returned by context hydration must be treated the "
                        "same as an empty local result under the weak gchatpic proof chain."
                    ),
                ),
                AssetResolutionScenario(
                    name=f"top_level_image_local_download_dead_{age_label}",
                    suite="request_state_payload_state_terminal_equivalence",
                    asset_type="image",
                    topology="top_level",
                    age_days=age_days,
                    source_path_state="placeholder_zero",
                    hint_remote_state="relative_download_dead",
                    context_payload_state="public_token",
                    public_result_state="none",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                    notes=(
                        "Projected localhost /download unsupported plus placeholder local "
                        "evidence and failed direct public-token fetch must classify "
                        "terminally regardless of age."
                    ),
                ),
            ]
        )
    return scenarios


def _prefetch_seeded_image_interaction_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="top_level_image_prefetch_payload_only_gchatpic_empty_local",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="top_level",
            source_path_state="placeholder_zero",
            hint_remote_state="relative_gchatpic",
            hint_file_id_state="public_token",
            context_payload_state="empty_local",
            prefetch_seed=PrefetchSeed(
                request_context_payload_state="empty_local",
                prefetched_media_state="payload_only",
                public_prefetch_state="payload_only",
            ),
            expected_resolver="qq_not_downloaded_local_placeholder",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_image_prefetch_payload_only_gchatpic_stale_local",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="top_level",
            source_path_state="placeholder_zero",
            hint_remote_state="relative_gchatpic",
            hint_file_id_state="public_token",
            context_payload_state="stale_local",
            prefetch_seed=PrefetchSeed(
                request_context_payload_state="stale_local",
                prefetched_media_state="payload_only",
                public_prefetch_state="payload_only",
            ),
            expected_resolver="qq_not_downloaded_local_placeholder",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_image_prefetch_remote_failed_download_dead_empty_local",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="top_level",
            source_path_state="placeholder_zero",
            hint_remote_state="relative_download_dead",
            hint_file_id_state="public_token",
            context_payload_state="empty_local",
            prefetch_seed=PrefetchSeed(
                request_context_payload_state="empty_local",
                public_prefetch_state="remote_attempted_failed",
            ),
            expected_resolver="qq_not_downloaded_local_placeholder",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_image_prefetch_remote_failed_download_dead_stale_local",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="top_level",
            source_path_state="placeholder_zero",
            hint_remote_state="relative_download_dead",
            hint_file_id_state="public_token",
            context_payload_state="stale_local",
            prefetch_seed=PrefetchSeed(
                request_context_payload_state="stale_local",
                public_prefetch_state="remote_attempted_failed",
            ),
            expected_resolver="qq_not_downloaded_local_placeholder",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_image_prefetch_payload_only_dead_remote_terminal",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="forward",
            source_path_state="stale_missing",
            hint_remote_state="expired_pair",
            forward_payload_state="remote_url",
            prefetch_seed=PrefetchSeed(
                prefetched_forward_state="payload_only",
                forward_timeout_cache_state="metadata_timeout",
            ),
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=2,
        ),
        AssetResolutionScenario(
            name="forward_image_prefetch_payload_only_no_remote_terminal",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="forward",
            source_path_state="none",
            hint_remote_state="none",
            forward_payload_state="empty_local",
            prefetch_seed=PrefetchSeed(
                prefetched_forward_state="payload_only",
                forward_timeout_cache_state="metadata_timeout",
            ),
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="nested_forward_image_prefetch_payload_only_dead_remote_terminal",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="nested_forward",
            source_path_state="stale_missing",
            hint_remote_state="expired_pair",
            forward_payload_state="remote_url",
            prefetch_seed=PrefetchSeed(
                prefetched_forward_state="payload_only",
                forward_timeout_cache_state="metadata_timeout",
            ),
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=2,
        ),
        AssetResolutionScenario(
            name="nested_forward_image_prefetch_payload_only_live_remote_wins",
            suite="prefetch_seeded_image_interactions",
            asset_type="image",
            topology="nested_forward",
            source_path_state="none",
            hint_remote_state="live_http",
            forward_payload_state="remote_url",
            prefetch_seed=PrefetchSeed(
                prefetched_forward_state="payload_only",
                forward_timeout_cache_state="metadata_timeout",
            ),
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
        ),
    ]


def _prefetch_seeded_forward_media_interaction_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    public_remote_resolver = {
        "video": "napcat_public_token_get_file_remote_url",
        "file": "napcat_public_token_get_file_remote_url",
        "speech": "napcat_public_token_get_record_remote_url",
    }
    for asset_type in ("video", "file", "speech"):
        scenarios.extend(
            [
                AssetResolutionScenario(
                    name=f"forward_{asset_type}_prefetch_public_payload_only_live_remote_wins",
                    suite="prefetch_seeded_forward_media_interactions",
                    asset_type=asset_type,
                    topology="forward",
                    source_path_state="none",
                    hint_remote_state="live_http",
                    hint_file_id_state="public_token",
                    forward_payload_state="public_token",
                    public_result_state="valid_remote_only",
                    prefetch_seed=PrefetchSeed(public_prefetch_state="payload_only"),
                    expected_resolver="napcat_forward_remote_url",
                    expected_path_kind="remote",
                    max_client_calls=0,
                    max_fast_calls=0,
                    max_remote_attempts=1,
                ),
                AssetResolutionScenario(
                    name=f"forward_{asset_type}_prefetch_public_remote_failed_terminal",
                    suite="prefetch_seeded_forward_media_interactions",
                    asset_type=asset_type,
                    topology="forward",
                    source_path_state="stale_missing",
                    hint_remote_state="expired_pair",
                    hint_file_id_state="public_token",
                    forward_payload_state="public_token",
                    public_result_state="not_found",
                    prefetch_seed=PrefetchSeed(
                        public_prefetch_state="remote_attempted_failed",
                        forward_timeout_cache_state="metadata_timeout",
                    ),
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=1,
                    max_remote_attempts=3,
                ),
                AssetResolutionScenario(
                    name=f"nested_forward_{asset_type}_prefetch_payload_only_live_forward_remote_wins",
                    suite="prefetch_seeded_forward_media_interactions",
                    asset_type=asset_type,
                    topology="nested_forward",
                    source_path_state="none",
                    hint_remote_state="live_http",
                    forward_payload_state="remote_url",
                    prefetch_seed=PrefetchSeed(
                        prefetched_forward_state="payload_only",
                        forward_timeout_cache_state="metadata_timeout",
                    ),
                    expected_resolver="napcat_forward_remote_url",
                    expected_path_kind="remote",
                    max_client_calls=0,
                    max_fast_calls=0,
                    max_remote_attempts=1,
                ),
                AssetResolutionScenario(
                    name=f"nested_forward_{asset_type}_prefetch_payload_only_no_remote_nonterminal",
                    suite="prefetch_seeded_forward_media_interactions",
                    asset_type=asset_type,
                    topology="nested_forward",
                    source_path_state="none",
                    hint_remote_state="none",
                    forward_payload_state="empty_local",
                    prefetch_seed=PrefetchSeed(
                        prefetched_forward_state="payload_only",
                        forward_timeout_cache_state="metadata_timeout",
                    ),
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=0,
                    max_remote_attempts=0,
                ),
            ]
        )
    return scenarios


def _exhaustive_sticker_forward_parent_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for topology in ("forward", "nested_forward"):
        for parent_state in (
            "missing_element_id",
            "missing_message_id_raw",
            "missing_peer_uid",
            "blank_parent_bundle",
        ):
            scenarios.append(
                AssetResolutionScenario(
                    name=f"exhaustive_{topology}_sticker_{parent_state}_no_remote",
                    suite="exhaustive_sticker_forward_parent",
                    asset_type="sticker",
                    topology=topology,
                    forward_parent_state=parent_state,
                    age_days=20,
                    expected_resolver=None,
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                )
            )
            for remote_state in ("live_http", "relative_http"):
                scenarios.append(
                    AssetResolutionScenario(
                        name=f"exhaustive_{topology}_sticker_{parent_state}_{remote_state}",
                        suite="exhaustive_sticker_forward_parent",
                        asset_type="sticker",
                        topology=topology,
                        forward_parent_state=parent_state,
                        age_days=20,
                        hint_remote_state=remote_state,
                        expected_resolver="sticker_remote_download",
                        expected_path_kind="remote",
                        max_client_calls=0,
                        max_fast_calls=1,
                        max_remote_attempts=1,
                    )
                )
    return scenarios


def _exhaustive_local_path_state_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for asset_type in ("image", "video", "file", "speech"):
        scenarios.append(
            AssetResolutionScenario(
                name=f"exhaustive_top_level_{asset_type}_source_existing",
                suite="exhaustive_local_path_states",
                asset_type=asset_type,
                topology="top_level",
                source_path_state="existing",
                expected_resolver="source_local_path",
                expected_path_kind="local",
                max_client_calls=0,
                max_fast_calls=0,
                max_remote_attempts=0,
            )
        )
        scenarios.append(
            AssetResolutionScenario(
                name=f"exhaustive_top_level_{asset_type}_source_existing_zero",
                suite="exhaustive_local_path_states",
                asset_type=asset_type,
                topology="top_level",
                source_path_state="existing_zero",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1 if asset_type != "image" else 2,
                max_remote_attempts=0,
            )
        )
        scenarios.append(
            AssetResolutionScenario(
                name=f"exhaustive_top_level_{asset_type}_hint_path_existing",
                suite="exhaustive_local_path_states",
                asset_type=asset_type,
                topology="top_level",
                hint_local_state="path_existing",
                expected_resolver="hint_local_path",
                expected_path_kind="local",
                max_client_calls=0,
                max_fast_calls=0,
                max_remote_attempts=0,
            )
        )
        scenarios.append(
            AssetResolutionScenario(
                name=f"exhaustive_top_level_{asset_type}_hint_path_zero",
                suite="exhaustive_local_path_states",
                asset_type=asset_type,
                topology="top_level",
                hint_local_state="path_zero",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1 if asset_type != "image" else 2,
                max_remote_attempts=0,
            )
        )
        scenarios.append(
            AssetResolutionScenario(
                name=f"exhaustive_top_level_{asset_type}_hint_stale_local_url",
                suite="exhaustive_local_path_states",
                asset_type=asset_type,
                topology="top_level",
                hint_local_state="stale_local_url",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1 if asset_type != "image" else 2,
                max_remote_attempts=0,
            )
        )
    for asset_type in ("image", "video", "file", "speech", "sticker"):
        kwargs: dict[str, Any] = {
            "name": f"exhaustive_forward_{asset_type}_stale_http_remote_missing",
            "suite": "exhaustive_local_path_states",
            "asset_type": asset_type,
            "topology": "forward",
            "age_days": 20,
            "hint_remote_state": "stale_http",
            "expected_resolver": None,
            "expected_path_kind": "missing",
            "max_client_calls": 0,
            "max_fast_calls": 1,
            "max_remote_attempts": 1,
            "notes": "Dead remote URL should fail after one bounded remote attempt, not silently count as local/hydrated.",
        }
        if asset_type != "sticker":
            kwargs["source_path_state"] = "stale_missing"
            kwargs["forward_payload_state"] = "remote_url"
            kwargs["max_fast_calls"] = 1 if asset_type == "image" else 2
        scenarios.append(AssetResolutionScenario(**kwargs))
    return scenarios


def _exhaustive_old_forward_direct_file_id_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for topology in ("forward", "nested_forward"):
        for asset_type in ("video", "file"):
            for source_state in ("none", "stale_missing", "existing_zero"):
                for direct_state in ("blank_payload", "timeout", "not_found"):
                    scenarios.append(
                        AssetResolutionScenario(
                            name=f"exhaustive_{topology}_{asset_type}_{source_state}_{direct_state}_direct_file_id",
                            suite="exhaustive_old_forward_direct_file_id",
                            asset_type=asset_type,
                            topology=topology,
                            age_days=260,
                            source_path_state=source_state,
                            direct_file_result_state=direct_state,
                            expected_resolver="qq_expired_after_napcat",
                            expected_path_kind="missing",
                            max_client_calls=1,
                            max_fast_calls=1,
                            max_remote_attempts=0,
                            notes=(
                                "Very old forward video/file assets with only direct file-id fallback "
                                "must classify as expired on blank/timeout/not_found without spilling "
                                "into targeted materialize."
                            ),
                        )
                    )
    return scenarios


def _exhaustive_public_token_shape_drift_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    resolver_by_asset_type = {
        "image": "napcat_public_token_get_image",
        "video": "napcat_public_token_get_file",
        "file": "napcat_public_token_get_file",
        "speech": "napcat_public_token_get_record",
    }
    for topology in ("top_level", "forward", "nested_forward"):
        for asset_type in ("image", "video", "file", "speech"):
            payload_fields: dict[str, Any]
            if topology == "top_level":
                payload_fields = {
                    "context_payload_state": "public_token",
                }
            else:
                payload_fields = {
                    "forward_payload_state": "public_token",
                }
            for fallback_state, expected_path_kind in (
                ("valid_local", "local"),
                ("valid_remote", "remote"),
                ("valid_remote_only", "remote"),
            ):
                scenario_kwargs = {
                    "name": f"public_token_shape_drift_{topology}_{asset_type}_{fallback_state}",
                    "suite": "public_token_shape_drift",
                    "asset_type": asset_type,
                    "topology": topology,
                    "age_days": 20,
                    "public_result_state": "opaque_error",
                    "public_fallback_result_state": fallback_state,
                    "expected_resolver": (
                        resolver_by_asset_type[asset_type]
                        if expected_path_kind == "local"
                        else f"{resolver_by_asset_type[asset_type]}_remote_url"
                    ),
                    "expected_path_kind": expected_path_kind,
                    "max_client_calls": 2,
                    "max_fast_calls": 1,
                    "max_remote_attempts": 1 if fallback_state in {"valid_remote", "valid_remote_only"} else 0,
                    "notes": (
                        "Bounded compatibility coverage for NapCat runtimes that only honor "
                        "`file_id=<token>` after rejecting `file=<token>`."
                    ),
                    **payload_fields,
                }
                if topology != "top_level":
                    scenario_kwargs["source_path_state"] = "none"
                scenarios.append(AssetResolutionScenario(**scenario_kwargs))
    return scenarios


def _exhaustive_old_forward_payload_file_id_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for topology in ("forward", "nested_forward"):
        for asset_type in ("video", "file"):
            for source_state in ("none", "stale_missing", "existing_zero"):
                for direct_state in ("blank_payload", "timeout", "not_found"):
                    scenarios.append(
                        AssetResolutionScenario(
                            name=f"exhaustive_{topology}_{asset_type}_{source_state}_{direct_state}_payload_file_id",
                            suite="exhaustive_old_forward_payload_file_id",
                            asset_type=asset_type,
                            topology=topology,
                            age_days=260,
                            source_path_state=source_state,
                            forward_metadata_state="payload_file_id_only",
                            direct_file_result_state=direct_state,
                            expected_resolver="qq_expired_after_napcat",
                            expected_path_kind="missing",
                            max_client_calls=1,
                            max_fast_calls=1,
                            max_remote_attempts=0,
                            notes=(
                                "Very old forwarded file/video assets whose only surviving direct-file-id "
                                "arrives in the forward metadata payload should prefer direct-file-id "
                                "before targeted materialize and classify quickly on terminal failures."
                            ),
                        )
                    )
    return scenarios


def _exhaustive_old_public_zero_byte_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for topology in ("top_level", "forward", "nested_forward"):
        for asset_type in ("video", "file", "speech"):
            for source_state in ("none", "stale_missing"):
                scenario_kwargs: dict[str, Any] = {
                    "name": f"exhaustive_{topology}_{asset_type}_{source_state}_public_zero_local",
                    "suite": "exhaustive_old_public_zero_byte",
                    "asset_type": asset_type,
                    "topology": topology,
                    "age_days": 260,
                    "source_path_state": source_state,
                    "public_result_state": "valid_zero_local",
                    "expected_resolver": "qq_expired_after_napcat",
                    "expected_path_kind": "missing",
                    "max_client_calls": 1,
                    "max_fast_calls": 1 if topology == "top_level" else 2,
                    "max_remote_attempts": 0,
                    "notes": (
                        "Old public-token payloads that only expose an existing zero-byte local file "
                        "should classify as expired instead of leaking through as ambiguous missing."
                    ),
                }
                if topology == "top_level":
                    scenario_kwargs["context_payload_state"] = "public_token"
                else:
                    scenario_kwargs["forward_payload_state"] = "public_token"
                scenarios.append(AssetResolutionScenario(**scenario_kwargs))
    return scenarios


def _exhaustive_forward_image_terminal_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    terminal_signal_specs: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "metadata_empty_materialize_empty",
            {
                "forward_metadata_state": "empty",
                "forward_materialize_state": "empty",
                "max_client_calls": 0,
                "max_fast_calls": 1,
                "max_remote_attempts": 2,
            },
        ),
        (
            "metadata_error_materialize_error",
            {
                "forward_metadata_state": "error",
                "forward_materialize_state": "error",
                "max_client_calls": 0,
                "max_fast_calls": 1,
                "max_remote_attempts": 2,
            },
        ),
        (
            "metadata_unavailable_terminal",
            {
                "forward_metadata_state": "unavailable",
                "forward_materialize_state": "unavailable",
                "max_client_calls": 0,
                "max_fast_calls": 1,
                "max_remote_attempts": 2,
            },
        ),
        (
            "metadata_timeout_materialize_empty",
            {
                "forward_metadata_state": "timeout",
                "forward_materialize_state": "empty",
                "max_client_calls": 0,
                "max_fast_calls": 1,
                "max_remote_attempts": 2,
            },
        ),
        (
            "metadata_timeout_materialize_error",
            {
                "forward_metadata_state": "timeout",
                "forward_materialize_state": "error",
                "max_client_calls": 0,
                "max_fast_calls": 1,
                "max_remote_attempts": 2,
            },
        ),
        (
            "metadata_timeout_materialize_unavailable",
            {
                "forward_metadata_state": "timeout",
                "forward_materialize_state": "unavailable",
                "max_client_calls": 0,
                "max_fast_calls": 1,
                "max_remote_attempts": 2,
            },
        ),
    )
    for topology in ("forward", "nested_forward"):
        for age_label, age_days in (("recent", 7), ("old", 260)):
            for source_state in ("none", "stale_missing"):
                for signal_name, spec in terminal_signal_specs:
                    expected_resolver = "qq_expired_after_napcat"
                    scenario_spec = dict(spec)
                    if source_state == "none" and signal_name.startswith("metadata_timeout"):
                        scenario_spec["max_fast_calls"] = 0
                    scenarios.append(
                        AssetResolutionScenario(
                            name=f"exhaustive_{topology}_image_{age_label}_{source_state}_dead_remote_{signal_name}",
                            suite="exhaustive_forward_image_terminal",
                            asset_type="image",
                            topology=topology,
                            age_days=age_days,
                            source_path_state=source_state,
                            hint_remote_state="expired_pair",
                            expected_resolver=expected_resolver,
                            expected_path_kind="missing",
                            notes=(
                                "Expired original remote plus unsupported projected localhost route provides "
                                "strong remote terminal evidence. When no public/local recovery handle exists, "
                                "that remote evidence alone is sufficient to classify terminally."
                            ),
                            **scenario_spec,
                        )
                    )
            for signal_name, signal_state, signal_kwargs in (
                ("metadata_timeout_terminal", "timeout", {"max_fast_calls": 2}),
                ("metadata_unavailable_terminal", "unavailable", {"max_fast_calls": 2}),
            ):
                scenarios.append(
                    AssetResolutionScenario(
                        name=f"exhaustive_{topology}_image_{age_label}_no_remote_{signal_name}",
                        suite="exhaustive_forward_image_terminal",
                        asset_type="image",
                        topology=topology,
                        age_days=age_days,
                        source_path_state="none",
                        forward_metadata_state=signal_state,
                        expected_resolver="qq_expired_after_napcat",
                        expected_path_kind="missing",
                        max_client_calls=0,
                        max_remote_attempts=0,
                        notes=(
                            "Current downloader semantics treat no-remote forward image plus metadata terminal signal as expired."
                        ),
                        **signal_kwargs,
                    )
                )
        for remote_state, signal_state in (
            ("live_http", "timeout"),
            ("relative_http", "unavailable"),
        ):
            scenarios.append(
                AssetResolutionScenario(
                    name=f"exhaustive_{topology}_image_{remote_state}_{signal_state}_remote_wins",
                    suite="exhaustive_forward_image_terminal",
                    asset_type="image",
                    topology=topology,
                    age_days=20,
                    source_path_state="none",
                    hint_remote_state=remote_state,
                    forward_metadata_state=signal_state,
                    expected_resolver="napcat_forward_remote_url",
                    expected_path_kind="remote",
                    max_client_calls=0,
                    max_fast_calls=0,
                    max_remote_attempts=1,
                    notes=(
                        "Live forward remote URL remains stronger evidence than a metadata timeout/unavailable signal."
                    ),
                )
            )
    return scenarios


def _partial_parent_handle_sufficient_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="partial_parent_forward_image_hint_local_existing_recovers",
            suite="partial_parent_handle_sufficient",
            asset_type="image",
            topology="forward",
            forward_parent_state="missing_element_id",
            hint_local_state="path_existing",
            expected_resolver="hint_local_path",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
            notes="A broken forward parent must not suppress an already-usable direct local hint for image.",
        ),
        AssetResolutionScenario(
            name="partial_parent_nested_forward_file_hint_local_existing_recovers",
            suite="partial_parent_handle_sufficient",
            asset_type="file",
            topology="nested_forward",
            forward_parent_state="missing_message_id_raw",
            hint_local_state="file_existing",
            expected_resolver="hint_local_path",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
            notes="A malformed nested-forward parent must not block a surviving local file hint.",
        ),
        AssetResolutionScenario(
            name="partial_parent_forward_image_live_remote_survives",
            suite="partial_parent_handle_sufficient",
            asset_type="image",
            topology="forward",
            forward_parent_state="blank_parent_bundle",
            hint_remote_state="live_http",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
            notes="Parent-scoped route loss must not kill a surviving live remote image handle.",
        ),
        AssetResolutionScenario(
            name="partial_parent_nested_forward_video_direct_file_id_survives",
            suite="partial_parent_handle_sufficient",
            asset_type="video",
            topology="nested_forward",
            forward_parent_state="missing_peer_uid",
            hint_file_id_state="direct_file_id",
            direct_file_result_state="valid_remote",
            expected_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
            notes="Direct file-id should remain sufficient even when nested-forward parent context is partial.",
        ),
        AssetResolutionScenario(
            name="partial_parent_forward_speech_public_token_survives",
            suite="partial_parent_handle_sufficient",
            asset_type="speech",
            topology="forward",
            forward_parent_state="missing_message_id_raw",
            hint_file_id_state="public_token",
            public_result_state="valid_remote",
            expected_resolver="napcat_public_token_get_record_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
            notes="Speech public-token recovery must survive partial forward parent damage.",
        ),
        AssetResolutionScenario(
            name="partial_parent_forward_image_public_token_survives",
            suite="partial_parent_handle_sufficient",
            asset_type="image",
            topology="forward",
            forward_parent_state="missing_peer_uid",
            hint_file_id_state="public_token",
            hint_remote_state="live_http",
            public_result_state="valid_remote_only",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
            notes="A surviving direct public token must override broken parent context for forward image.",
        ),
        AssetResolutionScenario(
            name="partial_parent_nested_forward_file_public_token_survives",
            suite="partial_parent_handle_sufficient",
            asset_type="file",
            topology="nested_forward",
            forward_parent_state="missing_element_id",
            hint_file_id_state="public_token",
            public_result_state="valid_remote_only",
            expected_resolver="napcat_public_token_get_file_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
            notes="Broken nested-forward parent must not prevent direct public-token file recovery.",
        ),
        AssetResolutionScenario(
            name="partial_parent_forward_image_no_surviving_handle_unresolved",
            suite="partial_parent_handle_sufficient",
            asset_type="image",
            topology="forward",
            forward_parent_state="missing_element_id",
            source_path_state="none",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=2,
            max_remote_attempts=0,
            notes="When the parent is partial and no parent-independent handle survives, the case must remain unresolved.",
        ),
    ]


def _top_level_speech_terminal_evidence_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for age_label, age_days in (("recent", 20), ("old", 240)):
        scenarios.append(
            AssetResolutionScenario(
                name=f"top_level_speech_stale_public_not_found_fallback_terminal_{age_label}",
                suite="top_level_speech_terminal_evidence",
                asset_type="speech",
                topology="top_level",
                age_days=age_days,
                source_path_state="stale_missing",
                hint_file_id_state="public_token",
                context_payload_state="public_token",
                public_result_state="not_found",
                public_fallback_result_state="not_found",
                expected_resolver="qq_expired_after_napcat",
                expected_path_kind="missing",
                max_client_calls=2,
                max_fast_calls=1,
                max_remote_attempts=0,
                notes=(
                    "A stale top-level speech asset with a context-issued public token and "
                    "file-not-found on both primary and fallback get_record routes should be terminal."
                ),
            )
        )
        scenarios.append(
            AssetResolutionScenario(
                name=f"top_level_speech_stale_blank_public_payload_terminal_{age_label}",
                suite="top_level_speech_terminal_evidence",
                asset_type="speech",
                topology="top_level",
                age_days=age_days,
                source_path_state="stale_missing",
                hint_file_id_state="public_token",
                context_payload_state="blank_public_payload",
                expected_resolver="qq_expired_after_napcat",
                expected_path_kind="missing",
                max_client_calls=1,
                max_fast_calls=1,
                max_remote_attempts=0,
                notes="Blank get_record payload after successful context hydration should classify terminally.",
            )
        )
    return scenarios


def _exact_friend_speech_current_reduction_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="current_full_dev_top_level_speech_not_found_fallback_background_mid_age",
            suite="exact_friend_speech_current_reduction",
            asset_type="speech",
            topology="top_level",
            age_days=100,
            source_path_state="stale_missing",
            hint_file_id_state="public_token",
            context_payload_state="public_token",
            public_result_state="not_found",
            public_fallback_result_state="not_found",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=2,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes=(
                "Current full-dev reduction of the friend's exact top-level speech shape: "
                "file-not-found on primary and fallback get_record routes now settles as background terminal."
            ),
        )
    ]


def _historical_exact_friend_speech_reference_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="historical_old_main_top_level_speech_not_found_fallback_actionable_mid_age",
            suite="historical_exact_friend_speech_reference",
            asset_type="speech",
            topology="top_level",
            age_days=100,
            source_path_state="stale_missing",
            hint_file_id_state="public_token",
            context_payload_state="public_token",
            public_result_state="not_found",
            public_fallback_result_state="not_found",
            expected_resolver="missing_after_napcat",
            expected_path_kind="missing",
            max_client_calls=2,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes=(
                "Historical old-main reference for the friend's exact top-level speech trace: "
                "the same evidence shape previously surfaced as actionable missing_after_napcat."
            ),
        )
    ]


def exact_friend_speech_current_reduction_scenarios() -> list[AssetResolutionScenario]:
    return list(_exact_friend_speech_current_reduction_scenarios())


def historical_exact_friend_speech_reference_scenarios() -> list[AssetResolutionScenario]:
    return list(_historical_exact_friend_speech_reference_scenarios())


def _top_level_context_payload_surface_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="top_level_video_context_error_no_handle",
            suite="top_level_context_payload_surface",
            asset_type="video",
            topology="top_level",
            context_payload_state="error",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_video_context_local_path_recovers",
            suite="top_level_context_payload_surface",
            asset_type="video",
            topology="top_level",
            context_payload_state="local_path",
            expected_resolver="napcat_context_hydrated",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_video_context_zero_local_no_handle",
            suite="top_level_context_payload_surface",
            asset_type="video",
            topology="top_level",
            context_payload_state="zero_local",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_video_context_zero_public_payload_terminal",
            suite="top_level_context_payload_surface",
            asset_type="video",
            topology="top_level",
            source_path_state="stale_missing",
            context_payload_state="zero_public_payload",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="top_level_video_context_blank_payload_terminal",
            suite="top_level_context_payload_surface",
            asset_type="video",
            topology="top_level",
            source_path_state="stale_missing",
            context_payload_state="blank_payload",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
    ]


def _forward_payload_surface_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for state in ("error", "empty", "zero_local", "stale_local"):
        scenarios.append(
            AssetResolutionScenario(
                name=f"forward_image_payload_{state}_terminal",
                suite="forward_payload_surface",
                asset_type="image",
                topology="forward",
                forward_payload_state=state,
                expected_resolver="qq_expired_after_napcat",
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=0,
            )
        )
    scenarios.append(
        AssetResolutionScenario(
            name="forward_image_payload_local_path_recovers",
            suite="forward_payload_surface",
            asset_type="image",
            topology="forward",
            forward_payload_state="local_path",
            expected_resolver="napcat_forward_hydrated",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        )
    )
    for state in ("blank_public_payload", "zero_public_payload"):
        scenarios.append(
            AssetResolutionScenario(
                name=f"forward_video_payload_{state}_remote",
                suite="forward_payload_surface",
                asset_type="video",
                topology="forward",
                hint_remote_state="live_http",
                forward_payload_state=state,
                expected_resolver="napcat_forward_remote_url",
                expected_path_kind="remote",
                max_client_calls=0,
                max_fast_calls=0,
                max_remote_attempts=1,
            )
        )
    scenarios.append(
        AssetResolutionScenario(
            name="forward_file_payload_file_id_only_remote",
            suite="forward_payload_surface",
            asset_type="file",
            topology="forward",
            forward_payload_state="payload_file_id_only",
            direct_file_result_state="valid_remote",
            expected_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
        )
    )
    return scenarios


def _forward_metadata_surface_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="forward_image_metadata_local_path_recovers",
            suite="forward_metadata_surface",
            asset_type="image",
            topology="forward",
            forward_metadata_state="local_path",
            expected_resolver="napcat_forward_hydrated",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_image_metadata_zero_local_terminal",
            suite="forward_metadata_surface",
            asset_type="image",
            topology="forward",
            forward_metadata_state="zero_local",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_image_metadata_empty_local_unresolved",
            suite="forward_metadata_surface",
            asset_type="image",
            topology="forward",
            forward_metadata_state="empty_local",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_image_metadata_stale_local_terminal",
            suite="forward_metadata_surface",
            asset_type="image",
            topology="forward",
            forward_metadata_state="stale_local",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_image_metadata_remote_url_recovers",
            suite="forward_metadata_surface",
            asset_type="image",
            topology="forward",
            hint_remote_state="live_http",
            forward_metadata_state="remote_url",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_image_metadata_blank_payload_unresolved",
            suite="forward_metadata_surface",
            asset_type="image",
            topology="forward",
            forward_metadata_state="blank_payload",
            expected_resolver=None,
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_video_metadata_public_token_remote",
            suite="forward_metadata_surface",
            asset_type="video",
            topology="forward",
            hint_remote_state="live_http",
            forward_metadata_state="public_token",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_video_metadata_blank_public_payload_remote",
            suite="forward_metadata_surface",
            asset_type="video",
            topology="forward",
            hint_remote_state="live_http",
            forward_metadata_state="blank_public_payload",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_video_metadata_zero_public_payload_remote",
            suite="forward_metadata_surface",
            asset_type="video",
            topology="forward",
            hint_remote_state="live_http",
            forward_metadata_state="zero_public_payload",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
        ),
    ]


def _public_fallback_surface_scenarios() -> list[AssetResolutionScenario]:
    scenarios: list[AssetResolutionScenario] = []
    for fallback_state, expected_resolver, max_remote_attempts in (
        ("none", "qq_expired_after_napcat", 0),
        ("valid_zero_local", "qq_expired_after_napcat", 0),
        ("expired_remote", "qq_expired_after_napcat", 1),
        ("blank_payload", "qq_expired_after_napcat", 0),
        ("timeout", "qq_expired_after_napcat", 0),
        ("opaque_error", "qq_expired_after_napcat", 0),
    ):
        scenarios.append(
            AssetResolutionScenario(
                name=f"public_token_fallback_top_level_video_{fallback_state}",
                suite="public_fallback_surface",
                asset_type="video",
                topology="top_level",
                source_path_state="stale_missing",
                context_payload_state="public_token",
                public_result_state="opaque_error",
                public_fallback_result_state=fallback_state,
                expected_resolver=expected_resolver,
                expected_path_kind="missing",
                max_client_calls=2,
                max_fast_calls=1,
                max_remote_attempts=max_remote_attempts,
            )
        )
    scenarios.extend(
        [
            AssetResolutionScenario(
                name="public_token_fallback_top_level_video_known_bad_video",
                suite="public_fallback_surface",
                asset_type="video",
                topology="top_level",
                source_path_state="stale_missing",
                context_payload_state="public_token",
                public_result_state="opaque_error",
                public_fallback_result_state="known_bad_video",
                expected_resolver="napcat_video_url_unavailable",
                expected_path_kind="missing",
                max_client_calls=2,
                max_fast_calls=1,
                max_remote_attempts=0,
            ),
            AssetResolutionScenario(
                name="public_token_fallback_top_level_file_known_bad_file",
                suite="public_fallback_surface",
                asset_type="file",
                topology="top_level",
                source_path_state="stale_missing",
                context_payload_state="public_token",
                public_result_state="opaque_error",
                public_fallback_result_state="known_bad_file",
                expected_resolver="napcat_file_url_unavailable",
                expected_path_kind="missing",
                max_client_calls=2,
                max_fast_calls=1,
                max_remote_attempts=0,
            ),
            AssetResolutionScenario(
                name="public_token_fallback_top_level_speech_known_bad_record",
                suite="public_fallback_surface",
                asset_type="speech",
                topology="top_level",
                source_path_state="stale_missing",
                context_payload_state="public_token",
                public_result_state="opaque_error",
                public_fallback_result_state="known_bad_record",
                expected_resolver="napcat_record_url_unavailable",
                expected_path_kind="missing",
                max_client_calls=2,
                max_fast_calls=1,
                max_remote_attempts=0,
            ),
        ]
    )
    return scenarios


def _join_schema_end_to_end_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="join_top_level_image_sourcepath_to_materialization",
            suite="join_schema_end_to_end",
            asset_type="image",
            topology="top_level",
            chat_provenance="group",
            segment_path_provenance="sourcePath",
            filesystem_family="ntqq",
            source_path_state="existing",
            expected_resolver="source_local_path",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
            notes="Top-level path provenance should flow through request/algebra to copied materialization semantics.",
        ),
        AssetResolutionScenario(
            name="join_top_level_provider_image_sourcepath_bundle_copy",
            suite="join_schema_end_to_end",
            asset_type="image",
            topology="top_level",
            chat_provenance="group",
            segment_path_provenance="sourcePath",
            filesystem_family="ntqq",
            source_path_state="existing",
            expected_resolver="source_local_path",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=0,
            notes="Provider provenance must participate in the top-level sourcepath->bundle copy join chain, not only exist in schema prose.",
        ),
        AssetResolutionScenario(
            name="join_top_level_file_direct_file_id_remote_to_materialization",
            suite="join_schema_end_to_end",
            asset_type="file",
            topology="top_level",
            chat_provenance="group",
            hint_file_id_state="direct_file_id",
            direct_file_result_state="valid_remote",
            expected_resolver="napcat_segment_file_id_get_file_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=0,
            max_remote_attempts=1,
            notes="Top-level direct-file-id flow should preserve request identity and land in copied materialization semantics.",
        ),
        AssetResolutionScenario(
            name="join_recursive_forward_speech_public_remote_to_materialization",
            suite="join_schema_end_to_end",
            asset_type="speech",
            topology="nested_forward",
            chat_provenance="group",
            forward_recursive_family="forward_chain_parent_partial",
            forward_expansion_state="parent_partial",
            depth_semantics="lower_bound",
            forward_parent_state="missing_message_id_raw",
            hint_file_id_state="public_token",
            speech_original_format="amr",
            speech_requested_out_format="mp3",
            public_result_state="valid_remote_only",
            expected_resolver="napcat_public_token_get_record_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
            notes="Recursive-forward speech should preserve join semantics through converted mp3 materialization outcome projection.",
        ),
        AssetResolutionScenario(
            name="join_recursive_forward_leaf_local_exact_depth_to_materialization",
            suite="join_schema_end_to_end",
            asset_type="image",
            topology="forward",
            chat_provenance="group",
            forward_recursive_family="forward_leaf",
            forward_expansion_state="exact",
            depth_semantics="exact",
            forward_payload_state="local_path",
            expected_resolver="napcat_forward_hydrated",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes="Recursive exact-depth local recovery should land in copied materialization semantics, not just a resolver label.",
        ),
        AssetResolutionScenario(
            name="join_recursive_forward_alias_repeat_timeout_to_materialization",
            suite="join_schema_end_to_end",
            asset_type="video",
            topology="nested_forward",
            chat_provenance="group",
            forward_recursive_family="forward_chain_alias_repeat",
            forward_expansion_state="alias_repeat",
            depth_semantics="lower_bound",
            source_path_state="stale_missing",
            forward_payload_state="public_token",
            public_result_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
            notes="Alias-repeat semantics must preserve background-terminal materialization outcome, not only local resolver classification.",
        ),
        AssetResolutionScenario(
            name="join_recursive_forward_budget_cut_remote_survives_to_materialization",
            suite="join_schema_end_to_end",
            asset_type="image",
            topology="nested_forward",
            chat_provenance="group",
            forward_recursive_family="forward_chain_budget_cut",
            forward_expansion_state="budget_cut",
            depth_semantics="lower_bound",
            hint_remote_state="live_http",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=0,
            max_remote_attempts=1,
            notes="Budget-cut does not imply terminal missing when an independent remote handle still survives into materialization.",
        ),
    ]


def validate_join_schema_scenario(scenario: AssetResolutionScenario) -> list[str]:
    issues: list[str] = []
    if scenario.topology == "top_level" and str(scenario.forward_recursive_family or "none") != "none":
        issues.append("top_level_cannot_have_forward_recursive_family")
    if str(scenario.forward_expansion_state or "none") != "none" and str(scenario.forward_recursive_family or "none") == "none":
        issues.append("forward_expansion_state_requires_recursive_family")
    if str(scenario.depth_semantics or "exact") == "lower_bound" and str(scenario.forward_recursive_family or "none") == "none":
        issues.append("lower_bound_depth_requires_recursive_family")
    if (
        str(scenario.forward_expansion_state or "none")
        in {"alias_repeat", "preview_only", "parent_partial", "budget_cut", "unavailable"}
        and str(scenario.depth_semantics or "exact") != "lower_bound"
    ):
        issues.append("non_exact_expansion_requires_lower_bound_depth")
    if (
        str(scenario.forward_expansion_state or "none") == "alias_repeat"
        and str(scenario.forward_recursive_family or "none") != "forward_chain_alias_repeat"
    ):
        issues.append("alias_repeat_requires_alias_repeat_family")
    if (
        str(scenario.forward_expansion_state or "none") == "budget_cut"
        and str(scenario.forward_recursive_family or "none") != "forward_chain_budget_cut"
    ):
        issues.append("budget_cut_requires_budget_cut_family")
    if str(scenario.segment_path_provenance or "none") != "none" and scenario.asset_type not in {"image", "video", "file", "speech", "sticker"}:
        issues.append("segment_path_provenance_requires_material_asset_family")
    if (
        str(scenario.segment_path_provenance or "none") in {"staticFacePath", "dynamicFacePath"}
        and scenario.asset_type != "sticker"
    ):
        issues.append("sticker_path_provenance_requires_sticker_asset")
    if scenario.asset_type != "speech" and str(scenario.speech_requested_out_format or "default") != "default":
        issues.append("non_speech_cannot_request_record_output_format")
    if scenario.asset_type != "speech" and str(scenario.speech_original_format or "unknown") != "unknown":
        issues.append("non_speech_cannot_claim_speech_original_format")
    return issues


def _forward_recursive_symbolic_scenarios() -> list[AssetResolutionScenario]:
    return [
        AssetResolutionScenario(
            name="forward_leaf_local_recovery_exact_depth",
            suite="forward_recursive_symbolic",
            asset_type="image",
            topology="forward",
            forward_recursive_family="forward_leaf",
            forward_expansion_state="exact",
            depth_semantics="exact",
            forward_payload_state="local_path",
            expected_resolver="napcat_forward_hydrated",
            expected_path_kind="local",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_chain_transition_handle_gain_remote",
            suite="forward_recursive_symbolic",
            asset_type="image",
            topology="nested_forward",
            forward_recursive_family="forward_chain_transition",
            forward_expansion_state="exact",
            depth_semantics="exact",
            hint_remote_state="live_http",
            forward_payload_state="remote_url",
            expected_resolver="napcat_forward_remote_url",
            expected_path_kind="remote",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_chain_transition_preview_only_terminal",
            suite="forward_recursive_symbolic",
            asset_type="image",
            topology="nested_forward",
            forward_recursive_family="forward_chain_transition",
            forward_expansion_state="preview_only",
            depth_semantics="lower_bound",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_chain_parent_partial_handle_survives",
            suite="forward_recursive_symbolic",
            asset_type="speech",
            topology="forward",
            forward_recursive_family="forward_chain_parent_partial",
            forward_expansion_state="parent_partial",
            depth_semantics="lower_bound",
            forward_parent_state="missing_message_id_raw",
            hint_file_id_state="public_token",
            public_result_state="valid_remote_only",
            expected_resolver="napcat_public_token_get_record_remote_url",
            expected_path_kind="remote",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=1,
        ),
        AssetResolutionScenario(
            name="forward_chain_alias_repeat_terminal_lower_bound",
            suite="forward_recursive_symbolic",
            asset_type="video",
            topology="nested_forward",
            forward_recursive_family="forward_chain_alias_repeat",
            forward_expansion_state="alias_repeat",
            depth_semantics="lower_bound",
            source_path_state="stale_missing",
            forward_payload_state="public_token",
            public_result_state="timeout",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=1,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_chain_budget_cut_terminal_lower_bound",
            suite="forward_recursive_symbolic",
            asset_type="file",
            topology="nested_forward",
            forward_recursive_family="forward_chain_budget_cut",
            forward_expansion_state="budget_cut",
            depth_semantics="lower_bound",
            source_path_state="stale_missing",
            forward_payload_state="unavailable",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
        AssetResolutionScenario(
            name="forward_chain_terminal_proof_unavailable_lower_bound",
            suite="forward_recursive_symbolic",
            asset_type="image",
            topology="nested_forward",
            forward_recursive_family="forward_chain_terminal_proof",
            forward_expansion_state="unavailable",
            depth_semantics="lower_bound",
            expected_resolver="qq_expired_after_napcat",
            expected_path_kind="missing",
            max_client_calls=0,
            max_fast_calls=1,
            max_remote_attempts=0,
        ),
    ]


@lru_cache(maxsize=1)
def _generated_asset_resolution_scenarios_cached() -> tuple[AssetResolutionScenario, ...]:
    scenarios: list[AssetResolutionScenario] = []

    forward_media_types = ("image", "video", "file", "speech")
    expensive_forward_types = ("video", "file", "speech")
    forward_topologies = ("forward", "nested_forward")
    malformed_parent_states = (
        "missing_element_id",
        "missing_message_id_raw",
        "missing_peer_uid",
        "blank_parent_bundle",
    )

    for topology in forward_topologies:
        for asset_type in forward_media_types:
            for parent_state in malformed_parent_states:
                scenarios.append(
                    AssetResolutionScenario(
                        name=f"{topology}_{asset_type}_{parent_state}_no_remote",
                        suite="forward_parent_shape",
                        asset_type=asset_type,
                        topology=topology,
                        forward_parent_state=parent_state,
                        age_days=260,
                        source_path_state="stale_missing",
                        expected_resolver=None,
                        expected_path_kind="missing",
                        max_client_calls=0,
                        max_fast_calls=1,
                        max_remote_attempts=0,
                    )
                )
                for remote_state in ("live_http", "relative_http"):
                    scenarios.append(
                        AssetResolutionScenario(
                            name=f"{topology}_{asset_type}_{parent_state}_{remote_state}",
                            suite="forward_parent_shape",
                            asset_type=asset_type,
                            topology=topology,
                            forward_parent_state=parent_state,
                            age_days=260,
                            source_path_state="stale_missing",
                            hint_remote_state=remote_state,
                            expected_resolver="napcat_forward_remote_url",
                            expected_path_kind="remote",
                            max_client_calls=0,
                            max_fast_calls=0,
                            max_remote_attempts=1,
                        )
                    )

    for topology in ("forward", "nested_forward"):
        for remote_state in ("live_http", "relative_http"):
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_sticker_{remote_state}_remote_recovery",
                    suite="live_recovery_paths",
                    asset_type="sticker",
                    topology=topology,
                    age_days=20,
                    hint_remote_state=remote_state,
                    expected_resolver="sticker_remote_download",
                    expected_path_kind="remote",
                    max_client_calls=0,
                    max_fast_calls=1,
                    max_remote_attempts=1,
                )
            )
        scenarios.append(
            AssetResolutionScenario(
                name=f"{topology}_sticker_missing_peer_uid_live_http",
                suite="forward_parent_shape",
                asset_type="sticker",
                topology=topology,
                forward_parent_state="missing_peer_uid",
                age_days=20,
                hint_remote_state="live_http",
                expected_resolver="sticker_remote_download",
                expected_path_kind="remote",
                max_client_calls=0,
                max_fast_calls=1,
                max_remote_attempts=1,
            )
        )

    for topology in forward_topologies:
        for asset_type in forward_media_types:
            for age_label, age_days in (("recent", 20), ("old", 260)):
                for remote_state in ("live_http", "relative_http"):
                    scenarios.append(
                        AssetResolutionScenario(
                            name=f"{topology}_{asset_type}_{age_label}_{remote_state}_remote_recovery",
                            suite="family_diff_matrix",
                            asset_type=asset_type,
                            topology=topology,
                            age_days=age_days,
                            source_path_state="stale_missing",
                            hint_remote_state=remote_state,
                            forward_payload_state="remote_url",
                            expected_resolver="napcat_forward_remote_url",
                            expected_path_kind="remote",
                            max_client_calls=0,
                            max_fast_calls=0,
                            max_remote_attempts=1,
                        )
                    )

    for topology in forward_topologies:
        for asset_type in expensive_forward_types:
            for signal_state in ("unavailable", "timeout"):
                scenarios.append(
                    AssetResolutionScenario(
                        name=f"{topology}_{asset_type}_very_old_{signal_state}",
                        suite="route_health",
                        asset_type=asset_type,
                        topology=topology,
                        age_days=260,
                        source_path_state="stale_missing",
                        forward_payload_state=signal_state,
                        expected_resolver="qq_expired_after_napcat",
                        expected_path_kind="missing",
                        max_client_calls=0,
                        max_fast_calls=1,
                        max_remote_attempts=0,
                    )
                )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_empty_terminal",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    forward_metadata_state="none",
                    forward_materialize_state="empty",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=2,
                    max_remote_attempts=0,
                )
            )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_materialize_error",
                    suite="route_health",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    forward_metadata_state="none",
                    forward_materialize_state="error",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=2,
                    max_remote_attempts=0,
                )
            )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_public_not_found",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    forward_payload_state="public_token",
                    public_result_state="not_found",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=2,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                )
            )
            for remote_state in ("live_http", "relative_http"):
                scenarios.append(
                    AssetResolutionScenario(
                        name=f"{topology}_{asset_type}_recent_unavailable_{remote_state}_remote_wins",
                        suite="route_health",
                        asset_type=asset_type,
                        topology=topology,
                        age_days=20,
                        source_path_state="stale_missing",
                        hint_remote_state=remote_state,
                        forward_payload_state="unavailable",
                        expected_resolver="napcat_forward_remote_url",
                        expected_path_kind="remote",
                        max_client_calls=0,
                        max_fast_calls=0,
                        max_remote_attempts=1,
                    )
                )

    for topology in forward_topologies:
        for asset_type in ("video", "file"):
            for direct_mode, expected_path_kind in (
                ("valid_local", "local"),
                ("valid_remote", "remote"),
            ):
                scenarios.append(
                    AssetResolutionScenario(
                        name=f"{topology}_{asset_type}_very_old_blank_payload_direct_{direct_mode}",
                        suite="live_recovery_paths",
                        asset_type=asset_type,
                        topology=topology,
                        age_days=260,
                        source_path_state="stale_missing",
                        forward_payload_state="blank_payload",
                        direct_file_result_state=direct_mode,
                        expected_resolver="napcat_segment_file_id_get_file"
                        if direct_mode == "valid_local"
                        else "napcat_segment_file_id_get_file_remote_url",
                        expected_path_kind=expected_path_kind,
                        max_client_calls=2,
                        max_fast_calls=1,
                        max_remote_attempts=1 if direct_mode == "valid_remote" else 0,
                    )
                )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_direct_not_found",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    direct_file_result_state="not_found",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=1,
                    max_fast_calls=2,
                    max_remote_attempts=0,
                )
            )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_blank_payload_direct_not_found",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    forward_payload_state="blank_payload",
                    direct_file_result_state="not_found",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=2,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                )
            )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_blank_payload_direct_timeout",
                    suite="route_health",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    forward_payload_state="blank_payload",
                    direct_file_result_state="timeout",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=2,
                    max_fast_calls=1,
                    max_remote_attempts=0,
                )
            )

    for topology in forward_topologies:
        for asset_type in expensive_forward_types:
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_timeout_no_local_hint",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="none",
                    forward_payload_state="timeout",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=2,
                    max_remote_attempts=0,
                )
            )
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_very_old_empty_no_local_hint",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="none",
                    forward_metadata_state="none",
                    forward_materialize_state="empty",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0,
                    max_fast_calls=2,
                    max_remote_attempts=0,
                )
            )

    for asset_type in ("image", "video", "file", "speech"):
        scenarios.append(
            AssetResolutionScenario(
                name=f"top_level_{asset_type}_hint_local_zero_byte_rejected",
                suite="classification_fast_fail",
                asset_type=asset_type,
                topology="top_level",
                age_days=20,
                hint_local_state="file_zero",
                expected_resolver=None,
                expected_path_kind="missing",
                max_client_calls=0,
                max_fast_calls=2 if asset_type == "image" else 1,
                max_remote_attempts=0,
            )
        )
    for topology in forward_topologies:
        for asset_type in ("video", "file", "speech"):
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_materialize_zero_byte_rejected",
                    suite="classification_fast_fail",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=260,
                    source_path_state="stale_missing",
                    forward_metadata_state="none",
                    forward_materialize_state="zero_local",
                    expected_resolver="qq_expired_after_napcat",
                    expected_path_kind="missing",
                    max_client_calls=0 if asset_type == "speech" else 1,
                    max_fast_calls=2,
                    max_remote_attempts=0,
                )
            )

    for topology in ("top_level",):
        for asset_type in ("video", "file", "speech"):
            scenarios.append(
                AssetResolutionScenario(
                    name=f"{topology}_{asset_type}_recent_context_unavailable_direct_remote",
                    suite="route_health",
                    asset_type=asset_type,
                    topology=topology,
                    age_days=20,
                    context_payload_state="unavailable",
                    direct_file_result_state="valid_remote" if asset_type in {"video", "file"} else "none",
                    expected_resolver=(
                        "napcat_segment_file_id_get_file_remote_url"
                        if asset_type in {"video", "file"}
                        else None
                    ),
                    expected_path_kind=("remote" if asset_type in {"video", "file"} else "missing"),
                    max_client_calls=(1 if asset_type in {"video", "file"} else 0),
                    max_fast_calls=1,
                    max_remote_attempts=(1 if asset_type in {"video", "file"} else 0),
                )
            )

    scenarios.extend(_exhaustive_old_forward_terminal_scenarios())
    scenarios.extend(_terminal_evidence_age_invariance_scenarios())
    scenarios.extend(_request_state_payload_state_terminal_equivalence_scenarios())
    scenarios.extend(_prefetch_seeded_image_interaction_scenarios())
    scenarios.extend(_prefetch_seeded_forward_media_interaction_scenarios())
    scenarios.extend(_exhaustive_sticker_forward_parent_scenarios())
    scenarios.extend(_exhaustive_local_path_state_scenarios())
    scenarios.extend(_exhaustive_old_forward_direct_file_id_scenarios())
    scenarios.extend(_exhaustive_public_token_shape_drift_scenarios())
    scenarios.extend(_exhaustive_old_forward_payload_file_id_scenarios())
    scenarios.extend(_exhaustive_old_public_zero_byte_scenarios())
    scenarios.extend(_exhaustive_forward_image_terminal_scenarios())
    scenarios.extend(_partial_parent_handle_sufficient_scenarios())
    scenarios.extend(_top_level_speech_terminal_evidence_scenarios())
    scenarios.extend(_exact_friend_speech_current_reduction_scenarios())
    scenarios.extend(_top_level_context_payload_surface_scenarios())
    scenarios.extend(_forward_payload_surface_scenarios())
    scenarios.extend(_forward_metadata_surface_scenarios())
    scenarios.extend(_public_fallback_surface_scenarios())
    scenarios.extend(_forward_recursive_symbolic_scenarios())
    scenarios.extend(_join_schema_end_to_end_scenarios())

    return tuple(scenarios)


def generated_asset_resolution_scenarios() -> list[AssetResolutionScenario]:
    return list(_generated_asset_resolution_scenarios_cached())


def all_asset_resolution_scenarios() -> list[AssetResolutionScenario]:
    return list(_all_asset_resolution_scenarios_cached())


@lru_cache(maxsize=1)
def _all_asset_resolution_scenarios_cached() -> tuple[AssetResolutionScenario, ...]:
    return (
        *default_asset_resolution_scenarios(),
        *_generated_asset_resolution_scenarios_cached(),
    )


def run_asset_resolution_matrix(*, suite: str | None = None) -> list[AssetResolutionResult]:
    normalized_suite = str(suite or "").strip().lower()
    return list(_run_asset_resolution_matrix_cached(normalized_suite))


@lru_cache(maxsize=None)
def _run_asset_resolution_matrix_cached(
    normalized_suite: str,
) -> tuple[AssetResolutionResult, ...]:
    return tuple(
        run_asset_resolution_scenario(scenario)
        for scenario in _all_asset_resolution_scenarios_cached()
        if not normalized_suite or scenario.suite.lower() == normalized_suite
    )
