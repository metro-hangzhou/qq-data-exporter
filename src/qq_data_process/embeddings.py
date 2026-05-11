from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any

from .models import EmbeddingPolicy, ImageEmbeddingInput, TextEmbeddingInput

DEFAULT_EXTERNAL_PROXY = "http://127.0.0.1:7897"


def _normalize_rows(rows: list[list[float]]) -> list[list[float]]:
    normalized: list[list[float]] = []
    for row in rows:
        norm = sum(value * value for value in row) ** 0.5
        if norm == 0:
            normalized.append(row)
            continue
        normalized.append([value / norm for value in row])
    return normalized


def _coerce_vectors(raw: Any) -> list[list[float]]:
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    vectors: list[list[float]] = []
    for item in raw:
        if hasattr(item, "tolist"):
            item = item.tolist()
        vectors.append([float(value) for value in item])
    return vectors


def _prepare_external_download_env(cache_root: Path) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    hub_cache = cache_root / "hub"
    xet_cache = cache_root / "xet"
    modules_cache = cache_root / "modules"
    hub_cache.mkdir(parents=True, exist_ok=True)
    xet_cache.mkdir(parents=True, exist_ok=True)
    modules_cache.mkdir(parents=True, exist_ok=True)

    proxy_value = os.getenv("QQ_DATA_EXTERNAL_PROXY", DEFAULT_EXTERNAL_PROXY)
    os.environ.setdefault("QQ_DATA_EXTERNAL_PROXY", proxy_value)
    os.environ.setdefault("HTTP_PROXY", proxy_value)
    os.environ.setdefault("HTTPS_PROXY", proxy_value)
    os.environ.setdefault("ALL_PROXY", proxy_value)
    os.environ.setdefault("http_proxy", proxy_value)
    os.environ.setdefault("https_proxy", proxy_value)
    os.environ.setdefault("all_proxy", proxy_value)
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")

    # Keep all HF/Xet caches and logs inside the repository instead of the user profile.
    os.environ["HF_HOME"] = str(cache_root)
    os.environ["HF_HUB_CACHE"] = str(hub_cache)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(hub_cache)
    os.environ["HF_XET_CACHE"] = str(xet_cache)
    os.environ["HF_MODULES_CACHE"] = str(modules_cache)
    os.environ["XDG_CACHE_HOME"] = str(cache_root)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def _prepare_external_proxy_env() -> None:
    proxy_value = os.getenv("QQ_DATA_EXTERNAL_PROXY", DEFAULT_EXTERNAL_PROXY)
    os.environ.setdefault("QQ_DATA_EXTERNAL_PROXY", proxy_value)
    os.environ.setdefault("HTTP_PROXY", proxy_value)
    os.environ.setdefault("HTTPS_PROXY", proxy_value)
    os.environ.setdefault("ALL_PROXY", proxy_value)
    os.environ.setdefault("http_proxy", proxy_value)
    os.environ.setdefault("https_proxy", proxy_value)
    os.environ.setdefault("all_proxy", proxy_value)
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")


def _resolve_torch_dtype(
    policy: EmbeddingPolicy, torch_module: Any, *, resolved_device: str
) -> Any | None:
    dtype_name = (policy.torch_dtype or "auto").lower()
    if dtype_name == "auto":
        if resolved_device.startswith("cuda"):
            return getattr(torch_module, "float16", None)
        return getattr(torch_module, "float32", None)
    if dtype_name == "float16":
        return getattr(torch_module, "float16", None)
    if dtype_name == "float32":
        return getattr(torch_module, "float32", None)
    if dtype_name == "bfloat16":
        return getattr(torch_module, "bfloat16", None)
    return None


def _select_embedding_device(policy: EmbeddingPolicy, torch_module: Any) -> str:
    if policy.device:
        return policy.device
    cuda = getattr(torch_module, "cuda", None)
    if cuda is None or not hasattr(cuda, "is_available") or not cuda.is_available():
        return "cpu"
    if policy.allow_cpu_fallback:
        try:
            total_memory = float(cuda.get_device_properties(0).total_memory)
            total_vram_gb = total_memory / (1024**3)
            if total_vram_gb < policy.min_cuda_vram_gb:
                return "cpu"
        except Exception:
            return "cpu"
    return "cuda"


def _resolve_runtime_batch_size(policy: EmbeddingPolicy, torch_module: Any) -> int:
    base = max(1, int(policy.batch_size))
    cuda = getattr(torch_module, "cuda", None)
    if (
        cuda is None
        or not hasattr(cuda, "is_available")
        or not cuda.is_available()
        or (policy.device and not policy.device.startswith("cuda"))
    ):
        return base
    try:
        total_memory = float(cuda.get_device_properties(0).total_memory)
        total_vram_gb = total_memory / (1024**3)
    except Exception:
        return base
    if total_vram_gb <= 8.5:
        return min(base, 8)
    if total_vram_gb <= 12.0:
        return min(base, 8)
    return base


