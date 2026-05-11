from __future__ import annotations

from collections.abc import Mapping
import socket
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from websockets.exceptions import InvalidHandshake, InvalidStatus
from websockets.sync.client import connect as websocket_connect

from .fast_history_client import NapCatFastHistoryClient
from .settings import NapCatSettings


class NapCatEndpointProbe(BaseModel):
    name: str
    url: str
    host: str
    port: int
    listening: bool
    transport_listening: bool = False
    protocol_ready: bool = False
    protocol_identified: bool = False
    status_code: int | None = None
    detail: str | None = None


class NapCatRouteProbe(BaseModel):
    name: str
    method: str
    path: str
    reachable: bool
    status_code: int | None = None
    detail: str | None = None
    timed_out: bool = False
    connect_error: bool = False
    elapsed_ms: int | None = None


def probe_settings_endpoints(settings: NapCatSettings, *, timeout: float = 0.25) -> list[NapCatEndpointProbe]:
    return [
        probe_endpoint(
            "webui",
            settings.webui_url,
            timeout=timeout,
            access_token=settings.webui_token,
            use_system_proxy=settings.use_system_proxy,
        ),
        probe_endpoint(
            "onebot_http",
            settings.http_url,
            timeout=timeout,
            access_token=settings.access_token,
            use_system_proxy=settings.use_system_proxy,
        ),
        probe_endpoint(
            "onebot_ws",
            settings.ws_url,
            timeout=timeout,
            access_token=settings.access_token,
            use_system_proxy=settings.use_system_proxy,
        ),
    ]


def collect_debug_preflight_evidence(
    settings: NapCatSettings,
    *,
    timeout: float = 0.5,
) -> dict[str, object]:
    endpoint_probes = [probe.model_dump(mode="json") for probe in probe_settings_endpoints(settings, timeout=timeout)]
    return {
        "endpoint_probes": endpoint_probes,
        "path_matrix": collect_path_matrix(settings),
        "capability_matrix": {
            "fast_history_plugin": collect_fast_history_route_matrix(settings, timeout=timeout),
        },
    }


def collect_path_matrix(settings: NapCatSettings) -> list[dict[str, object]]:
    plugin_candidates = _plugin_path_candidates(settings)
    path_specs = [
        ("project_root", settings.project_root),
        ("export_dir", settings.export_dir),
        ("state_dir", settings.state_dir),
        ("napcat_dir", settings.napcat_dir),
        ("napcat_launcher_path", settings.napcat_launcher_path),
        ("workdir", settings.workdir),
        ("onebot_config_path", settings.onebot_config_path),
        ("webui_config_path", settings.webui_config_path),
        *[(f"fast_history_plugin_path[{index}]", path) for index, path in enumerate(plugin_candidates, start=1)],
    ]
    return [_path_probe(label, path) for label, path in path_specs]


def collect_fast_history_route_matrix(
    settings: NapCatSettings,
    *,
    timeout: float = 0.5,
) -> dict[str, object]:
    if settings.fast_history_mode == "off":
        return {
            "enabled": False,
            "base_url": settings.fast_history_url,
            "plugin_id": settings.fast_history_plugin_id,
            "routes": [],
        }
    if not settings.fast_history_url:
        return {
            "enabled": False,
            "base_url": None,
            "plugin_id": settings.fast_history_plugin_id,
            "routes": [],
        }

    client = NapCatFastHistoryClient(
        settings.fast_history_url,
        use_system_proxy=settings.use_system_proxy,
        timeout=timeout,
    )
    try:
        health = _safe_health(client)
        capabilities = _safe_capabilities(client)
    finally:
        client.close()

    return {
        "enabled": True,
        "base_url": settings.fast_history_url,
        "plugin_id": settings.fast_history_plugin_id,
        "health": health,
        "capabilities_source": "plugin_route" if capabilities.get("reachable") else "fallback",
        "routes": capabilities.get("routes", []),
    }


