from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "NapCatBootstrapper": ("bootstrap", "NapCatBootstrapper"),
    "NapCatEndpointProbe": ("diagnostics", "NapCatEndpointProbe"),
    "NapCatRouteProbe": ("diagnostics", "NapCatRouteProbe"),
    "collect_debug_preflight_evidence": ("diagnostics", "collect_debug_preflight_evidence"),
    "collect_fast_history_route_matrix": ("diagnostics", "collect_fast_history_route_matrix"),
    "collect_path_matrix": ("diagnostics", "collect_path_matrix"),
    "probe_endpoint": ("diagnostics", "probe_endpoint"),
    "probe_settings_endpoints": ("diagnostics", "probe_settings_endpoints"),
    "NapCatMetadataDirectory": ("directory", "NapCatMetadataDirectory"),
    "NapCatTargetLookupError": ("directory", "NapCatTargetLookupError"),
    "FAST_HISTORY_PLUGIN_ID": ("fast_history_client", "FAST_HISTORY_PLUGIN_ID"),
    "NapCatFastHistoryClient": ("fast_history_client", "NapCatFastHistoryClient"),
    "NapCatFastHistoryConnectError": ("fast_history_client", "NapCatFastHistoryConnectError"),
    "NapCatFastHistoryError": ("fast_history_client", "NapCatFastHistoryError"),
    "NapCatFastHistoryTimeoutError": ("fast_history_client", "NapCatFastHistoryTimeoutError"),
    "NapCatFastHistoryUnavailable": ("fast_history_client", "NapCatFastHistoryUnavailable"),
    "derive_fast_history_url": ("fast_history_client", "derive_fast_history_url"),
    "NapCatGateway": ("gateway", "NapCatGateway"),
    "NapCatApiConnectError": ("http_client", "NapCatApiConnectError"),
    "NapCatApiError": ("http_client", "NapCatApiError"),
    "NapCatHttpClient": ("http_client", "NapCatHttpClient"),
    "NapCatQrLoginService": ("login", "NapCatQrLoginService"),
    "NapCatMediaDownloader": ("media_downloader", "NapCatMediaDownloader"),
    "ChatHistoryBounds": ("models", "ChatHistoryBounds"),
    "ChatTarget": ("models", "ChatTarget"),
    "MetadataCache": ("models", "MetadataCache"),
    "NapCatLoginInfo": ("models", "NapCatLoginInfo"),
    "NapCatLoginStatus": ("models", "NapCatLoginStatus"),
    "NapCatQuickLoginAccount": ("models", "NapCatQuickLoginAccount"),
    "normalize_chat_type": ("models", "normalize_chat_type"),
    "NapCatHistoryProvider": ("provider", "NapCatHistoryProvider"),
    "NapCatRealtimeProvider": ("realtime", "NapCatRealtimeProvider"),
    "NapCatLaunchInfo": ("runtime", "NapCatLaunchInfo"),
    "NapCatRuntimeStarter": ("runtime", "NapCatRuntimeStarter"),
    "NapCatStartResult": ("runtime", "NapCatStartResult"),
    "get_latest_napcat_launch_log_path": ("runtime", "get_latest_napcat_launch_log_path"),
    "NapCatSettings": ("settings", "NapCatSettings"),
    "NapCatWebUiAuthError": ("webui_client", "NapCatWebUiAuthError"),
    "NapCatWebUiClient": ("webui_client", "NapCatWebUiClient"),
    "NapCatWebUiConnectError": ("webui_client", "NapCatWebUiConnectError"),
    "NapCatWebUiError": ("webui_client", "NapCatWebUiError"),
    "NapCatWebSocketClient": ("websocket_client", "NapCatWebSocketClient"),
    "NapCatWebSocketError": ("websocket_client", "NapCatWebSocketError"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = target
    module = import_module(f"{__name__}.{module_name}")
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from .bootstrap import NapCatBootstrapper
    from .diagnostics import (
        NapCatEndpointProbe,
        NapCatRouteProbe,
        collect_debug_preflight_evidence,
        collect_fast_history_route_matrix,
        collect_path_matrix,
        probe_endpoint,
        probe_settings_endpoints,
    )
    from .directory import NapCatMetadataDirectory, NapCatTargetLookupError
    from .fast_history_client import (
        FAST_HISTORY_PLUGIN_ID,
        NapCatFastHistoryClient,
        NapCatFastHistoryConnectError,
        NapCatFastHistoryError,
        NapCatFastHistoryTimeoutError,
        NapCatFastHistoryUnavailable,
        derive_fast_history_url,
    )
    from .gateway import NapCatGateway
    from .http_client import NapCatApiConnectError, NapCatApiError, NapCatHttpClient
    from .login import NapCatQrLoginService
    from .media_downloader import NapCatMediaDownloader
    from .models import (
        ChatHistoryBounds,
        ChatTarget,
        MetadataCache,
        NapCatLoginInfo,
        NapCatLoginStatus,
        NapCatQuickLoginAccount,
        normalize_chat_type,
    )
    from .provider import NapCatHistoryProvider
    from .realtime import NapCatRealtimeProvider
    from .runtime import NapCatLaunchInfo, NapCatRuntimeStarter, NapCatStartResult, get_latest_napcat_launch_log_path
    from .settings import NapCatSettings
    from .websocket_client import NapCatWebSocketClient, NapCatWebSocketError
    from .webui_client import NapCatWebUiAuthError, NapCatWebUiClient, NapCatWebUiConnectError, NapCatWebUiError