def _resolve_quantization_mode(policy: EmbeddingPolicy, torch_module: Any) -> str:
    mode = (policy.quantization or "auto").lower()
    if mode in {"none", "int8"}:
        return mode
    cuda = getattr(torch_module, "cuda", None)
    if (
        cuda is None
        or not hasattr(cuda, "is_available")
        or not cuda.is_available()
        or (policy.device and not policy.device.startswith("cuda"))
    ):
        return "none"
    try:
        total_memory = float(cuda.get_device_properties(0).total_memory)
        total_vram_gb = total_memory / (1024**3)
    except Exception:
        return "none"
    if total_vram_gb <= 8.5:
        return "int8"
    return "none"


class DeterministicEmbeddingProvider:
    """Development-safe embedding provider.

    This is intentionally not the production Jina runtime. It exists so the
    preprocessing and retrieval pipeline can be exercised and tested without
    pulling a large model into the default runtime path.
    """

    def __init__(self, vector_size: int = 16) -> None:
        self.vector_size = vector_size
        self._batch_size = 64
        self._preferred_outer_chunk_size = 256

    def embed_documents(self, inputs: list[TextEmbeddingInput]) -> list[list[float]]:
        return [self._vectorize(item.text) for item in inputs]

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return [self._vectorize(item) for item in queries]

    def embed_images(self, inputs: list[ImageEmbeddingInput]) -> list[list[float]]:
        return [self._vectorize(item.text_hint) for item in inputs]

    def _vectorize(self, text: str) -> list[float]:
        if not text:
            text = "<empty>"
        raw = hashlib.sha256(text.encode("utf-8")).digest()
        values = list(raw)
        while len(values) < self.vector_size:
            raw = hashlib.sha256(raw).digest()
            values.extend(raw)
        window = values[: self.vector_size]
        return [round(value / 255.0, 6) for value in window]


