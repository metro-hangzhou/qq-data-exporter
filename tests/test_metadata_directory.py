from __future__ import annotations

import shutil
from pathlib import Path

import httpx

from qq_data_integrations.napcat import (
    NapCatHttpClient,
    NapCatMetadataDirectory,
    NapCatTargetLookupError,
)


def test_metadata_directory_search_and_ambiguous_resolution() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_friend_list"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": [
                        {"user_id": 1001, "nickname": "Alice", "remark": "Alex"},
                        {"user_id": 1002, "nickname": "Alicia", "remark": "Alex"},
                        {"user_id": 1003, "nickname": "Bob", "remark": "Builder"},
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    state_dir = Path(".tmp") / "test_metadata_directory"
    shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    directory = NapCatMetadataDirectory(client, state_dir=state_dir)

    results = directory.search("private", "Alex", refresh=True)
    assert [item.chat_id for item in results] == ["1001", "1002"]

    try:
        directory.resolve("private", "Alex", refresh_if_missing=False)
    except NapCatTargetLookupError as exc:
        assert [item.chat_id for item in exc.matches] == ["1001", "1002"]
    else:
        raise AssertionError("expected ambiguous friend lookup")

    reloaded = NapCatMetadataDirectory(client, state_dir=state_dir)
    cached = reloaded.search("private", "Builder")
    assert [item.chat_id for item in cached] == ["1003"]
    client.close()
    shutil.rmtree(state_dir, ignore_errors=True)


def test_metadata_directory_supports_pinyin_and_initials_matching() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/get_friend_list"):
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "data": [
                        {"user_id": 1001, "nickname": "菜鸡"},
                        {"user_id": 1002, "nickname": "测试用户"},
                    ],
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = NapCatHttpClient(
        "http://127.0.0.1:3000",
        transport=httpx.MockTransport(handler),
    )
    state_dir = Path(".tmp") / "test_metadata_directory_pinyin"
    shutil.rmtree(state_dir, ignore_errors=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    directory = NapCatMetadataDirectory(client, state_dir=state_dir)

    assert [item.chat_id for item in directory.search("private", "caiji", refresh=True)] == ["1001"]
    assert [item.chat_id for item in directory.search("private", "cj")] == ["1001"]
    assert directory.resolve("private", "ceshiyonghu", refresh_if_missing=False).chat_id == "1002"

    client.close()
    shutil.rmtree(state_dir, ignore_errors=True)
