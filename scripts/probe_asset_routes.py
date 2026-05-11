from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qq_data_integrations.napcat.fast_history_client import (  # noqa: E402
    NapCatFastHistoryClient,
    NapCatFastHistoryError,
)
from qq_data_integrations.napcat.http_client import (  # noqa: E402
    NapCatApiError,
    NapCatHttpClient,
)
from qq_data_integrations.napcat.settings import NapCatSettings  # noqa: E402


DEFAULT_HTTP_TIMEOUT_S = 12.0
DEFAULT_FAST_TIMEOUT_S = 25.0
DEFAULT_PUBLIC_TIMEOUT_S = 12.0
DEFAULT_DOWNLOAD_TIMEOUT_MS = 25_000
DEFAULT_SAMPLE_BYTES = 4096


def _normalize_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _normalize_chat_type_raw(value: object) -> int | str:
    text = _normalize_text(value)
    if text is None:
        raise ValueError("chat_type_raw is required")
    try:
        return int(text)
    except ValueError:
        return text


def _looks_like_local_path(value: object) -> bool:
    text = _normalize_text(value)
    if text is None:
        return False
    if text.startswith("\\\\"):
        return True
    if len(text) >= 3 and text[1:3] == ":\\":
        return True
    if len(text) >= 3 and text[1:3] == ":/":
        return True
    return False


def _path_probe(value: object) -> dict[str, Any]:
    text = _normalize_text(value)
    if text is None:
        return {"provided": False}
    path = Path(text)
    exists = path.exists()
    info: dict[str, Any] = {
        "provided": True,
        "path": str(path),
        "exists": exists,
    }
    if exists:
        try:
            stat = path.stat()
        except OSError as exc:
            info["stat_error"] = str(exc)
        else:
            info["is_file"] = path.is_file()
            info["size"] = stat.st_size
    return info


def _first_local_path(*values: object) -> str | None:
    for value in values:
        text = _normalize_text(value)
        if text and _looks_like_local_path(text):
            return text
    return None


def _project_path_onto_http_base(candidate: str, http_base_url: str) -> str | None:
    parsed = urlparse(candidate)
    if not parsed.scheme or not parsed.netloc:
        return None
    if not parsed.path.startswith("/download"):
        return None
    http_base = http_base_url.rstrip("/") + "/"
    return urljoin(http_base, urlunparse(("", "", parsed.path, parsed.params, parsed.query, parsed.fragment)).lstrip("/"))


def _derive_projected_localhost_download_url(remote_url: str | None, http_base_url: str) -> str | None:
    candidate = _normalize_text(remote_url)
    if candidate is None:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        host = parsed.netloc.lower()
        if host.startswith("127.0.0.1") or host.startswith("localhost"):
            return candidate
        projected = _project_path_onto_http_base(candidate, http_base_url)
        return projected or candidate
    if candidate.startswith("/download") or candidate.startswith("download?"):
        return urljoin(http_base_url.rstrip("/") + "/", candidate.lstrip("/"))
    return None


