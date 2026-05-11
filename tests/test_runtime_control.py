from __future__ import annotations

import os

from qq_data_process.runtime_control import (
    apply_cpu_thread_limit,
    resolve_cpu_thread_limit,
)


def test_resolve_cpu_thread_limit_supports_reserve_cores() -> None:
    assert resolve_cpu_thread_limit(cpu_count=16, reserve_cores=4) == 12
    assert resolve_cpu_thread_limit(cpu_count=4, reserve_cores=10) == 1


def test_resolve_cpu_thread_limit_supports_explicit_max_threads() -> None:
    assert resolve_cpu_thread_limit(cpu_count=16, max_threads=6) == 6
    assert resolve_cpu_thread_limit(cpu_count=4, max_threads=10) == 4


def test_apply_cpu_thread_limit_sets_cap_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("OMP_NUM_THREADS", "32")
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "7")

    result = apply_cpu_thread_limit(max_threads=6)

    assert result["applied"] is True
    assert result["thread_limit"] == 6
    assert result["policy"] == "max_threads=6"
    assert result["cpu_count"] >= 1
    assert result["torch_applied"] in {True, False}
    assert result["torch_interop_threads"] in {None, 1, 2}
    assert os.getenv("OMP_NUM_THREADS") == "6"
    assert os.getenv("OPENBLAS_NUM_THREADS") == "6"
    assert os.getenv("TOKENIZERS_PARALLELISM") == "false"
