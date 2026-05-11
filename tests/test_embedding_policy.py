from __future__ import annotations

from types import SimpleNamespace

from qq_data_process.embeddings import (
    _resolve_quantization_mode,
    _select_embedding_device,
)
from qq_data_process.models import EmbeddingPolicy


def test_default_embedding_policy_uses_qwen3_vl() -> None:
    policy = EmbeddingPolicy()
    assert policy.provider_name == "qwen3_vl"
    assert policy.model_name == "Qwen/Qwen3-VL-Embedding-2B"


def _fake_torch(*, cuda_available: bool, total_vram_gb: float) -> SimpleNamespace:
    cuda = SimpleNamespace(
        is_available=lambda: cuda_available,
        get_device_properties=lambda _index: SimpleNamespace(
            total_memory=int(total_vram_gb * (1024**3))
        ),
    )
    return SimpleNamespace(cuda=cuda)


def test_select_embedding_device_falls_back_to_cpu_on_low_vram_gpu() -> None:
    policy = EmbeddingPolicy(provider_name="jina_v4", min_cuda_vram_gb=10.0)
    torch_module = _fake_torch(cuda_available=True, total_vram_gb=8.0)
    assert _select_embedding_device(policy, torch_module) == "cpu"


def test_select_embedding_device_keeps_explicit_cuda_override() -> None:
    policy = EmbeddingPolicy(provider_name="jina_v4", device="cuda")
    torch_module = _fake_torch(cuda_available=True, total_vram_gb=8.0)
    assert _select_embedding_device(policy, torch_module) == "cuda"


def test_small_vram_cuda_prefers_int8_quantization() -> None:
    policy = EmbeddingPolicy(provider_name="jina_v4", device="cuda")
    torch_module = _fake_torch(cuda_available=True, total_vram_gb=8.0)
    assert _resolve_quantization_mode(policy, torch_module) == "int8"
