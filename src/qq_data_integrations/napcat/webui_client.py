from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

import httpx
import orjson

from .models import NapCatLoginInfo, NapCatLoginStatus, NapCatQuickLoginAccount


class NapCatWebUiError(RuntimeError):
    pass


class NapCatWebUiAuthError(NapCatWebUiError):
    pass


class NapCatWebUiConnectError(NapCatWebUiError):
    pass


class NapCatWebUiTimeoutError(NapCatWebUiError):
    pass


class NapCatWebUiResponseError(NapCatWebUiError):
    pass


class NapCatWebUiClient:
    def __init__(
        self,
        base_url: str,
        *,
        raw_token: str | None = None,
        credential: str | None = None,
        use_system_proxy: bool = False,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._raw_token = raw_token
        self._credential = credential
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            trust_env=use_system_proxy,
        )

    def close(self) -> None:
        self._client.close()

    def ensure_authenticated(self) -> str:
        if self._credential:
            return self._credential
        if self._raw_token is None:
            raise NapCatWebUiAuthError(
                "NapCat WebUI token is required. Set NAPCAT_WEBUI_TOKEN or point NAPCAT_WEBUI_CONFIG to webui.json."
            )
        response = self._post("/auth/login", json={"hash": _hash_webui_token(self._raw_token)})
        data = self._extract_data(response)
        credential = data.get("Credential") if isinstance(data, dict) else None
        if not isinstance(credential, str) or not credential:
            raise NapCatWebUiAuthError("NapCat WebUI login did not return a credential")
        self._credential = credential
        return credential

    def check_login_status(self) -> NapCatLoginStatus:
        data = self._request("/QQLogin/CheckLoginStatus")
        return NapCatLoginStatus(
            is_login=bool(data.get("isLogin")),
            is_offline=bool(data.get("isOffline")),
            qrcode_url=_as_optional_str(data.get("qrcodeurl")),
            login_error=_as_optional_str(data.get("loginError")),
        )

    def get_qrcode(self) -> str:
        data = self._request("/QQLogin/GetQQLoginQrcode")
        qrcode = _as_optional_str(data.get("qrcode"))
        if not qrcode:
            raise NapCatWebUiError("NapCat WebUI did not return a QR code URL")
        return qrcode

    def refresh_qrcode(self) -> None:
        self._request("/QQLogin/RefreshQRcode")

    def get_login_info(self) -> NapCatLoginInfo:
        data = self._request("/QQLogin/GetQQLoginInfo")
        return NapCatLoginInfo(
            uin=_as_optional_str(data.get("uin")),
            nick=_as_optional_str(data.get("nick")),
            online=data.get("online") if isinstance(data.get("online"), bool) else None,
            avatar_url=_as_optional_str(data.get("avatarUrl")),
        )

    def get_quick_login_list(self) -> list[NapCatQuickLoginAccount]:
        accounts = _normalize_quick_login_accounts(
            self._request_data("/QQLogin/GetQuickLoginListNew")
        )
        if accounts:
            return accounts
        return _normalize_quick_login_accounts(
            self._request_data("/QQLogin/GetQuickLoginList")
        )

    def get_quick_login_uin(self) -> str | None:
        payload = self._request_data("/QQLogin/GetQuickLoginQQ")
        if isinstance(payload, dict):
            return _as_optional_str(payload.get("uin") or payload.get("qq") or payload.get("account"))
        return _as_optional_str(payload)

    def set_quick_login_uin(self, uin: str) -> None:
        self._request_data("/QQLogin/SetQuickLoginQQ", {"uin": str(uin).strip()})

    def request_quick_login(self, uin: str) -> None:
        self._request_data("/QQLogin/SetQuickLogin", {"uin": str(uin).strip()})

    def get_ob11_config(self) -> dict[str, Any]:
        data = self._request("/OB11Config/GetConfig")
        return data if isinstance(data, dict) else {}

    def set_ob11_config(self, config: dict[str, Any]) -> None:
        self._request(
            "/OB11Config/SetConfig",
            {
                "config": orjson.dumps(config).decode("utf-8"),
            },
        )

    def ensure_default_onebot_servers(
        self,
        *,
        http_url: str,
        ws_url: str,
        token: str | None,
    ) -> bool:
        config = self.get_ob11_config()
        network = config.setdefault("network", {})
        if not isinstance(network, dict):
            network = {}
            config["network"] = network

        changed = False
        changed |= _ensure_http_server(
            network,
            url=http_url,
            token=token or "",
        )
        changed |= _ensure_ws_server(
            network,
            url=ws_url,
            token=token or "",
        )
        if changed:
            self.set_ob11_config(config)
        return changed

    def _request(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = self._request_data(path, payload)
        if not isinstance(data, dict):
            return {}
        return data

    def _request_data(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        credential = self.ensure_authenticated()
        response = self._post(
            path,
            json=payload or {},
            headers={"Authorization": f"Bearer {credential}"},
        )
        return self._extract_data(response)

    def _extract_data(self, response: httpx.Response) -> Any:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise NapCatWebUiResponseError(
                f"NapCat WebUI returned HTTP {response.status_code}."
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise NapCatWebUiResponseError(
                "NapCat WebUI returned non-JSON response."
            ) from exc
        if not isinstance(payload, dict):
            raise NapCatWebUiResponseError(
                "NapCat WebUI returned unexpected JSON payload."
            )
        if payload.get("code") != 0:
            message = payload.get("message") or "NapCat WebUI request failed"
            if message == "Unauthorized":
                raise NapCatWebUiAuthError(message)
            raise NapCatWebUiError(str(message))
        return payload.get("data")

    def _post(self, path: str, **kwargs) -> httpx.Response:
        try:
            return self._client.post(path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NapCatWebUiConnectError(
                "Cannot connect to NapCat WebUI at "
                f"{self._base_url}. No service is listening there. "
                "Start or enable NapCat for your QQNT, or set NAPCAT_WEBUI_URL / NAPCAT_WORKDIR "
                "to the actual runtime."
            ) from exc
        except httpx.TimeoutException as exc:
            raise NapCatWebUiTimeoutError(
                f"NapCat WebUI timed out waiting for {path} at {self._base_url}"
            ) from exc


def _hash_webui_token(token: str) -> str:
    return hashlib.sha256(f"{token}.napcat".encode("utf-8")).hexdigest()


def _as_optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalize_quick_login_accounts(payload: Any) -> list[NapCatQuickLoginAccount]:
    if not isinstance(payload, list):
        return []
    accounts: list[NapCatQuickLoginAccount] = []
    for item in payload:
        if isinstance(item, dict):
            uin = _as_optional_str(item.get("uin") or item.get("qq") or item.get("account"))
            if not uin:
                continue
            accounts.append(
                NapCatQuickLoginAccount(
                    uin=uin,
                    nick_name=_as_optional_str(item.get("nickName") or item.get("nick")),
                    face_url=_as_optional_str(item.get("faceUrl") or item.get("avatarUrl")),
                )
            )
            continue
        uin = _as_optional_str(item)
        if uin:
            accounts.append(NapCatQuickLoginAccount(uin=uin))
    return accounts


def _ensure_http_server(network: dict[str, Any], *, url: str, token: str) -> bool:
    host, port = _parse_endpoint(url, default_port=3000)
    servers = network.setdefault("httpServers", [])
    if not isinstance(servers, list):
        servers = []
        network["httpServers"] = servers
    return _ensure_server_entry(
        servers,
        desired_name="qq-data-http",
        desired_entry={
            "name": "qq-data-http",
            "enable": True,
            "host": host,
            "port": port,
            "enableCors": True,
            "enableWebsocket": False,
            "messagePostFormat": "array",
            "token": token,
            "debug": False,
        },
    )


def _ensure_ws_server(network: dict[str, Any], *, url: str, token: str) -> bool:
    host, port = _parse_endpoint(url, default_port=3001)
    servers = network.setdefault("websocketServers", [])
    if not isinstance(servers, list):
        servers = []
        network["websocketServers"] = servers
    return _ensure_server_entry(
        servers,
        desired_name="qq-data-ws",
        desired_entry={
            "name": "qq-data-ws",
            "enable": True,
            "host": host,
            "port": port,
            "reportSelfMessage": False,
            "messagePostFormat": "array",
            "token": token,
            "enableForcePushEvent": True,
            "debug": False,
            "heartInterval": 30000,
        },
    )


def _ensure_server_entry(
    servers: list[Any],
    *,
    desired_name: str,
    desired_entry: dict[str, Any],
) -> bool:
    target = next(
        (
            item
            for item in servers
            if isinstance(item, dict)
            and (item.get("name") == desired_name or _same_address(item, desired_entry))
        ),
        None,
    )
    if target is None:
        servers.append(desired_entry)
        return True

    changed = False
    for key, value in desired_entry.items():
        if target.get(key) != value:
            target[key] = value
            changed = True
    return changed


def _same_address(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return str(left.get("host") or "").strip() == str(right.get("host") or "").strip() and str(
        left.get("port") or ""
    ).strip() == str(right.get("port") or "").strip()


def _parse_endpoint(url: str, *, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    port = parsed.port or default_port
    return host, port
