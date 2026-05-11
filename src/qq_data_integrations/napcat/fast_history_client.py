from __future__ import annotations

from collections.abc import Callable
from threading import RLock
from time import monotonic
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import httpx

from .models import normalize_chat_type

FAST_HISTORY_PLUGIN_ID = "napcat-plugin-qq-data-fast"
FAST_HISTORY_MAX_PAGE_SIZE = 200
FAST_HISTORY_BULK_SAFE_DATA_COUNT = 2000


class NapCatFastHistoryError(RuntimeError):
    pass


class NapCatFastHistoryUnavailable(NapCatFastHistoryError):
    pass


class NapCatFastHistoryConnectError(NapCatFastHistoryUnavailable):
    pass


class NapCatFastHistoryTimeoutError(NapCatFastHistoryError):
    pass


class NapCatFastHistoryResponseError(NapCatFastHistoryError):
    pass


class NapCatFastHistoryClient:
    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        headers_provider: Callable[[], dict[str, str] | None] | None = None,
        use_system_proxy: bool = False,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers or {})
        self._headers_provider = headers_provider
        self._headers_lock = RLock()
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            trust_env=use_system_proxy,
            headers=self._headers,
        )

    def close(self) -> None:
        self._client.close()

    def health(self) -> dict[str, Any]:
        response = self._get("/health")
        data = self._extract_data(response, require_code=False)
        return data if isinstance(data, dict) else {}

    def capabilities(self) -> dict[str, Any]:
        response = self._get("/capabilities")
        data = self._extract_data(response, require_code=False)
        return data if isinstance(data, dict) else {}

    def probe_route(
        self,
        path: str,
        *,
        method: Literal["GET", "POST"] = "GET",
        json: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        try:
            if method == "POST":
                response = self._client.post(path, json=json if json is not None else {}, timeout=timeout)
            else:
                response = self._client.get(path, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return {
                "path": path,
                "method": method,
                "reachable": False,
                "status_code": None,
                "detail": str(exc),
                "timed_out": False,
                "connect_error": True,
            }
        except httpx.TimeoutException as exc:
            return {
                "path": path,
                "method": method,
                "reachable": False,
                "status_code": None,
                "detail": str(exc),
                "timed_out": True,
                "connect_error": False,
            }
        elapsed_ms = None
        if started is not None:
            elapsed_ms = int(round((monotonic() - started) * 1000))
        status_code = int(response.status_code)
        reachable = response.is_success
        return {
            "path": path,
            "method": method,
            "reachable": reachable,
            "status_code": status_code,
            "detail": None if reachable else f"http_{status_code}",
            "timed_out": False,
            "connect_error": False,
            "elapsed_ms": elapsed_ms,
        }

    def get_history(
        self,
        chat_type: str,
        chat_id: str,
        *,
        message_id: str | None = None,
        count: int = 20,
        reverse_order: bool = False,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_type": normalize_chat_type(chat_type),
            "chat_id": str(chat_id),
            "count": count,
        }
        if message_id not in {None, "", "0", 0}:
            payload["message_id"] = str(message_id)
            payload["reverse_order"] = bool(reverse_order)
        response = self._post("/history", json=payload, timeout=timeout)
        return self._extract_data(response)

    def get_history_tail_bulk(
        self,
        chat_type: str,
        chat_id: str,
        *,
        data_count: int,
        page_size: int = FAST_HISTORY_MAX_PAGE_SIZE,
        anchor_message_id: str | None = None,
        anchor_message_seq: str | None = None,
        history_fetch_strategy: str | None = None,
        include_debug_stats: bool = False,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_type": normalize_chat_type(chat_type),
            "chat_id": str(chat_id),
            "data_count": int(data_count),
            "page_size": int(page_size),
        }
        if anchor_message_id not in {None, "", "0", 0}:
            payload["anchor_message_id"] = str(anchor_message_id)
        if anchor_message_seq not in {None, "", "0", 0}:
            payload["anchor_message_seq"] = str(anchor_message_seq)
        if history_fetch_strategy:
            payload["history_fetch_strategy"] = str(history_fetch_strategy)
        if include_debug_stats:
            payload["include_debug_stats"] = True
        response = self._post("/history-tail-bulk", json=payload, timeout=timeout)
        return self._extract_data(response)

    def get_history_full_bulk(
        self,
        chat_type: str,
        chat_id: str,
        *,
        data_count: int,
        page_size: int = FAST_HISTORY_MAX_PAGE_SIZE,
        history_fetch_strategy: str | None = None,
        include_debug_stats: bool = False,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_type": normalize_chat_type(chat_type),
            "chat_id": str(chat_id),
            "data_count": int(data_count),
            "page_size": int(page_size),
        }
        if history_fetch_strategy:
            payload["history_fetch_strategy"] = str(history_fetch_strategy)
        if include_debug_stats:
            payload["include_debug_stats"] = True
        response = self._post("/history-full-bulk", json=payload, timeout=timeout)
        return self._extract_data(response)

    def hydrate_media(
        self,
        *,
        message_id_raw: str,
        element_id: str,
        peer_uid: str,
        chat_type_raw: int | str,
        asset_type: str | None = None,
        asset_role: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        payload = {
            "message_id_raw": str(message_id_raw),
            "element_id": str(element_id),
            "peer_uid": str(peer_uid),
            "chat_type_raw": int(chat_type_raw),
        }
        if asset_type:
            payload["asset_type"] = str(asset_type)
        if asset_role:
            payload["asset_role"] = str(asset_role)
        response = self._post("/hydrate-media", json=payload, timeout=timeout)
        return self._extract_data(response)

    def hydrate_media_batch(
        self,
        items: list[dict[str, Any]],
        *,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        response = self._post("/hydrate-media-batch", json={"items": items}, timeout=timeout)
        return self._extract_data(response)

    def hydrate_forward_media(
        self,
        *,
        message_id_raw: str,
        element_id: str,
        peer_uid: str,
        chat_type_raw: int | str,
        asset_type: str | None = None,
        asset_role: str | None = None,
        file_name: str | None = None,
        md5: str | None = None,
        file_id: str | None = None,
        url: str | None = None,
        materialize: bool = False,
        download_timeout_ms: int | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        payload = {
            "message_id_raw": str(message_id_raw),
            "element_id": str(element_id),
            "peer_uid": str(peer_uid),
            "chat_type_raw": int(chat_type_raw),
        }
        if asset_type:
            payload["asset_type"] = str(asset_type)
        if asset_role:
            payload["asset_role"] = str(asset_role)
        if file_name:
            payload["file_name"] = str(file_name)
        if md5:
            payload["md5"] = str(md5)
        if file_id:
            payload["file_id"] = str(file_id)
        if url:
            payload["url"] = str(url)
        if materialize:
            payload["materialize"] = True
        if download_timeout_ms is not None:
            payload["download_timeout_ms"] = int(download_timeout_ms)
        response = self._post("/hydrate-forward-media", json=payload, timeout=timeout)
        return self._extract_data(response)

    def hydrate_forward_detail(
        self,
        *,
        message_id_raw: str,
        element_id: str,
        peer_uid: str,
        chat_type_raw: int | str,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        payload = {
            "message_id_raw": str(message_id_raw),
            "element_id": str(element_id),
            "peer_uid": str(peer_uid),
            "chat_type_raw": int(chat_type_raw),
        }
        response = self._post("/hydrate-forward-detail", json=payload, timeout=timeout)
        return self._extract_data(response)

    def hydrate_forward_detail_batch(
        self,
        items: list[dict[str, Any]],
        *,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        response = self._post(
            "/hydrate-forward-detail-batch",
            json={"items": items},
            timeout=timeout,
        )
        return self._extract_data(response)

    def _extract_data(self, response: httpx.Response, *, require_code: bool = True) -> Any:
        if response.status_code in {404, 503}:
            raise NapCatFastHistoryUnavailable(
                f"NapCat fast history plugin is unavailable at {self._base_url} (status={response.status_code})."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise NapCatFastHistoryResponseError(
                f"NapCat fast history plugin returned HTTP {response.status_code}."
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise NapCatFastHistoryResponseError(
                "NapCat fast history plugin returned non-JSON response."
            ) from exc
        if not isinstance(payload, dict):
            raise NapCatFastHistoryResponseError(
                "NapCat fast history plugin returned unexpected JSON payload."
            )
        code = payload.get("code")
        if require_code and "code" not in payload:
            raise NapCatFastHistoryResponseError(
                "NapCat fast history plugin returned JSON without a response code."
            )
        if code not in {None, 0}:
            raise NapCatFastHistoryError(
                str(payload.get("message") or payload.get("msg") or "NapCat fast history request failed")
            )
        return payload.get("data", payload)

    def _post(
        self,
        path: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        **kwargs,
    ) -> httpx.Response:
        self._ensure_headers()
        try:
            response = self._client.post(path, timeout=timeout, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NapCatFastHistoryConnectError(
                "Cannot connect to NapCat fast history plugin at "
                f"{self._base_url}. Ensure the plugin is installed and NapCat WebUI is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise NapCatFastHistoryTimeoutError(
                f"NapCat fast history plugin timed out waiting for {path} at {self._base_url}"
            ) from exc
        if response.status_code in {401, 403} and self._ensure_headers(force_refresh=True):
            response = self._client.post(path, timeout=timeout, **kwargs)
        return response

    def _get(
        self,
        path: str,
        *,
        timeout: float | httpx.Timeout | None = None,
        **kwargs,
    ) -> httpx.Response:
        self._ensure_headers()
        try:
            response = self._client.get(path, timeout=timeout, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NapCatFastHistoryConnectError(
                "Cannot connect to NapCat fast history plugin at "
                f"{self._base_url}. Ensure the plugin is installed and NapCat WebUI is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise NapCatFastHistoryTimeoutError(
                f"NapCat fast history plugin timed out waiting for {path} at {self._base_url}"
            ) from exc
        if response.status_code in {401, 403} and self._ensure_headers(force_refresh=True):
            response = self._client.get(path, timeout=timeout, **kwargs)
        return response

    def _ensure_headers(self, *, force_refresh: bool = False) -> bool:
        if self._headers_provider is None:
            return False
        with self._headers_lock:
            if not force_refresh and self._headers.get("Authorization"):
                return False
            provided = self._headers_provider() or {}
            authorization = str(provided.get("Authorization") or "").strip()
            if not authorization:
                return False
            self._headers["Authorization"] = authorization
            self._client.headers["Authorization"] = authorization
            return True


def derive_fast_history_url(
    webui_url: str,
    *,
    plugin_id: str = FAST_HISTORY_PLUGIN_ID,
) -> str:
    parsed = urlparse(webui_url.rstrip("/"))
    path = parsed.path or ""
    if path.endswith("/api"):
        path = path[:-4]
    path = f"{path}/plugin/{plugin_id}/api"
    path = "/" + "/".join(segment for segment in path.split("/") if segment)
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))