class JinaV4EmbeddingProvider:
    """Runtime embedding provider for jinaai/jina-embeddings-v4.

    The actual model load is lazy so unit tests can still exercise the
    retrieval stack with an injected deterministic provider.
    """

    def __init__(self, policy: EmbeddingPolicy) -> None:
        self.policy = policy
        self._model: Any | None = None
        self._image_module: Any | None = None
        self._vector_size = policy.vector_size_hint or 2048
        self._device = policy.device
        self._batch_size = policy.batch_size
        self._quantization = policy.quantization
        self._preferred_outer_chunk_size = 512

    @property
    def vector_size(self) -> int:
        if self._vector_size is None:
            probe = self.embed_queries(["dimension probe"])
            self._vector_size = len(probe[0])
        return self._vector_size

    def embed_documents(self, inputs: list[TextEmbeddingInput]) -> list[list[float]]:
        texts = [item.text or "<empty>" for item in inputs]
        return self._encode_texts(
            texts,
            task=self.policy.document_task,
            prompt_name=self.policy.document_prompt_name,
        )

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        texts = [item or "<empty>" for item in queries]
        return self._encode_texts(
            texts,
            task=self.policy.query_task,
            prompt_name=self.policy.query_prompt_name,
        )

    def embed_images(self, inputs: list[ImageEmbeddingInput]) -> list[list[float]]:
        if not inputs:
            return []

        image_vectors: dict[int, list[float]] = {}
        fallback_indexes: list[int] = []
        images: list[Any] = []
        mapping: list[int] = []
        pil_image = self._import_pil_image()

        for index, item in enumerate(inputs):
            path_value = item.payload.get("path")
            if path_value and Path(path_value).exists():
                try:
                    with pil_image.open(path_value) as handle:  # type: ignore[union-attr]
                        images.append(handle.convert("RGB"))
                    mapping.append(index)
                    continue
                except Exception:
                    pass
            fallback_indexes.append(index)

        if images:
            encoded = self._encode_images(images)
            for index, vector in zip(mapping, encoded):
                image_vectors[index] = vector

        if fallback_indexes:
            fallback_vectors = self._encode_texts(
                [inputs[index].text_hint or "<image>" for index in fallback_indexes],
                task=self.policy.image_fallback_task,
                prompt_name=self.policy.image_fallback_prompt_name,
            )
            for index, vector in zip(fallback_indexes, fallback_vectors):
                image_vectors[index] = vector

        return [image_vectors[index] for index in range(len(inputs))]

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model

        if self.policy.cache_dir is not None:
            _prepare_external_download_env(self.policy.cache_dir)

        try:
            import torch
            from huggingface_hub import snapshot_download
            from transformers import AutoModel
            from transformers import dynamic_module_utils as dynamic_module_utils
            from transformers.utils import hub as transformers_hub
        except ImportError as exc:
            raise RuntimeError(
                "Jina v4 embeddings require torch, transformers, and huggingface_hub in the current environment."
            ) from exc

        resolved_device = _select_embedding_device(self.policy, torch)
        self._device = resolved_device
        self._batch_size = _resolve_runtime_batch_size(self.policy, torch)
        self._quantization = _resolve_quantization_mode(self.policy, torch)
        self._preferred_outer_chunk_size = max(
            64,
            min(512, self._batch_size * 8),
        )

        load_kwargs: dict[str, Any] = {
            "trust_remote_code": True,
            "low_cpu_mem_usage": True,
        }
        resolved_dtype = _resolve_torch_dtype(
            self.policy, torch, resolved_device=resolved_device
        )
        if resolved_dtype is not None:
            load_kwargs["torch_dtype"] = resolved_dtype
        if self._quantization == "int8":
            bits_and_bytes_config = None
            try:
                from transformers import BitsAndBytesConfig

                bits_and_bytes_config = BitsAndBytesConfig(
                    load_in_8bit=True,
                    llm_int8_enable_fp32_cpu_offload=False,
                )
            except Exception as exc:
                raise RuntimeError(
                    "8-bit GPU quantization requires bitsandbytes support in the current environment."
                ) from exc
            load_kwargs["quantization_config"] = bits_and_bytes_config
            load_kwargs["device_map"] = {"": 0}
        model_source: str = self.policy.model_name
        if self.policy.cache_dir is not None:
            modules_cache = self.policy.cache_dir / "modules"
            transformers_hub.HF_MODULES_CACHE = str(modules_cache)
            dynamic_module_utils.HF_MODULES_CACHE = str(modules_cache)
            local_dir = self.policy.cache_dir / self.policy.model_name.replace("/", "--")
            local_dir.mkdir(parents=True, exist_ok=True)
            if (local_dir / "config.json").exists():
                model_source = str(local_dir)
                load_kwargs["local_files_only"] = True
            else:
                try:
                    snapshot_download(
                        repo_id=self.policy.model_name,
                        local_dir=str(local_dir),
                    )
                    model_source = str(local_dir)
                    load_kwargs["local_files_only"] = True
                except Exception:
                    load_kwargs["cache_dir"] = str(self.policy.cache_dir)

        model = AutoModel.from_pretrained(model_source, **load_kwargs)
        if hasattr(model, "eval"):
            model.eval()

        if self._quantization != "int8" and hasattr(model, "to"):
            try:
                model = model.to(resolved_device)
            except Exception:
                # Some remote-code models manage device placement internally.
                pass

        self._model = model
        return model

    def _encode_texts(
        self, texts: list[str], *, task: str, prompt_name: str | None = None
    ) -> list[list[float]]:
        model = self._ensure_model()
        if hasattr(model, "encode_text"):
            raw = model.encode_text(
                texts,
                task=task,
                prompt_name=prompt_name,
                batch_size=self._batch_size,
                return_numpy=True,
            )
        elif hasattr(model, "encode"):
            raw = model.encode(
                texts,
                task=task,
                prompt_name=prompt_name,
                batch_size=self._batch_size,
                return_numpy=True,
            )
        else:
            raise RuntimeError(
                "Loaded Jina embedding model does not expose encode_text(...) or encode(...)."
            )
        vectors = _coerce_vectors(raw)
        if self.policy.normalize:
            vectors = _normalize_rows(vectors)
        if vectors and self._vector_size is None:
            self._vector_size = len(vectors[0])
        return vectors

    def _encode_images(self, images: list[Any]) -> list[list[float]]:
        model = self._ensure_model()
        if not hasattr(model, "encode_image"):
            raise RuntimeError(
                "Loaded Jina embedding model does not expose encode_image(...)."
            )
        try:
            raw = model.encode_image(images)
        except TypeError:
            raw = model.encode_image(images, task=self.policy.image_fallback_task)
        vectors = _coerce_vectors(raw)
        if self.policy.normalize:
            vectors = _normalize_rows(vectors)
        if vectors and self._vector_size is None:
            self._vector_size = len(vectors[0])
        return vectors

    def _import_pil_image(self) -> Any:
        if self._image_module is None:
            try:
                from PIL import Image
            except ImportError as exc:
                raise RuntimeError(
                    "Jina v4 image embedding requires pillow in the current environment."
                ) from exc
            self._image_module = Image
        return self._image_module


