from __future__ import annotations

from typing import Any

import httpx


class NapCatApiError(RuntimeError):
    pass


class NapCatApiConnectError(NapCatApiError):
    pass


class NapCatApiTimeoutError(NapCatApiError):
    pass


class NapCatApiResponseError(NapCatApiError):
    pass


class NapCatHttpClient:
    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        use_system_proxy: bool = False,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=use_system_proxy,
        )

    def close(self) -> None:
        self._client.close()

    def call_action(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        try:
            response = self._client.post(f"/{action}", json=params or {}, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NapCatApiConnectError(
                "Cannot connect to NapCat OneBot HTTP server at "
                f"{self._base_url}. No service is listening there. "
                "Enable an HTTP server in NapCat WebUI, or set NAPCAT_HTTP_URL / NAPCAT_WORKDIR "
                "to the actual runtime."
            ) from exc
        except httpx.TimeoutException as exc:
            raise NapCatApiTimeoutError(
                f"NapCat action timed out: {action}"
            ) from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise NapCatApiResponseError(
                f"NapCat action returned HTTP {response.status_code}: {action}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise NapCatApiResponseError(
                f"NapCat action returned non-JSON response: {action}"
            ) from exc
        if not isinstance(payload, dict):
            raise NapCatApiResponseError(
                f"NapCat action returned unexpected JSON payload: {action}"
            )
        if "status" not in payload:
            raise NapCatApiResponseError(
                f"NapCat action returned JSON without status: {action}"
            )
        if payload.get("retcode") not in {None, 0}:
            raise NapCatApiError(
                payload.get("message")
                or payload.get("msg")
                or f"NapCat action returned retcode={payload.get('retcode')}: {action}"
            )
        if payload.get("status") != "ok":
            raise NapCatApiError(
                payload.get("message")
                or payload.get("msg")
                or f"NapCat action failed: {action}"
            )
        return payload.get("data", payload)

    def get_group_list(self, *, no_cache: bool = False) -> Any:
        return self.call_action("get_group_list", {"no_cache": no_cache})

    def get_friend_list(self, *, no_cache: bool = False) -> Any:
        return self.call_action("get_friend_list", {"no_cache": no_cache})

    def get_group_member_list(self, group_id: str, *, no_cache: bool = False) -> Any:
        return self.call_action(
            "get_group_member_list", {"group_id": int(group_id), "no_cache": no_cache}
        )

    def get_group_msg_history(
        self,
        group_id: str,
        *,
        message_seq: str | int | None = None,
        count: int = 20,
        reverse_order: bool = False,
        disable_get_url: bool = True,
        parse_mult_msg: bool = False,
    ) -> Any:
        params: dict[str, Any] = {
            "group_id": int(group_id),
            "count": count,
            "disable_get_url": disable_get_url,
            "parse_mult_msg": parse_mult_msg,
        }
        if message_seq not in {None, "", 0, "0"}:
            params["message_seq"] = str(message_seq)
            params["reverse_order"] = reverse_order
        return self.call_action("get_group_msg_history", params)

    def get_friend_msg_history(
        self,
        user_id: str,
        *,
        message_seq: str | int | None = None,
        count: int = 20,
        reverse_order: bool = False,
        disable_get_url: bool = True,
        parse_mult_msg: bool = False,
    ) -> Any:
        params: dict[str, Any] = {
            "user_id": int(user_id),
            "count": count,
            "disable_get_url": disable_get_url,
            "parse_mult_msg": parse_mult_msg,
        }
        if message_seq not in {None, "", 0, "0"}:
            params["message_seq"] = str(message_seq)
            params["reverse_order"] = reverse_order
        return self.call_action("get_friend_msg_history", params)

    def get_file(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if file_id:
            params["file_id"] = str(file_id)
        if file:
            params["file"] = str(file)
        return self.call_action("get_file", params, timeout=timeout)

    def get_image(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if file_id:
            params["file_id"] = str(file_id)
        if file:
            params["file"] = str(file)
        return self.call_action("get_image", params, timeout=timeout)

    def get_record(
        self,
        *,
        file_id: str | None = None,
        file: str | None = None,
        out_format: str | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if file_id:
            params["file_id"] = str(file_id)
        if file:
            params["file"] = str(file)
        if out_format:
            params["out_format"] = str(out_format)
        return self.call_action("get_record", params, timeout=timeout)

    def get_forward_msg(self, message_id: str) -> Any:
        return self.call_action("get_forward_msg", {"message_id": str(message_id)})
