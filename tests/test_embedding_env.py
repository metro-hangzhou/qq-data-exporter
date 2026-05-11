from __future__ import annotations

import os
from pathlib import Path

from qq_data_process.embeddings import _prepare_external_download_env


def test_prepare_external_download_env_uses_repo_local_cache_and_proxy(monkeypatch) -> None:
    cache_root = Path(".tmp") / "test_embedding_env" / "hf"

    for key in (
        "QQ_DATA_EXTERNAL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "HF_XET_CACHE",
        "HF_MODULES_CACHE",
        "XDG_CACHE_HOME",
        "HF_HUB_DISABLE_XET",
    ):
        monkeypatch.delenv(key, raising=False)

    _prepare_external_download_env(cache_root)

    assert cache_root.exists()
    assert os.environ["QQ_DATA_EXTERNAL_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert os.environ["HF_HOME"] == str(cache_root)
    assert os.environ["HF_HUB_CACHE"] == str(cache_root / "hub")
    assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(cache_root / "hub")
    assert os.environ["HF_XET_CACHE"] == str(cache_root / "xet")
    assert os.environ["HF_MODULES_CACHE"] == str(cache_root / "modules")
    assert os.environ["XDG_CACHE_HOME"] == str(cache_root)
    assert os.environ["HF_HUB_DISABLE_XET"] == "1"