class OpenRouterEmbeddingProvider:
    def __init__(self, policy: EmbeddingPolicy) -> None:
        self.policy = policy
        self._client: Any | None = None
        self._vector_size = policy.vector_size_hint
        self._batch_size = max(1, int(policy.batch_size))
        self._device = "openrouter_api"
        self._quantization = "remote"
        self._preferred_outer_chunk_size = self._batch_size

    @property
    def vector_size(self) -> int:
        if self._vector_size is None:
            probe = self.embed_queries(["dimension probe"])
            self._vector_size = len(probe[0])
        return self._vector_size

    def embed_documents(self, inputs: list[TextEmbeddingInput]) -> list[list[float]]:
        return self._embed_payloads([item.text or "<empty>" for item in inputs])

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        return self._embed_payloads([item or "<empty>" for item in queries])

    def embed_images(self, inputs: list[ImageEmbeddingInput]) -> list[list[float]]:
        payloads: list[Any] = []
        for item in inputs:
            path_value = item.payload.get("path")
            if path_value and Path(path_value).exists():
                try:
                    payloads.append(self._image_payload(Path(path_value), item.text_hint))
                    continue
                except Exception:
                    pass
            payloads.append(item.text_hint or "<image>")
        return self._embed_payloads(payloads)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        _prepare_external_proxy_env()
        api_key = os.getenv(self.policy.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"OpenRouter embeddings require env var {self.policy.api_key_env}."
            )
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError(
                "OpenRouter embeddings require httpx in the current environment."
            ) from exc
        self._client = httpx.Client(
            base_url=self.policy.api_base_url,
            timeout=self.policy.request_timeout_seconds,
            trust_env=True,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        return self._client

    def _embed_payloads(self, payloads: list[Any]) -> list[list[float]]:
        if not payloads:
            return []
        client = self._client_or_create()
        vectors: list[list[float]] = []
        for batch in _chunk_list(payloads, self._batch_size):
            batch_vectors = self._embed_batch_with_fallback(client, batch)
            if self.policy.normalize:
                batch_vectors = _normalize_rows(batch_vectors)
            vectors.extend(batch_vectors)
        if vectors and self._vector_size is None:
            self._vector_size = len(vectors[0])
        return vectors

    def _embed_batch_with_fallback(
        self, client: Any, batch: list[Any]
    ) -> list[list[float]]:
        try:
            response = client.post(
                "/embeddings",
                json={
                    "model": self.policy.model_name,
                    "input": batch,
                    "encoding_format": "float",
                },
            )
            response.raise_for_status()
            data = response.json().get("data", [])
            ordered = sorted(data, key=lambda item: int(item.get("index", 0)))
            return [
                [float(value) for value in item.get("embedding", [])]
                for item in ordered
            ]
        except Exception as exc:
            if len(batch) <= 1 or not self._is_retryable_embedding_error(exc):
                raise
            midpoint = len(batch) // 2
            left = self._embed_batch_with_fallback(client, batch[:midpoint])
            right = self._embed_batch_with_fallback(client, batch[midpoint:])
            return left + right

    def _is_retryable_embedding_error(self, exc: Exception) -> bool:
        httpx_module = None
        try:
            import httpx as httpx_module  # type: ignore[no-redef]
        except Exception:
            httpx_module = None
        if httpx_module is not None:
            if isinstance(exc, (httpx_module.ReadTimeout, httpx_module.WriteTimeout)):
                return True
            if isinstance(exc, httpx_module.HTTPStatusError):
                status = exc.response.status_code
                if status in {408, 413, 429, 500, 502, 503, 504}:
                    return True
        if isinstance(exc, json.JSONDecodeError):
            return True
        return False

    def _image_payload(self, image_path: Path, text_hint: str) -> dict[str, Any]:
        mime_type, _ = mimetypes.guess_type(str(image_path))
        if not mime_type:
            mime_type = "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return {
            "content": [
                {"type": "text", "text": text_hint or "<image>"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
                },
            ]
        }


