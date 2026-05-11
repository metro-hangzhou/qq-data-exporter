from __future__ import annotations

import logging
from typing import Any

from qq_data_core import ExportPerfTraceWriter


def cleanup_gateway_media_cache(
    gateway: Any,
    *,
    trace: ExportPerfTraceWriter | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if hasattr(gateway, "reset_export_state"):
        try:
            gateway.reset_export_state()
        except Exception as exc:  # pragma: no cover - defensive operational fallback
            if logger is not None:
                logger.exception("export_reset_state_failed")
            payload.update(
                {
                    "cache_root": None,
                    "cache_cleared": False,
                    "cleanup_error": str(exc),
                    "reset_state_failed": True,
                }
            )
    if not hasattr(gateway, "cleanup_media_download_cache"):
        payload.update(
            {
                "cache_root": None,
                "cache_cleared": False,
                "skipped_reason": "cleanup_unsupported",
            }
        )
        if trace is not None:
            trace.write_event("export_cleanup_remote_cache", payload)
        return payload

    try:
        cleanup_payload = dict(gateway.cleanup_media_download_cache() or {})
        payload.update(cleanup_payload)
    except Exception as exc:  # pragma: no cover - defensive operational fallback
        payload.update(
            {
                "cache_root": None,
                "cache_cleared": False,
                "cleanup_error": str(exc),
            }
        )
        if logger is not None:
            logger.exception("export_cleanup_remote_cache_failed")
    else:
        if logger is not None:
            logger.info(
                "export_cleanup_remote_cache cache_root=%s removed_files=%s removed_dirs=%s freed_bytes=%s cache_cleared=%s skipped_reason=%s",
                payload.get("cache_root"),
                payload.get("removed_files"),
                payload.get("removed_dirs"),
                payload.get("freed_bytes"),
                payload.get("cache_cleared"),
                payload.get("skipped_reason", ""),
            )
    if trace is not None:
        trace.write_event("export_cleanup_remote_cache", payload)
    return payload