def probe_endpoint(
    name: str,
    url: str,
    *,
    timeout: float = 0.25,
    access_token: str | None = None,
    use_system_proxy: bool = False,
) -> NapCatEndpointProbe:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or _default_port(parsed.scheme)
    transport_listening, detail = _try_connect(host, port, timeout=timeout)
    protocol_ready = False
    protocol_identified = False
    status_code: int | None = None
    if transport_listening:
        if parsed.scheme in {"http", "https"}:
            protocol_ready, protocol_identified, status_code, detail = _probe_http_endpoint(
                name,
                url,
                timeout=timeout,
                access_token=access_token,
                use_system_proxy=use_system_proxy,
            )
        elif parsed.scheme in {"ws", "wss"}:
            protocol_ready, protocol_identified, status_code, detail = _probe_websocket_endpoint(
                url,
                timeout=timeout,
                access_token=access_token,
            )
        else:
            protocol_ready = transport_listening
    return NapCatEndpointProbe(
        name=name,
        url=url,
        host=host,
        port=port,
        listening=protocol_ready,
        transport_listening=transport_listening,
        protocol_ready=protocol_ready,
        protocol_identified=protocol_identified,
        status_code=status_code,
        detail=detail,
    )


def _default_port(scheme: str) -> int:
    if scheme in {"https", "wss"}:
        return 443
    return 80


def _try_connect(host: str, port: int, *, timeout: float) -> tuple[bool, str | None]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def _probe_http_endpoint(
    name: str,
    url: str,
    *,
    timeout: float,
    access_token: str | None,
    use_system_proxy: bool,
) -> tuple[bool, bool, int | None, str | None]:
    headers: dict[str, str] = {}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    try:
        response = httpx.get(
            url,
            timeout=timeout,
            trust_env=use_system_proxy,
            follow_redirects=False,
            headers=headers or None,
        )
    except Exception as exc:
        return False, False, None, str(exc)

    status_code = int(response.status_code)
    payload: object | None = None
    try:
        payload = response.json()
    except Exception:
        payload = None

    header_signature = _looks_like_napcat_server_header(response.headers)
    if name == "webui":
        identified = _looks_like_napcat_webui_payload(payload) or bool(header_signature)
        ready = identified and status_code < 500
        return (
            ready,
            identified,
            status_code,
            _http_probe_detail(
                status_code,
                payload,
                fallback=response.text,
                header_signature=header_signature,
            ),
        )

    identified = _looks_like_napcat_onebot_payload(payload) or bool(header_signature)
    detail = _http_probe_detail(
        status_code,
        payload,
        fallback=response.text,
        header_signature=header_signature,
    )
    if not identified:
        fallback_result = _probe_onebot_action_endpoint(
            url,
            timeout=timeout,
            headers=headers or None,
            use_system_proxy=use_system_proxy,
        )
        if fallback_result is not None:
            return fallback_result
    ready = identified and status_code < 500 and status_code not in {401, 403}
    return ready, identified, status_code, detail


def _probe_websocket_endpoint(
    url: str,
    *,
    timeout: float,
    access_token: str | None,
) -> tuple[bool, bool, int | None, str | None]:
    additional_headers: dict[str, str] | None = None
    if access_token:
        additional_headers = {"Authorization": f"Bearer {access_token}"}
    try:
        with websocket_connect(
            url,
            open_timeout=timeout,
            close_timeout=max(timeout, 0.25),
            additional_headers=additional_headers,
            user_agent_header=None,
        ):
            return True, True, 101, "websocket handshake ok"
    except InvalidStatus as exc:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        return False, True, status_code, f"websocket rejected status={status_code}"
    except InvalidHandshake as exc:
        return False, True, None, str(exc)
    except Exception as exc:
        return False, False, None, str(exc)


def _looks_like_napcat_webui_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if "code" not in payload:
        return False
    message = str(payload.get("message") or "").casefold()
    return bool(message) or payload.get("code") in {0, -1}


def _looks_like_napcat_onebot_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if "retcode" in payload and "status" in payload:
        return True
    message = str(
        payload.get("message") or payload.get("msg") or payload.get("wording") or ""
    ).casefold()
    wording = str(payload.get("wording") or payload.get("status") or "").casefold()
    return any(keyword in message or keyword in wording for keyword in ("napcat", "onebot", "qqnt"))


