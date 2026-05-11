from __future__ import annotations

import os
import time
from typing import Any


THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMEXPR_MAX_THREADS",
    "POLARS_MAX_THREADS",
    "RAYON_NUM_THREADS",
)


def resolve_cpu_thread_limit(
    *,
    cpu_count: int | None = None,
    max_threads: int | None = None,
    reserve_cores: int | None = None,
) -> int | None:
    total = max(1, int(cpu_count or os.cpu_count() or 1))
    if max_threads is not None:
        return max(1, min(total, int(max_threads)))
    if reserve_cores is not None:
        reserve = max(0, int(reserve_cores))
        return max(1, total - reserve)
    return None


def apply_cpu_thread_limit(
    *,
    max_threads: int | None = None,
    reserve_cores: int | None = None,
    interop_threads: int | None = None,
    yield_ms: int | None = None,
    yield_every: int | None = None,
) -> dict[str, Any]:
    total = max(1, int(os.cpu_count() or 1))
    thread_limit = resolve_cpu_thread_limit(
        cpu_count=total,
        max_threads=max_threads,
        reserve_cores=reserve_cores,
    )
    if thread_limit is None:
        return {
            "applied": False,
            "cpu_count": total,
            "thread_limit": total,
            "policy": "none",
        }

    for name in THREAD_ENV_VARS:
        _cap_env_var(name, thread_limit)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if yield_ms is not None:
        os.environ["QQ_DATA_CPU_YIELD_MS"] = str(max(0, int(yield_ms)))
    if yield_every is not None:
        os.environ["QQ_DATA_CPU_YIELD_EVERY"] = str(max(1, int(yield_every)))

    torch_applied = False
    torch_interop_threads = None
    try:
        import torch

        if hasattr(torch, "set_num_threads"):
            torch.set_num_threads(thread_limit)
            torch_applied = True
        if hasattr(torch, "set_num_interop_threads"):
            target_interop = max(1, min(thread_limit, int(interop_threads or 2)))
            torch.set_num_interop_threads(target_interop)
            torch_interop_threads = target_interop
    except Exception:
        pass

    policy = (
        f"max_threads={thread_limit}"
        if max_threads is not None
        else f"reserve_cores={int(reserve_cores or 0)}"
    )
    return {
        "applied": True,
        "cpu_count": total,
        "thread_limit": thread_limit,
        "policy": policy,
        "torch_applied": torch_applied,
        "torch_interop_threads": torch_interop_threads,
        "yield_ms": int(os.getenv("QQ_DATA_CPU_YIELD_MS", "0") or 0),
        "yield_every": int(os.getenv("QQ_DATA_CPU_YIELD_EVERY", "256") or 256),
    }


def apply_process_priority(mode: str | None) -> bool:
    normalized = str(mode or "").strip().lower()
    if normalized in {"", "default", "normal"}:
        return False
    if os.name != "nt":
        return False
    priority_classes = {
        "below_normal": 0x00004000,
        "idle": 0x00000040,
        "high": 0x00000080,
    }
    priority_class = priority_classes.get(normalized)
    if priority_class is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        handle = kernel32.GetCurrentProcess()
        return bool(kernel32.SetPriorityClass(handle, priority_class))
    except Exception:
        return False


def maybe_cooperative_yield(counter: int) -> None:
    every = _safe_int(os.getenv("QQ_DATA_CPU_YIELD_EVERY"), default=256, minimum=1)
    sleep_ms = _safe_int(os.getenv("QQ_DATA_CPU_YIELD_MS"), default=0, minimum=0)
    if sleep_ms <= 0 or counter <= 0 or counter % every != 0:
        return
    time.sleep(sleep_ms / 1000.0)


def _cap_env_var(name: str, limit: int) -> None:
    existing = os.getenv(name)
    if existing is None:
        os.environ[name] = str(limit)
        return
    try:
        os.environ[name] = str(min(limit, int(existing)))
    except ValueError:
        os.environ[name] = str(limit)


def _safe_int(value: str | None, *, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(value or default))
    except (TypeError, ValueError):
        return max(minimum, default)