class Qwen3VLEmbeddingProvider:
    def __init__(self, policy: EmbeddingPolicy) -> None:
        self.policy = policy
        self._embedder: Any | None = None
        self._vector_size = policy.vector_size_hint or 2048
        self._device = policy.device
        self._batch_size = policy.batch_size
        self._quantization = policy.quantization
        self._preferred_outer_chunk_size = 512

    @property
    def vector_size(self) -> int:
        return self._vector_size or 2048

    def embed_documents(self, inputs: list[TextEmbeddingInput]) -> list[list[float]]:
        payloads = [{"text": item.text or "<empty>"} for item in inputs]
        return self._encode_inputs(payloads)

    def embed_queries(self, queries: list[str]) -> list[list[float]]:
        payloads = [{"text": item or "<empty>"} for item in queries]
        return self._encode_inputs(payloads)

    def embed_images(self, inputs: list[ImageEmbeddingInput]) -> list[list[float]]:
        payloads: list[dict[str, Any]] = []
        for item in inputs:
            path_value = item.payload.get("path")
            payload: dict[str, Any] = {"text": item.text_hint or "<image>"}
            if path_value and Path(path_value).exists():
                payload["image"] = str(Path(path_value).resolve())
            payloads.append(payload)
        return self._encode_inputs(payloads)

    def _ensure_model(self) -> Any:
        if self._embedder is not None:
            return self._embedder

        if self.policy.cache_dir is not None:
            _prepare_external_download_env(self.policy.cache_dir)

        try:
            import importlib.util
            import sys
            import torch
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise RuntimeError(
                "Qwen3-VL embeddings require torch, transformers, huggingface_hub, and qwen-vl-utils."
            ) from exc

        resolved_device = _select_embedding_device(self.policy, torch)
        self._device = resolved_device
        self._batch_size = _resolve_runtime_batch_size(self.policy, torch)
        self._quantization = _resolve_quantization_mode(self.policy, torch)
        self._preferred_outer_chunk_size = max(
            128,
            min(1024, self._batch_size * 16),
        )

        load_kwargs: dict[str, Any] = {
        }
        if self.policy.attn_implementation:
            load_kwargs["attn_implementation"] = self.policy.attn_implementation
        resolved_dtype = _resolve_torch_dtype(
            self.policy, torch, resolved_device=resolved_device
        )
        if resolved_dtype is not None:
            load_kwargs["torch_dtype"] = resolved_dtype

        model_source: str = self.policy.model_name
        if self.policy.cache_dir is not None:
            local_dir = self.policy.cache_dir / self.policy.model_name.replace("/", "--")
            local_dir.mkdir(parents=True, exist_ok=True)
            if (local_dir / "config.json").exists() and (local_dir / "scripts" / "qwen3_vl_embedding.py").exists():
                model_source = str(local_dir)
            else:
                try:
                    snapshot_download(
                        repo_id=self.policy.model_name,
                        local_dir=str(local_dir),
                    )
                    model_source = str(local_dir)
                except Exception:
                    load_kwargs["cache_dir"] = str(self.policy.cache_dir)
        script_path = Path(model_source) / "scripts" / "qwen3_vl_embedding.py"
        if not script_path.exists():
            raise RuntimeError(
                f"Qwen3-VL embedding helper script was not found at {script_path}."
            )
        module_name = "qq_data_process._qwen3_vl_embedding_helper"
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Failed to load Qwen3-VL embedding helper module.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        embedder_cls = getattr(module, "Qwen3VLEmbedder")
        embedder = embedder_cls(
            model_name_or_path=model_source,
            **load_kwargs,
        )
        self._embedder = embedder
        model = getattr(embedder, "model", None)
        if model is not None:
            device = getattr(model, "device", None)
            if device is not None:
                self._device = str(device)
        return embedder

    def _encode_inputs(self, payloads: list[dict[str, Any]]) -> list[list[float]]:
        if not payloads:
            return []
        embedder = self._ensure_model()
        vectors: list[list[float]] = []
        for batch in _chunk_list(payloads, self._batch_size):
            embeddings = embedder.process(batch, normalize=self.policy.normalize)
            embeddings = embeddings.detach().float().cpu().tolist()
            vectors.extend([[float(value) for value in row] for row in embeddings])
        if vectors and self._vector_size is None:
            self._vector_size = len(vectors[0])
        return vectors


def _chunk_list(items: list[Any], size: int) -> list[list[Any]]:
    step = max(1, size)
    return [items[index : index + step] for index in range(0, len(items), step)]


def build_embedding_provider(policy: EmbeddingPolicy) -> object:
    if policy.provider_name == "deterministic":
        return DeterministicEmbeddingProvider(vector_size=policy.vector_size_hint or 16)
    if policy.provider_name == "jina_v4":
        return JinaV4EmbeddingProvider(policy)
    if policy.provider_name == "openrouter":
        return OpenRouterEmbeddingProvider(policy)
    if policy.provider_name == "qwen3_vl":
        return Qwen3VLEmbeddingProvider(policy)
    raise ValueError(f"Unsupported embedding provider: {policy.provider_name}")