def _probe_onebot_action_endpoint(
    url: str,
    *,
    timeout: float,
    headers: Mapping[str, str] | None,
    use_system_proxy: bool,
) -> tuple[bool, bool, int | None, str | None] | None:
    action_url = url.rstrip("/") + "/get_status"
    try:
        response = httpx.post(
            action_url,
            json={},
            timeout=timeout,
            trust_env=use_system_proxy,
            follow_redirects=False,
            headers=dict(headers or {}),
        )
    except Exception:
        return None

    status_code = int(response.status_code)
    payload: object | None = None
    try:
        payload = response.json()
    except Exception:
        payload = None
    header_signature = _looks_like_napcat_server_header(response.headers)
    identified = _looks_like_napcat_onebot_payload(payload) or bool(header_signature)
    if not identified:
        return None
    ready = status_code < 500 and status_code not in {401, 403}
    detail = _http_probe_detail(
        status_code,
        payload,
        fallback=response.text,
        header_signature=header_signature,
    )
    return ready, identified, status_code, f"{detail} [action_probe]"


def _looks_like_napcat_server_header(headers: Mapping[str, str]) -> str | None:
    for header_name in ("server", "x-powered-by", "x-napcat-version", "napcat-version", "x-qqnt-version"):
        value = headers.get(header_name)
        if not value:
            continue
        normalized = value.casefold()
        if any(keyword in normalized for keyword in ("napcat", "qqnt", "onebot")):
            return f"{header_name}={value}"
    return None


def _http_probe_detail(
    status_code: int,
    payload: object,
    *,
    fallback: str,
    header_signature: str | None = None,
) -> str:
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("wording") or "").strip()
        if message:
            return f"http {status_code} {message}"
    text = fallback.strip().replace("\n", " ")
    if len(text) > 160:
        text = text[:160] + "...<trimmed>"
    detail = f"http {status_code} {text}" if text else f"http {status_code}"
    if header_signature:
        detail = f"{detail} [{header_signature}]"
    return detail


def _safe_health(client: NapCatFastHistoryClient) -> dict[str, object]:
    try:
        payload = client.health()
        return {
            "reachable": True,
            "status": "ok",
            "payload": payload,
        }
    except Exception as exc:
        return {
            "reachable": False,
            "status": "error",
            "detail": str(exc),
        }


def _safe_capabilities(client: NapCatFastHistoryClient) -> dict[str, object]:
    try:
        payload = client.capabilities()
    except Exception as exc:
        return {
            "reachable": False,
            "detail": str(exc),
            "routes": [],
        }
    routes = []
    for item in payload.get("routes", []):
        if not isinstance(item, dict):
            continue
        routes.append(
            NapCatRouteProbe(
                name=str(item.get("name") or "").strip() or "unknown",
                method=str(item.get("method") or "").strip() or "GET",
                path=str(item.get("path") or "").strip() or "/",
                reachable=True,
            ).model_dump(mode="json")
        )
    return {
        "reachable": True,
        "routes": routes,
    }


def _plugin_path_candidates(settings: NapCatSettings) -> list[object]:
    candidates = []
    if settings.napcat_dir is not None:
        candidates.extend(
            [
                settings.napcat_dir / "napcat" / "plugins" / settings.fast_history_plugin_id,
                settings.napcat_dir / "plugins" / settings.fast_history_plugin_id,
            ]
        )
    if settings.workdir is not None:
        candidates.append(settings.workdir / "plugins" / settings.fast_history_plugin_id)
    deduped = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        deduped.append(candidate)
    return deduped


def _path_probe(label: str, path) -> dict[str, object]:
    if path is None:
        return {
            "label": label,
            "path": None,
            "exists": False,
            "is_dir": False,
            "is_file": False,
        }
    try:
        exists = path.exists()
        is_dir = path.is_dir() if exists else False
        is_file = path.is_file() if exists else False
    except OSError as exc:
        return {
            "label": label,
            "path": str(path),
            "exists": False,
            "is_dir": False,
            "is_file": False,
            "detail": str(exc),
        }
    return {
        "label": label,
        "path": str(path),
        "exists": exists,
        "is_dir": is_dir,
        "is_file": is_file,
    }