def _payload_data_probe(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    summary: dict[str, Any] = {
        "keys": sorted(payload.keys()),
    }
    for key in (
        "asset_type",
        "asset_role",
        "file_name",
        "md5",
        "file_id",
        "file_size",
        "public_action",
        "public_file_token",
        "remote_url",
        "url",
        "file",
    ):
        value = payload.get(key)
        if value not in {None, ""}:
            summary[key] = value
    local_path = _first_local_path(payload.get("file"), payload.get("url"))
    if local_path is not None:
        summary["local_path_probe"] = _path_probe(local_path)
    return summary


def _summarize_hydrate_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    assets = payload.get("assets")
    asset_items = assets if isinstance(assets, list) else []
    summary: dict[str, Any] = {
        "keys": sorted(payload.keys()),
        "targeted": bool(payload.get("targeted")),
        "targeted_mode": payload.get("targeted_mode"),
        "assets_count": len(asset_items),
    }
    if asset_items:
        summary["first_asset"] = _payload_data_probe(asset_items[0])
    local_path = _first_local_path(payload.get("file"), payload.get("url"))
    if local_path is not None:
        summary["payload_local_path_probe"] = _path_probe(local_path)
    return summary


def _extract_hydrate_probe_inputs(payload: object) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return result
    for key in ("public_action", "public_file_token", "file_name", "md5", "file_id", "remote_url", "url", "file"):
        value = payload.get(key)
        if value not in {None, ""}:
            result[key] = value
    assets = payload.get("assets")
    if isinstance(assets, list) and assets:
        first = assets[0]
        if isinstance(first, dict):
            for key in ("public_action", "public_file_token", "file_name", "md5", "file_id", "remote_url", "url", "file"):
                value = first.get(key)
                if value not in {None, ""} and key not in result:
                    result[key] = value
    return result


def _summarize_json_payload(data: object) -> dict[str, Any]:
    if isinstance(data, dict):
        summary: dict[str, Any] = {"type": "dict", "keys": sorted(data.keys())}
        for key in ("status", "retcode", "code", "message", "msg", "wording"):
            if key in data and data.get(key) not in {None, ""}:
                summary[key] = data.get(key)
        nested_data = data.get("data")
        if isinstance(nested_data, dict):
            summary["data_keys"] = sorted(nested_data.keys())
        return summary
    if isinstance(data, list):
        return {"type": "list", "length": len(data)}
    return {"type": type(data).__name__, "value": data}


def _sample_text_preview(sample: bytes, *, limit: int = 240) -> str | None:
    if not sample:
        return None
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        return None
    text = text.strip()
    if not text:
        return None
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _probe_http_get(
    url: str | None,
    *,
    timeout_s: float,
    use_system_proxy: bool,
    sample_bytes: int,
) -> dict[str, Any]:
    normalized = _normalize_text(url)
    if normalized is None:
        return {"attempted": False}
    started = monotonic()
    headers = {"Range": f"bytes=0-{max(sample_bytes - 1, 0)}"} if sample_bytes > 0 else None
    try:
        with httpx.Client(
            timeout=timeout_s,
            trust_env=use_system_proxy,
            follow_redirects=True,
        ) as client:
            with client.stream("GET", normalized, headers=headers) as response:
                sample = b""
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    if sample_bytes <= 0:
                        break
                    remaining = sample_bytes - len(sample)
                    if remaining <= 0:
                        break
                    sample += chunk[:remaining]
                    if len(sample) >= sample_bytes:
                        break
                status_code = int(response.status_code)
                content_type = response.headers.get("content-type")
                content_length = response.headers.get("content-length")
                final_url = str(response.url)
                is_success = response.is_success
    except httpx.TimeoutException as exc:
        return {
            "attempted": True,
            "ok": False,
            "url": normalized,
            "elapsed_ms": int(round((monotonic() - started) * 1000)),
            "error_type": "timeout",
            "detail": str(exc),
        }
    except httpx.HTTPError as exc:
        return {
            "attempted": True,
            "ok": False,
            "url": normalized,
            "elapsed_ms": int(round((monotonic() - started) * 1000)),
            "error_type": "http_error",
            "detail": str(exc),
        }

    result: dict[str, Any] = {
        "attempted": True,
        "ok": is_success,
        "url": normalized,
        "final_url": final_url,
        "elapsed_ms": int(round((monotonic() - started) * 1000)),
        "status_code": status_code,
        "content_type": content_type,
        "content_length": content_length,
        "sample_size": len(sample),
        "sample_sha256": hashlib.sha256(sample).hexdigest() if sample else None,
    }
    preview = _sample_text_preview(sample)
    if preview is not None:
        result["sample_text_preview"] = preview
    if sample:
        try:
            payload = json.loads(sample.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if payload is not None:
            result["sample_json_summary"] = _summarize_json_payload(payload)
    return result


def _call_public_action(
    client: NapCatHttpClient,
    *,
    action: str,
    token: str,
    timeout_s: float,
) -> dict[str, Any]:
    started = monotonic()
    try:
        if action == "get_image":
            payload = client.get_image(file=token, timeout=timeout_s)
        elif action == "get_file":
            payload = client.get_file(file=token, timeout=timeout_s)
        elif action == "get_record":
            payload = client.get_record(file=token, out_format="mp3", timeout=timeout_s)
        else:
            return {
                "attempted": False,
                "action": action,
                "token": token,
                "error_type": "unsupported_action",
            }
    except NapCatApiError as exc:
        return {
            "attempted": True,
            "ok": False,
            "action": action,
            "token": token,
            "elapsed_ms": int(round((monotonic() - started) * 1000)),
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }
    elapsed_ms = int(round((monotonic() - started) * 1000))
    summary = _payload_data_probe(payload)
    summary.update(
        {
            "attempted": True,
            "ok": True,
            "action": action,
            "token": token,
            "elapsed_ms": elapsed_ms,
        }
    )
    return summary


def _probe_hydrate_forward_media(
    client: NapCatFastHistoryClient | None,
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if client is None:
        return {"attempted": False, "detail": "fast history client unavailable"}
    started = monotonic()
    request_payload = {
        "message_id_raw": args.message_id_raw,
        "element_id": args.element_id,
        "peer_uid": args.peer_uid,
        "chat_type_raw": _normalize_chat_type_raw(args.chat_type_raw),
        "asset_type": args.asset_type,
        "asset_role": args.asset_role,
        "file_name": args.file_name,
        "md5": args.md5,
        "file_id": args.file_id,
        "url": args.remote_url,
        "materialize": not args.no_materialize,
        "download_timeout_ms": args.download_timeout_ms,
    }
    compact_request_payload = {key: value for key, value in request_payload.items() if value not in {None, ""}}
    try:
        payload = client.hydrate_forward_media(
            message_id_raw=args.message_id_raw,
            element_id=args.element_id,
            peer_uid=args.peer_uid,
            chat_type_raw=_normalize_chat_type_raw(args.chat_type_raw),
            asset_type=args.asset_type,
            asset_role=args.asset_role,
            file_name=args.file_name,
            md5=args.md5,
            file_id=args.file_id,
            url=args.remote_url,
            materialize=not args.no_materialize,
            download_timeout_ms=args.download_timeout_ms,
            timeout=args.fast_timeout_s,
        )
    except NapCatFastHistoryError as exc:
        return {
            "attempted": True,
            "ok": False,
            "elapsed_ms": int(round((monotonic() - started) * 1000)),
            "request": compact_request_payload,
            "error_type": type(exc).__name__,
            "detail": str(exc),
        }
    return {
        "attempted": True,
        "ok": True,
        "elapsed_ms": int(round((monotonic() - started) * 1000)),
        "request": compact_request_payload,
        "payload_summary": _summarize_hydrate_payload(payload),
        "payload_probe_inputs": _extract_hydrate_probe_inputs(payload),
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe per-route evidence and latency for a specific NapCat-export asset.",
    )
    parser.add_argument("--message-id-raw", required=True)
    parser.add_argument("--element-id", required=True)
    parser.add_argument("--peer-uid", required=True)
    parser.add_argument("--chat-type-raw", required=True)
    parser.add_argument("--asset-type", required=True)
    parser.add_argument("--asset-role")
    parser.add_argument("--file-name")
    parser.add_argument("--md5")
    parser.add_argument("--file-id")
    parser.add_argument("--remote-url")
    parser.add_argument("--original-remote-url")
    parser.add_argument("--projected-download-url")
    parser.add_argument("--local-path")
    parser.add_argument("--public-file-token")
    parser.add_argument("--public-action")
    parser.add_argument("--http-url")
    parser.add_argument("--fast-history-url")
    parser.add_argument("--access-token")
    parser.add_argument("--use-system-proxy", action="store_true")
    parser.add_argument("--http-timeout-s", type=float, default=DEFAULT_HTTP_TIMEOUT_S)
    parser.add_argument("--fast-timeout-s", type=float, default=DEFAULT_FAST_TIMEOUT_S)
    parser.add_argument("--public-timeout-s", type=float, default=DEFAULT_PUBLIC_TIMEOUT_S)
    parser.add_argument("--download-timeout-ms", type=int, default=DEFAULT_DOWNLOAD_TIMEOUT_MS)
    parser.add_argument("--sample-bytes", type=int, default=DEFAULT_SAMPLE_BYTES)
    parser.add_argument("--no-materialize", action="store_true")
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    settings = NapCatSettings.from_env()
    http_url = _normalize_text(args.http_url) or settings.http_url
    fast_history_url = _normalize_text(args.fast_history_url) or settings.fast_history_url
    access_token = _normalize_text(args.access_token) or settings.access_token
    use_system_proxy = bool(args.use_system_proxy or settings.use_system_proxy)

    fast_client: NapCatFastHistoryClient | None = None
    if fast_history_url:
        fast_client = NapCatFastHistoryClient(
            fast_history_url,
            use_system_proxy=use_system_proxy,
            timeout=args.fast_timeout_s,
        )
    onebot_client = NapCatHttpClient(
        http_url,
        access_token=access_token,
        use_system_proxy=use_system_proxy,
        timeout=max(args.http_timeout_s, args.public_timeout_s),
    )
    try:
        hydrate_probe = _probe_hydrate_forward_media(fast_client, args=args)
        hydrate_inputs = hydrate_probe.get("payload_probe_inputs") if isinstance(hydrate_probe, dict) else {}
        hydrate_inputs = hydrate_inputs if isinstance(hydrate_inputs, dict) else {}

        original_remote_url = (
            _normalize_text(args.original_remote_url)
            or _normalize_text(args.remote_url)
            or _normalize_text(hydrate_inputs.get("remote_url"))
        )
        projected_download_url = (
            _normalize_text(args.projected_download_url)
            or _derive_projected_localhost_download_url(
                _normalize_text(args.remote_url)
                or _normalize_text(args.original_remote_url)
                or _normalize_text(hydrate_inputs.get("remote_url"))
                or _normalize_text(hydrate_inputs.get("url")),
                http_url,
            )
        )
        input_local_path = _normalize_text(args.local_path)
        hydrate_local_path = _first_local_path(hydrate_inputs.get("file"), hydrate_inputs.get("url"))

        public_token = _normalize_text(args.public_file_token) or _normalize_text(hydrate_inputs.get("public_file_token"))
        public_action = _normalize_text(args.public_action) or _normalize_text(hydrate_inputs.get("public_action"))

        result: dict[str, Any] = {
            "settings": {
                "http_url": http_url,
                "fast_history_url": fast_history_url,
                "use_system_proxy": use_system_proxy,
            },
            "request": {
                "message_id_raw": args.message_id_raw,
                "element_id": args.element_id,
                "peer_uid": args.peer_uid,
                "chat_type_raw": _normalize_chat_type_raw(args.chat_type_raw),
                "asset_type": args.asset_type,
                "asset_role": args.asset_role,
                "file_name": args.file_name,
                "md5": args.md5,
                "file_id": args.file_id,
                "remote_url": args.remote_url,
                "original_remote_url": args.original_remote_url,
                "projected_download_url": args.projected_download_url,
                "local_path": args.local_path,
                "public_file_token": args.public_file_token,
                "public_action": args.public_action,
            },
            "routes": {
                "targeted_hydrate_forward_media": hydrate_probe,
                "original_remote_url_get": _probe_http_get(
                    original_remote_url,
                    timeout_s=args.http_timeout_s,
                    use_system_proxy=use_system_proxy,
                    sample_bytes=args.sample_bytes,
                ),
                "projected_localhost_download_get": _probe_http_get(
                    projected_download_url,
                    timeout_s=args.http_timeout_s,
                    use_system_proxy=use_system_proxy,
                    sample_bytes=args.sample_bytes,
                ),
                "local_path_existence": {
                    "input_local_path": _path_probe(input_local_path),
                    "hydrate_local_path": _path_probe(hydrate_local_path),
                },
                "public_token_action_probe": (
                    _call_public_action(
                        onebot_client,
                        action=public_action,
                        token=public_token,
                        timeout_s=args.public_timeout_s,
                    )
                    if public_token and public_action
                    else {"attempted": False}
                ),
            },
            "derived": {
                "original_remote_url": original_remote_url,
                "projected_localhost_download_url": projected_download_url,
                "hydrate_local_path": hydrate_local_path,
                "public_file_token": public_token,
                "public_action": public_action,
            },
        }
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        onebot_client.close()
        if fast_client is not None:
            fast_client.close()


if __name__ == "__main__":
    raise SystemExit(main())
