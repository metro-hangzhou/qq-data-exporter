from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from qq_data_cli.completion_runtime import completion_application_is_noop
from qq_data_cli.export_commands import (
    EXPORT_COMMAND_PROFILES,
    interval_is_full_history,
    interval_special_kinds,
    ParsedExportCommand,
    interval_needs_history_bounds,
    parse_root_export_command,
    resolve_interval,
)
from qq_data_cli.export_cleanup import cleanup_gateway_media_cache
from qq_data_cli.export_input import (
    move_export_date_cursor,
    roll_export_date_token,
)
from qq_data_cli.logging_utils import get_cli_log_path, get_cli_logger, setup_cli_logging
from qq_data_cli.status_display import (
    build_rich_status_text,
    format_export_result_lines,
    format_prefetch_media_progress_line,
)
from qq_data_cli.target_display import format_target_label, format_target_name, format_target_remark
from qq_data_cli.terminal_compat import (
    TerminalProbe,
    TerminalUiDecision,
    build_cli_ui_profile,
    probe_terminal_environment,
    read_requested_cli_ui_mode,
    render_cli_ui_mode_notice,
    render_terminal_doctor_lines,
    resolve_cli_ui_mode,
)
from qq_data_integrations.napcat.settings import NapCatSettings

if TYPE_CHECKING:
    from qq_data_cli.completion import SlashCommandCompleter
    from qq_data_core import ChatExportService, ExportPerfTraceWriter
    from qq_data_integrations.napcat.gateway import NapCatGateway
    from qq_data_integrations.napcat.login import NapCatQrLoginService
    from qq_data_integrations.napcat.models import ChatTarget
    from qq_data_integrations.napcat.webui_client import NapCatWebUiClient


class SlashRepl:
    COMPLETION_PRIMED_TTL_S = 120.0
    COMPLETION_PRIME_RETRY_COOLDOWN_S = 20.0
    QUICK_LOGIN_CACHE_TTL_S = 300.0
    QUICK_LOGIN_CACHE_RETRY_COOLDOWN_S = 20.0
    QUICK_LOGIN_STARTUP_PRIME_WAIT_S = 0.8

    def __init__(
        self,
        *,
        terminal_probe: TerminalProbe | None = None,
        ui_decision: TerminalUiDecision | None = None,
        startup_capture_path: Path | None = None,
        defer_startup_capture: bool = False,
    ) -> None:
        self._console = Console()
        self._service: ChatExportService | None = None
        self._fixture_loader = None
        self._settings = NapCatSettings.from_env()
        self._log_path = setup_cli_logging(self._settings.state_dir)
        self._logger = get_cli_logger("repl")
        self._runtime_starter = None
        self._bootstrapper = None
        self._gateway_init_lock = threading.Lock()
        self._target_completion_runtime_warm_lock = threading.Lock()
        self._target_completion_runtime_warm_thread: threading.Thread | None = None
        self._gateway: NapCatGateway | None = None
        self._webui_client: NapCatWebUiClient | None = None
        self._login_service: NapCatQrLoginService | None = None
        self._last_qr_url: str | None = None
        self._completion_primed_at: dict[str, float] = {}
        self._completion_prime_failed_at: dict[str, float] = {}
        self._target_cache_prime_lock = threading.Lock()
        self._target_cache_prime_threads: dict[str, threading.Thread | None] = {
            "group": None,
            "private": None,
        }
        self._quick_login_candidates_cache: list[tuple[str, str | None]] = []
        self._quick_login_candidates_cached_at: float | None = None
        self._quick_login_candidates_prime_failed_at: float | None = None
        self._quick_login_candidates_prime_thread: threading.Thread | None = None
        self._quick_login_candidates_lock = threading.Lock()
        self._quick_login_cache_notice_shown = False
        self._completer: SlashCommandCompleter | None = None
        self._session: PromptSession | None = None
        self._terminal_probe = terminal_probe or probe_terminal_environment()
        self._ui_decision = ui_decision or resolve_cli_ui_mode(
            self._terminal_probe,
            requested_mode=read_requested_cli_ui_mode(),
        )
        self._ui_profile = build_cli_ui_profile(self._ui_decision)
        self._defer_startup_capture = defer_startup_capture
        self._startup_capture_lock = threading.Lock()
        self._startup_capture_thread: threading.Thread | None = None
        if startup_capture_path is not None:
            self._startup_capture_path = startup_capture_path
        elif not defer_startup_capture:
            from qq_data_cli.startup_capture import get_latest_startup_capture_path

            self._startup_capture_path = get_latest_startup_capture_path(self._settings.state_dir)
        else:
            self._startup_capture_path = None
        self._logger.info(
            "repl_initialized state_dir=%s export_dir=%s workdir=%s log_path=%s startup_capture=%s ui_mode=%s ui_reason=%s",
            self._settings.state_dir,
            self._settings.export_dir,
            self._settings.workdir,
            self._log_path,
            self._startup_capture_path,
            self._ui_decision.resolved_mode,
            self._ui_decision.reason,
        )

    def run(self) -> None:
        self._kickoff_startup_capture_if_needed()
        self._warm_napcat_service_for_startup()
        self._kickoff_quick_login_candidates_prime_if_needed(announce=True)
        self._kickoff_target_completion_runtime_warm()
        self._wait_briefly_for_quick_login_candidates_prime()
        self._console.print("Slash REPL ready. 输入 /help 查看命令；常用有 /friends、/watch、/export。")
        ui_notice = render_cli_ui_mode_notice(self._ui_decision)
        if ui_notice:
            self._console.print(ui_notice)
        if self._startup_capture_path is not None:
            self._console.print(f"startup_capture={self._startup_capture_path}")
        self._logger.info("repl_run_start")
        try:
            if self._should_use_basic_loop():
                self._run_basic_loop()
                return

            if self._session is None:
                self._session = self._build_session()
            while True:
                try:
                    raw = self._session.prompt()
                except (EOFError, KeyboardInterrupt):
                    self._logger.info("repl_run_end reason=interactive_eof_or_interrupt")
                    self._console.print("bye")
                    return
                if self._handle_input(raw):
                    self._logger.info("repl_run_end reason=command_requested_exit")
                    return
        finally:
            self._logger.info("repl_shutdown")
            if self._gateway is not None:
                self._gateway.close()
            if self._webui_client is not None:
                self._webui_client.close()

    def _run_basic_loop(self) -> None:
        while True:
            try:
                raw = input("> ")
            except (EOFError, KeyboardInterrupt):
                self._logger.info("repl_basic_loop_end reason=eof_or_interrupt")
                self._console.print("bye")
                return
            if self._handle_input(raw):
                self._logger.info("repl_basic_loop_end reason=command_requested_exit")
                return

    def _handle_input(self, raw: str) -> bool:
        text = raw.strip()
        if not text:
            return False
        if not text.startswith("/"):
            self._console.print("请输入以 / 开头的命令；可输入 /help 查看示例。")
            return False
        return self._dispatch(text)

    def _should_use_basic_loop(self) -> bool:
        return not sys.stdin.isatty() or not sys.stdout.isatty()

    def _dispatch(self, raw: str) -> bool:
        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            self._console.print(_friendly_command_parse_error(exc))
            return False
        if not argv:
            return False

        command = argv[0].lower()
        try:
            if command == "/help":
                self._console.print("\n".join(_render_root_help_lines()))
                return False

            if command == "/doctor":
                self._handle_doctor()
                return False

            if command == "/terminal-doctor":
                self._handle_terminal_doctor()
                return False

            if command == "/quit":
                self._console.print("bye")
                return True

            if command == "/login":
                self._handle_login(argv[1:])
                return False

            if command == "/status":
                self._handle_status()
                return False

            if command == "/fixture-export":
                self._handle_fixture_export(argv)
                return False

            if command == "/groups":
                self._handle_list_targets("group", argv[1:])
                return False

            if command == "/friends":
                self._handle_list_targets("private", argv[1:])
                return False

            if command in EXPORT_COMMAND_PROFILES:
                self._handle_export(command, argv[1:])
                return False

            if command == "/watch":
                self._handle_watch(argv[1:])
                return False
        except Exception as exc:
            if exc.__class__.__name__ == "NapCatTargetLookupError":
                self._console.print(str(exc))
                matches = getattr(exc, "matches", None)
                if matches:
                    self._print_targets(matches, title="Closest Matches")
                return False
            self._logger.exception("repl_command_failed command=%s raw=%s", command, raw)
            self._console.print(_friendly_command_failure(exc))
            return False

        self._console.print(f"未识别的命令：{command}。可输入 /help 查看可用命令。")
        return False

    def _handle_login(self, argv: list[str]) -> None:
        positionals, options = _parse_options(
            argv,
            allowed_options={"timeout", "poll", "refresh", "no-quick", "quick-uin"},
            command_name="/login",
        )
        if len(positionals) > 1:
            raise ValueError("/login 最多只接受一个 QQ 号参数，例如：/login 3956020260")
        timeout_seconds = float(options.get("timeout") or 300)
        poll_interval = float(options.get("poll") or 3)
        refresh = bool(options.get("refresh"))
        use_quick_login = not bool(options.get("no-quick"))
        quick_uin = (
            str(options.get("quick-uin") or "").strip()
            or (positionals[0].strip() if positionals else "")
            or None
        )
        expected_quick_uin = quick_uin or str(self._settings.quick_login_uin or "").strip() or None

        self._ensure_endpoint_ready("webui", quick_login_uin=quick_uin)
        self._refresh_settings()
        login_service = self._require_login_service()
        initial_status = login_service.check_status()
        if initial_status.effectively_logged_in():
            ready_info = login_service.get_ready_login_info()
            if ready_info is not None:
                if expected_quick_uin and ready_info.uin and ready_info.uin != expected_quick_uin:
                    self._console.print(
                        "QQ session mismatch. "
                        f"current_uin={ready_info.uin} requested_uin={expected_quick_uin}. "
                        "Close the current NapCat/QQ session or switch the QQ account, then retry /login."
                    )
                    return
                self._console.print("QQ already logged in.")
                self._print_login_info(ready_info)
                self._refresh_settings()
                try:
                    self._ensure_endpoint_ready("onebot_http")
                    self._ensure_endpoint_ready("onebot_ws")
                    self._prime_target_cache("group", quiet=True, endpoint_ready=True)
                    self._prime_target_cache("private", quiet=True, endpoint_ready=True)
                except Exception as exc:
                    log_path = get_cli_log_path()
                    self._console.print(
                        "\n".join(
                            [
                                f"note: {exc}",
                                "note: 群/好友补全依赖 onebot_http；当前不可用时，像 /export group ssj 这样的目标补全不会弹出。",
                                f"note: 如需排查，请把 CLI 日志发回来：{log_path or ''}",
                            ]
                        )
                    )
                return
        desired_quick_uin = None
        if use_quick_login and not refresh:
            self._console.print("login_status=QQ not logged in; attempting quick login...")
            try:
                desired_quick_uin = login_service.resolve_desired_quick_login_uin(preferred_uin=quick_uin)
            except Exception:
                desired_quick_uin = expected_quick_uin
            try:
                quick_candidates = login_service.get_quick_login_candidates()
            except Exception:
                quick_candidates = []
            if quick_candidates:
                chosen_uin = (
                    desired_quick_uin
                    or quick_uin
                    or quick_candidates[0].uin
                )
                chosen_label = next(
                    (
                        candidate.display_label
                        for candidate in quick_candidates
                        if candidate.uin == chosen_uin
                    ),
                    chosen_uin,
                )
                self._console.print(f"quick_login_candidate={chosen_label}")
                quick_info = login_service.try_quick_login(
                    preferred_uin=chosen_uin,
                    timeout_seconds=min(timeout_seconds, 25.0),
                    poll_interval=min(poll_interval, 1.0),
                    on_status=self._render_login_status,
                )
                if quick_info is not None:
                    self._console.print("QQ quick login succeeded.")
                    self._print_login_info(quick_info)
                    self._refresh_settings()
                    try:
                        self._ensure_endpoint_ready("onebot_http")
                        self._ensure_endpoint_ready("onebot_ws")
                        self._prime_target_cache("group", quiet=True, endpoint_ready=True)
                        self._prime_target_cache("private", quiet=True, endpoint_ready=True)
                    except Exception as exc:
                        log_path = get_cli_log_path()
                        self._console.print(
                            "\n".join(
                                [
                                    f"note: {exc}",
                                    "note: 群/好友补全依赖 onebot_http；当前不可用时，像 /export group ssj 这样的目标补全不会弹出。",
                                    f"note: 如需排查，请把 CLI 日志发回来：{log_path or ''}",
                                ]
                        )
                    )
                    return
            self._console.print("login_status=quick login unavailable; preparing QR login...")
        info = login_service.login_until_success(
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            refresh=refresh,
            on_qrcode=self._render_login_qr,
            on_status=self._render_login_status,
        )
        self._console.print("QQ login succeeded.")
        self._print_login_info(info)
        # QQ 登录成功后，NapCat 可能刚刚生成了账号绑定的 onebot11_<uin>.json。
        # 这里立即刷新 settings，避免后续 endpoint/补全预热仍拿着登录前的旧配置视图。
        self._refresh_settings()
        try:
            self._ensure_endpoint_ready("onebot_http")
            self._ensure_endpoint_ready("onebot_ws")
            self._prime_target_cache("group", quiet=True, endpoint_ready=True)
            self._prime_target_cache("private", quiet=True, endpoint_ready=True)
        except Exception as exc:
            log_path = get_cli_log_path()
            self._console.print(
                "\n".join(
                    [
                        f"note: {exc}",
                        "note: 群/好友补全依赖 onebot_http；当前不可用时，像 /export group ssj 这样的目标补全不会弹出。",
                        f"note: 如需排查，请把 CLI 日志发回来：{log_path or ''}",
                    ]
                )
            )

    def _handle_status(self) -> None:
        from qq_data_cli.startup_capture import capture_startup_snapshot
        from qq_data_integrations.napcat.runtime import get_latest_napcat_launch_log_path

        gateway = self._require_gateway()
        terminal_probe = probe_terminal_environment()
        ui_decision = resolve_cli_ui_mode(
            terminal_probe,
            requested_mode=read_requested_cli_ui_mode(),
        )
        self._startup_capture_path = capture_startup_snapshot(
            self._settings,
            terminal_probe=terminal_probe,
            ui_decision=ui_decision,
            force_refresh=True,
            capture_profile="full",
        )
        lines = [
            f"http_url={self._settings.http_url}",
            f"ws_url={self._settings.ws_url}",
            f"webui_url={self._settings.webui_url}",
            f"fast_history_mode={self._settings.fast_history_mode}",
            f"fast_history_url={self._settings.fast_history_url or ''}",
            f"use_system_proxy={self._settings.use_system_proxy}",
            f"auto_start_napcat={self._settings.auto_start_napcat}",
            f"auto_configure_onebot={self._settings.auto_configure_onebot}",
            f"project_root={self._settings.project_root}",
            f"napcat_dir={self._settings.napcat_dir or ''}",
            f"napcat_launcher_path={self._settings.napcat_launcher_path or ''}",
            f"workdir={self._settings.workdir or ''}",
            f"onebot_config_path={self._settings.onebot_config_path or ''}",
            f"webui_config_path={self._settings.webui_config_path or ''}",
            f"export_dir={self._settings.export_dir}",
            f"state_dir={self._settings.state_dir}",
            f"log_path={get_cli_log_path() or ''}",
            f"napcat_log_path={get_latest_napcat_launch_log_path(self._settings.state_dir) or ''}",
            f"startup_capture_path={self._startup_capture_path or ''}",
            f"cached_groups={gateway.count_targets('group')}",
            f"cached_friends={gateway.count_targets('private')}",
            f"terminal_host={terminal_probe.terminal_host}",
            f"recommended_ui_mode={ui_decision.resolved_mode}",
            f"requested_ui_mode={ui_decision.requested_mode}",
        ]

        try:
            login_status = self._require_login_service().check_status()
            lines.extend(
                [
                    f"qq_logged_in={login_status.effectively_logged_in()}",
                    f"qq_offline={login_status.is_offline}",
                    f"qq_login_error={login_status.login_error or ''}",
                ]
            )
            if login_status.effectively_logged_in():
                info = self._require_login_service().get_login_info()
                lines.append(f"qq_uin={info.uin or ''}")
                lines.append(f"qq_nick={info.nick or ''}")
                lines.append(f"qq_online={info.online}")
        except Exception as exc:
            lines.append(f"qq_login_status_error={exc}")

        self._console.print("\n".join(lines))

    def _handle_doctor(self) -> None:
        from qq_data_integrations.napcat.diagnostics import probe_settings_endpoints

        self._handle_status()
        launch_info = self._require_runtime_starter().describe_launch()
        probes = probe_settings_endpoints(self._settings)
        table = Table(title="Endpoint Probes")
        table.add_column("Name")
        table.add_column("URL")
        table.add_column("Listening")
        table.add_column("Detail")
        for probe in probes:
            table.add_row(
                probe.name,
                probe.url,
                "yes" if probe.listening else "no",
                probe.detail or "",
            )
        self._console.print(table)
        self._console.print(
            "\n".join(
                [
                    f"launchable_runtime={launch_info.launchable}",
                    f"launch_reason={launch_info.reason or ''}",
                ]
            )
        )
        if not any(probe.listening for probe in probes):
            self._console.print(
                "No NapCat endpoints are listening. Install or start NapCat, then enable WebUI / OneBot in its runtime."
            )

    def _handle_terminal_doctor(self) -> None:
        probe = probe_terminal_environment()
        decision = resolve_cli_ui_mode(
            probe,
            requested_mode=read_requested_cli_ui_mode(),
        )
        self._console.print("\n".join(render_terminal_doctor_lines(probe, decision)))

    def _handle_fixture_export(self, argv: list[str]) -> None:
        from qq_data_integrations import FixtureSnapshotLoader, discover_qq_media_roots
        from qq_data_core import normalize_export_format

        if len(argv) < 3:
            raise ValueError("Usage: /fixture-export <fixture_json> <out_path> [jsonl|txt]")
        fixture_path = Path(argv[1])
        out_path = Path(argv[2])
        requested_fmt = argv[3].lower() if len(argv) > 3 else out_path.suffix.lstrip(".").lower() or "jsonl"
        fmt = normalize_export_format(requested_fmt)
        if self._fixture_loader is None:
            self._fixture_loader = FixtureSnapshotLoader()
        snapshot = self._fixture_loader.load_export(fixture_path)
        service = self._require_service()
        normalized = service.build_snapshot(snapshot)
        bundle = service.write_bundle(
            normalized,
            out_path,
            fmt=fmt,
            media_search_roots=discover_qq_media_roots(),
            media_cache_dir=self._settings.state_dir / "media_index",
        )
        self._console.print(
            f"written: {bundle.data_path} "
            f"(assets copied={bundle.copied_asset_count} reused={bundle.reused_asset_count} "
            f"missing={bundle.missing_asset_count} manifest={bundle.manifest_path})"
        )

    def _handle_list_targets(self, chat_type: str, argv: list[str]) -> None:
        positionals, options = _parse_options(
            argv,
            allowed_options={"limit", "refresh"},
            command_name=f"/{'groups' if chat_type == 'group' else 'friends'}",
        )
        keyword = positionals[0] if positionals else None
        limit = _parse_int_option(options, "limit", default=8)
        refresh = bool(options.get("refresh"))
        self._ensure_endpoint_ready("onebot_http")
        gateway = self._require_gateway()
        targets = gateway.list_targets(
            chat_type,
            keyword,
            refresh=refresh or gateway.count_targets(chat_type) == 0,
            limit=limit,
        )
        if not targets:
            self._console.print("No matches")
            return
        title = "Groups" if chat_type == "group" else "Friends"
        self._print_targets(targets, title=title)
        self._mark_completion_primed(chat_type)

    def _handle_export(self, command: str, argv: list[str]) -> None:
        positionals, options = _parse_options(
            argv,
            allowed_options={
                "format",
                "out",
                "limit",
                "data-count",
                "include-raw",
                "refresh",
                "strict-missing",
            },
            command_name=command,
        )
        parsed = parse_root_export_command(command, positionals, options, default_limit=20)

        self._ensure_endpoint_ready("onebot_http")
        assert parsed.chat_type is not None
        chat_type = _normalize_chat_type(parsed.chat_type)
        self._prime_target_cache(chat_type, quiet=False, endpoint_ready=True)
        if parsed.batch_target_queries:
            self._handle_batch_export(parsed, chat_type=chat_type)
            return

        assert parsed.target_query is not None
        target = self._resolve_target(chat_type, parsed.target_query, refresh=parsed.refresh)
        self._run_single_export(parsed, target=target, batch_prefix=None)

    def _handle_watch(self, argv: list[str]) -> None:
        from qq_data_cli.watch_view import WatchConversationView, _friendly_watch_runtime_notice
        from qq_data_core import WatchRequest

        positionals, options = _parse_options(
            argv,
            allowed_options={"refresh", "limit"},
            command_name="/watch",
        )
        if len(positionals) < 2:
            raise ValueError("Usage: /watch group|friend <name-or-id> [--refresh] [--limit N]")

        self._ensure_endpoint_ready("onebot_http")
        live_events_enabled = True
        initial_notice_text = ""
        try:
            self._ensure_endpoint_ready("onebot_ws")
        except RuntimeError as exc:
            live_events_enabled = False
            initial_notice_text = _friendly_watch_runtime_notice(
                "实时监听当前不可用",
                exc,
                suffix="当前窗口仍可继续查看历史并执行导出。",
            )
            self._logger.warning(
                "watch_live_events_disabled chat_type=%s query=%s reason=%s",
                positionals[0],
                positionals[1],
                exc,
            )
        chat_type = _normalize_chat_type(positionals[0])
        self._prime_target_cache(chat_type, quiet=False, endpoint_ready=True)
        target = self._resolve_target(chat_type, positionals[1], refresh=bool(options.get("refresh")))
        request = WatchRequest(
            chat_type=chat_type,
            chat_id=target.chat_id,
            chat_name=target.display_name,
        )
        history_limit = _parse_int_option(options, "limit", default=80)
        view = WatchConversationView(
            settings=self._settings,
            gateway=self._require_gateway(),
            service=self._require_service(),
            target=target,
            request=request,
            history_limit=history_limit,
            ui_profile=self._ui_profile,
            live_events_enabled=live_events_enabled,
            initial_notice_text=initial_notice_text,
        )
        self._logger.info(
            "watch_open chat_type=%s chat_id=%s chat_name=%s history_limit=%s live_events_enabled=%s",
            chat_type,
            target.chat_id,
            target.display_name,
            history_limit,
            live_events_enabled,
        )
        try:
            asyncio.run(view.run())
        except KeyboardInterrupt:
            self._logger.info("watch_closed reason=keyboard_interrupt chat_id=%s", target.chat_id)
            return
        except Exception as exc:
            self._logger.exception(
                "watch_crashed chat_type=%s chat_id=%s chat_name=%s",
                chat_type,
                target.chat_id,
                target.display_name,
            )
            self._console.print(_friendly_watch_crash_message(exc))
            return
        self._logger.info("watch_closed reason=application_return chat_id=%s", target.chat_id)

    def _render_login_qr(self, qr_url: str) -> None:
        from qq_data_cli.qr import build_login_qr_image_path, render_qr_text, write_qr_png

        if qr_url == self._last_qr_url:
            return
        self._last_qr_url = qr_url
        qr_image_path = write_qr_png(
            qr_url,
            build_login_qr_image_path(self._settings.project_root),
        )
        self._console.print(f"qr_image_path={qr_image_path}")
        self._console.print("请直接打开该图片扫码登录。")
        self._console.print(
            Panel.fit(
                render_qr_text(qr_url),
                title="QQ QR Login",
                subtitle="Scan with mobile QQ",
            )
        )
        self._console.print(f"qr_url={qr_url}")

    def _render_login_status(self, status) -> None:
        if status.login_error:
            self._console.print(f"login_status={status.login_error}")
        elif status.is_offline:
            self._console.print("login_status=offline")
        elif status.qrcode_url:
            self._console.print("login_status=waiting for scan/confirm")
        else:
            self._console.print("login_status=in progress")

    def _print_login_info(self, info) -> None:
        self._console.print(
            "\n".join(
                [
                    f"uin={info.uin or ''}",
                    f"nick={info.nick or ''}",
                    f"online={info.online}",
                ]
            )
        )

    def _print_targets(self, targets: list[ChatTarget], *, title: str) -> None:
        table = Table(title=title)
        table.add_column("Name")
        table.add_column("ID")
        table.add_column("Remark")
        table.add_column("Members", justify="right")
        for target in targets:
            table.add_row(
                format_target_name(target),
                target.chat_id,
                format_target_remark(target),
                "" if target.member_count is None else str(target.member_count),
            )
        self._console.print(table)

    def _resolve_target(self, chat_type: str, query: str, *, refresh: bool) -> ChatTarget:
        from qq_data_integrations.napcat.directory import NapCatTargetLookupError
        from qq_data_integrations.napcat.models import ChatTarget

        if query.isdigit():
            try:
                return self._require_gateway().resolve_target(
                    chat_type,
                    query,
                    refresh_if_missing=True,
                )
            except NapCatTargetLookupError:
                return ChatTarget(
                    chat_type=chat_type,
                    chat_id=query,
                    name=query,
                )
        if refresh:
            self._require_gateway().list_targets(chat_type, refresh=True, limit=32)
        return self._require_gateway().resolve_target(
            chat_type,
            query,
            refresh_if_missing=True,
        )

    def _handle_batch_export(self, parsed: ParsedExportCommand, *, chat_type: str) -> None:
        batch_out_dir = (parsed.out_path or self._settings.export_dir).resolve()
        batch_out_dir.mkdir(parents=True, exist_ok=True)
        total = len(parsed.batch_target_queries)
        completed = 0
        failed = 0
        for index, query in enumerate(parsed.batch_target_queries, start=1):
            batch_prefix = f"[{index}/{total}]"
            target: ChatTarget | None = None
            try:
                target = self._resolve_target(chat_type, query, refresh=parsed.refresh)
                self._run_single_export(
                    parsed,
                    target=target,
                    batch_prefix=batch_prefix,
                    output_dir=batch_out_dir,
                )
                completed += 1
            except Exception as exc:
                failed += 1
                target_hint = f" chat_id={target.chat_id}" if target is not None else ""
                log_hint = f" 日志：{get_cli_log_path()}" if get_cli_log_path() else ""
                self._console.print(
                    f"批量导出失败：{batch_prefix} {query}{target_hint} -> {exc}。"
                    f"将继续处理其余目标。{log_hint}"
                )
        self._console.print(
            f"batch_export_summary: completed={completed} failed={failed} "
            f"total={total} out_dir={batch_out_dir}"
        )

    def _run_single_export(
        self,
        parsed: ParsedExportCommand,
        *,
        target: ChatTarget,
        batch_prefix: str | None,
        output_dir: Path | None = None,
    ) -> None:
        from qq_data_cli.app import _build_zero_result_hint, _describe_runtime_session
        from qq_data_core import (
            apply_export_profile,
            build_export_content_summary,
            ExportForensicsCollector,
            ExportPerfTraceWriter,
            resolve_strict_missing_policy,
            trim_snapshot_to_last_messages,
        )

        gateway = self._require_gateway()
        session_line = _describe_runtime_session(self._settings)
        service = self._require_service()
        out_path = self._resolve_export_output_path(parsed, target=target, output_dir=output_dir)
        trace = ExportPerfTraceWriter(
            self._settings.state_dir,
            chat_type=target.chat_type,
            chat_id=target.chat_id,
            mode="root_export",
        )
        progress_display = _RootExportProgressDisplay(
            self._console,
            target_label=format_target_label(target),
            batch_prefix=batch_prefix,
        )
        progress_callback = self._build_root_export_progress_callback(
            trace=trace,
            prefix=batch_prefix,
            display=progress_display,
        )
        forensics = ExportForensicsCollector(
            self._settings.state_dir,
            chat_type=target.chat_type,
            chat_id=target.chat_id,
            policy=resolve_strict_missing_policy(parsed.strict_missing, env=os.environ),
            command_context={
                "entrypoint": "repl./export",
                "format": parsed.fmt,
                "limit": parsed.limit,
                "include_raw": parsed.include_raw,
                "profile": parsed.profile,
                "data_count": parsed.data_count,
                "strict_missing": parsed.strict_missing,
                "target_name": target.display_name,
                "batch_prefix": batch_prefix or "",
            },
        )
        forensics.capture_preflight(
            {
                "http_url": self._settings.http_url,
                "fast_history_mode": self._settings.fast_history_mode,
                "fast_history_url": self._settings.fast_history_url,
                "export_dir": str(self._settings.export_dir),
                "state_dir": str(self._settings.state_dir),
                "project_root": str(self._settings.project_root),
                "napcat_dir": str(self._settings.napcat_dir) if self._settings.napcat_dir else None,
                **self._collect_debug_preflight_evidence(),
            }
        )
        trace.write_event(
            "export_start",
            {
                "chat_name": target.display_name,
                "format": parsed.fmt,
                "limit": parsed.limit,
                "include_raw": parsed.include_raw,
                "target_dir": str(out_path.parent),
                "batch_prefix": batch_prefix or "",
            },
        )
        progress_display.start()
        report_path = None
        try:
            with trace.timed_stage(
                "repl.build_export_snapshot",
                payload={
                    "chat_type": target.chat_type,
                    "chat_id": target.chat_id,
                    "limit": parsed.limit,
                    "data_count": parsed.data_count,
                    "interval": parsed.interval,
                },
            ) as snapshot_stage:
                snapshot = self._build_export_snapshot(
                    parsed,
                    target=target,
                    progress_callback=progress_callback,
                )
                snapshot_stage.add(
                    source_history_source=str(snapshot.metadata.get("source") or ""),
                    source_message_count=len(snapshot.messages),
                )
            with trace.timed_stage(
                "repl.normalize_snapshot",
                payload={"source_message_count": len(snapshot.messages)},
            ) as normalize_stage:
                normalized = service.build_snapshot(snapshot, include_raw=parsed.include_raw)
                normalize_stage.add(normalized_message_count=len(normalized.messages))
            with trace.timed_stage(
                "repl.trim_snapshot",
                payload={"normalized_message_count": len(normalized.messages), "data_count": parsed.data_count},
            ) as trim_stage:
                normalized = trim_snapshot_to_last_messages(normalized, data_count=parsed.data_count)
                trim_stage.add(trimmed_message_count=len(normalized.messages))
            with trace.timed_stage(
                "repl.apply_export_profile",
                payload={"message_count": len(normalized.messages), "profile": parsed.profile},
            ) as profile_stage:
                normalized = apply_export_profile(normalized, parsed.profile)
                profile_stage.add(profiled_message_count=len(normalized.messages))
            with trace.timed_stage(
                "repl.write_bundle",
                payload={
                    "target_path": str(out_path),
                    "format": parsed.fmt,
                    "normalized_message_count": len(normalized.messages),
                    "profile": parsed.profile,
                },
            ) as bundle_stage:
                bundle = service.write_bundle(
                    normalized,
                    out_path,
                    fmt=parsed.fmt,
                    media_resolution_mode="napcat_only",
                    media_download_manager=(
                        gateway.build_media_download_manager()
                        if hasattr(gateway, "build_media_download_manager")
                        else None
                    ),
                    progress_callback=progress_callback,
                    forensics_collector=forensics,
                )
                bundle_stage.add(
                    copied_asset_count=bundle.copied_asset_count,
                    reused_asset_count=bundle.reused_asset_count,
                    missing_asset_count=bundle.missing_asset_count,
                    error_asset_count=bundle.error_asset_count,
                )
            with trace.timed_stage("repl.cleanup_remote_cache") as cleanup_stage:
                cleanup_stats = cleanup_gateway_media_cache(gateway, trace=trace, logger=self._logger)
                cleanup_stage.add(**cleanup_stats)
            with trace.timed_stage(
                "repl.build_export_content_summary",
                payload={"record_count": len(normalized.messages), "profile": parsed.profile},
            ):
                content_summary = build_export_content_summary(
                    normalized,
                    bundle,
                    profile=parsed.profile,
                    fmt=parsed.fmt,
                    strict_missing=parsed.strict_missing,
                )
            with trace.timed_stage(
                "repl.build_perf_summary",
                payload={"record_count": len(normalized.messages)},
            ):
                summary = trace.build_summary(record_count=len(normalized.messages))
            trace.write_event(
                "export_complete",
                {
                    "out_path": str(bundle.data_path.resolve()),
                    "manifest_path": str(bundle.manifest_path.resolve()),
                    "copied_asset_count": bundle.copied_asset_count,
                    "reused_asset_count": bundle.reused_asset_count,
                    "missing_asset_count": bundle.missing_asset_count,
                    "remote_cache_cleanup": cleanup_stats,
                    "content_summary": content_summary,
                    **summary,
                },
            )
            forensic_summary_path = None
            if forensics.enabled:
                with trace.timed_stage(
                    "repl.forensics_finalize",
                    payload={"incident_count": forensics.incident_count},
                ) as forensic_stage:
                    forensic_summary_path = forensics.finalize(
                        export_completed=True,
                        aborted=False,
                        data_path=bundle.data_path,
                        manifest_path=bundle.manifest_path,
                        trace_path=trace.path,
                        log_path=get_cli_log_path(),
                    )
                    forensic_stage.add(summary_path=str(forensic_summary_path) if forensic_summary_path else None)
            if forensic_summary_path is not None:
                bundle.forensic_summary_path = forensic_summary_path
                bundle.forensic_run_dir = forensic_summary_path.parent
                bundle.forensic_incident_count = forensics.incident_count
            zero_result_hint = _build_zero_result_hint(
                gateway,
                target=target,
                record_count=len(normalized.messages),
            )
            if zero_result_hint:
                self._console.print(zero_result_hint)
            report_path = trace.persist_report(record_count=len(normalized.messages))
            for line in format_export_result_lines(
                session_line=session_line,
                content_summary=content_summary,
                bundle=bundle,
                trace_summary=summary,
                trace_path=trace.path,
            ):
                self._console.print(build_rich_status_text(line))
            if report_path is not None:
                self._console.print(f"perf_report={report_path}")
            if int(getattr(bundle, "forensic_incident_count", 0) or 0):
                self._console.print(
                    f"forensics: incidents={getattr(bundle, 'forensic_incident_count', 0)} "
                    f"summary={getattr(bundle, 'forensic_summary_path', None)}"
                )
                self._console.print(
                    "send_back: "
                    f"manifest={bundle.manifest_path} "
                    f"trace={trace.path} "
                    f"forensic_summary={getattr(bundle, 'forensic_summary_path', None)} "
                    f"log={get_cli_log_path()}"
                )
        except Exception as exc:
            cleanup_stats = cleanup_gateway_media_cache(gateway, trace=trace, logger=self._logger)
            trace.write_event(
                "export_failed",
                {
                    "error": str(exc),
                    "remote_cache_cleanup": cleanup_stats,
                },
            )
            if forensics.enabled:
                with trace.timed_stage(
                    "repl.forensics_finalize",
                    payload={"incident_count": forensics.incident_count, "failure": True},
                ):
                    forensics.finalize(
                        export_completed=False,
                        aborted="strict missing aborted export" in str(exc).casefold(),
                        trace_path=trace.path,
                        log_path=get_cli_log_path(),
                        error=str(exc),
                    )
            raise
        finally:
            progress_display.stop()
            trace.close()
            if report_path is None:
                trace.persist_report(record_count=len(normalized.messages) if 'normalized' in locals() else None)

    def _resolve_export_output_path(
        self,
        parsed: ParsedExportCommand,
        *,
        target: ChatTarget,
        output_dir: Path | None,
    ) -> Path:
        from qq_data_core import build_default_output_path

        if output_dir is None and parsed.out_path is not None and not parsed.batch_target_queries:
            return parsed.out_path
        base_dir = (output_dir or self._settings.export_dir).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
        return build_default_output_path(
            base_dir,
            chat_type=target.chat_type,
            chat_id=target.chat_id,
            fmt=parsed.fmt,
        )

    def _build_root_export_progress_callback(
        self,
        *,
        trace: "ExportPerfTraceWriter",
        prefix: str | None,
        display: "_RootExportProgressDisplay",
    ):
        state: dict[str, object] = {
            "last_text": "",
            "last_emit": 0.0,
            "last_pages": -1,
            "last_current": -1,
            "last_phase": "",
        }
        label_prefix = f"{prefix} " if prefix else ""

        def callback(update: dict[str, object]) -> None:
            phase = str(update.get("phase") or "progress")
            trace.write_event(phase, update)
            if phase == "download_assets":
                download_text = self._format_root_export_download_progress(update, prefix=label_prefix)
                display.update_download_progress(download_text or "")
                return
            text = self._format_root_export_progress(update, prefix=label_prefix)
            if not text:
                return
            now = monotonic()
            if phase == "materialize_assets":
                current = int(update.get("current") or 0)
                total = int(update.get("total") or 0)
                last_current = int(state.get("last_current") or -1)
                if (
                    current < total
                    and current > last_current
                    and current - last_current < 8
                    and now - float(state.get("last_emit") or 0.0) < 0.25
                ):
                    return
                state["last_current"] = current
            elif phase in {"bounds_scan", "interval_scan", "interval_tail_scan", "tail_scan", "full_scan"}:
                pages = int(update.get("pages_scanned") or 0)
                last_pages = int(state.get("last_pages") or -1)
                if pages == last_pages and now - float(state.get("last_emit") or 0.0) < 0.75:
                    return
                state["last_pages"] = pages
            elif phase == "forward_expand":
                processed = int(update.get("processed_forwards") or 0)
                last_processed = int(state.get("last_processed_forwards") or -1)
                if (
                    processed == last_processed
                    and now - float(state.get("last_emit") or 0.0) < 0.75
                ):
                    return
                state["last_processed_forwards"] = processed
            elif text == state.get("last_text"):
                return
            state["last_text"] = text
            state["last_emit"] = now
            state["last_phase"] = phase
            display.update_progress(text)

        return callback

    def _format_root_export_progress(self, update: dict[str, object], *, prefix: str) -> str | None:
        from qq_data_core import format_export_datetime

        phase = str(update.get("phase") or "")
        elapsed_s = float(update.get("elapsed_s") or 0.0)
        rate_suffix = f" rate={float(update.get('rate_per_s') or 0.0):.1f}/s elapsed={elapsed_s:.1f}s" if elapsed_s > 0 else ""
        pages_scanned = int(update.get("pages_scanned") or 0)

        if phase == "bounds_scan":
            earliest = update.get("earliest_content_at")
            final = update.get("final_content_at")
            parts = [f"{prefix}export_progress: scanning bounds pages={pages_scanned}"]
            if earliest is not None and final is not None:
                parts.append(
                    f"window={format_export_datetime(earliest)}..{format_export_datetime(final)}"
                )
            if rate_suffix:
                parts.append(rate_suffix.strip())
            return " ".join(parts)

        if phase in {"interval_scan", "interval_tail_scan", "tail_scan"}:
            oldest = update.get("oldest_content_at")
            newest = update.get("newest_content_at")
            matched_messages = int(update.get("matched_messages") or 0)
            requested_data_count = int(update.get("requested_data_count") or 0)
            page_size = int(update.get("page_size") or 0)
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            label = {
                "interval_scan": "scanning interval",
                "interval_tail_scan": "scanning interval tail",
                "tail_scan": "scanning recent tail",
            }[phase]
            detail = f"{prefix}export_progress: {label} pages={pages_scanned} "
            if phase == "interval_scan":
                detail += f"matched={matched_messages} "
            else:
                detail += f"matched={matched_messages}/{requested_data_count} "
            detail += f"page_size={page_size} page={page_duration_s:.2f}s"
            if rate_suffix:
                detail += rate_suffix
            if oldest is not None and newest is not None:
                detail += (
                    f" page_window={format_export_datetime(oldest)}.."
                    f"{format_export_datetime(newest)}"
                )
            return detail

        if phase == "full_scan":
            earliest = update.get("earliest_content_at")
            collected_messages = int(update.get("collected_messages") or 0)
            page_size = int(update.get("page_size") or 0)
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            detail = (
                f"{prefix}export_progress: scanning full history pages={pages_scanned} "
                f"collected={collected_messages} page_size={page_size} page={page_duration_s:.2f}s"
            )
            if rate_suffix:
                detail += rate_suffix
            if earliest is not None:
                detail += f" earliest={format_export_datetime(earliest)}"
            return detail

        if phase == "forward_expand":
            processed = int(update.get("processed_forwards") or 0)
            total = int(update.get("total_forwards") or 0)
            resolved = int(update.get("resolved_forwards") or 0)
            detail = (
                f"{prefix}export_progress: expanding forwarded detail "
                f"{processed}/{total} resolved={resolved}"
            )
            if elapsed_s > 0 and processed > 0:
                detail += f" rate={processed / elapsed_s:.1f}/s elapsed={elapsed_s:.1f}s"
            return detail

        if phase == "write_data_file":
            stage = str(update.get("stage") or "start")
            record_count = int(update.get("record_count") or 0)
            status = "success" if stage == "done" else "in progress"
            action = "wrote" if stage == "done" else "writing"
            return f"status={status} {prefix}export_progress: {action} data file records={record_count}"

        if phase in {"prefetch_media", "prefetch_media_prepare", "prefetch_media_chunk"}:
            return format_prefetch_media_progress_line(update, prefix=prefix)

        if phase == "materialize_assets":
            current = int(update.get("current") or 0)
            total = int(update.get("total") or 0)
            asset_type = str(update.get("asset_type") or "-")
            asset_role = str(update.get("asset_role") or "").strip()
            role_suffix = f".{asset_role}" if asset_role else ""
            copied = int(update.get("copied_assets") or 0)
            reused = int(update.get("reused_assets") or 0)
            missing = int(update.get("missing_assets") or 0)
            errors = int(update.get("error_assets") or 0)
            detail = (
                f"status=in progress {prefix}export_progress: materializing assets {current}/{total} "
                f"{asset_type}{role_suffix} copied={copied} reused={reused} "
                f"missing={missing} err={errors}"
            )
            if elapsed_s > 0 and current > 0:
                detail += f" rate={current / elapsed_s:.1f}/s elapsed={elapsed_s:.1f}s"
            return detail

        if phase == "forensic_incident" and str(update.get("stage") or "") == "recorded":
            if not bool(update.get("is_new_incident")):
                return None
            incident_id = str(update.get("incident_id") or "-")
            reason_category = str(update.get("reason_category") or "unknown")
            asset_type = str(update.get("asset_type") or "-")
            file_name = str(update.get("file_name") or "").strip() or "-"
            incident_path = str(update.get("incident_path") or "").strip()
            detail = (
                f"{prefix}export_incident: {incident_id} reason={reason_category} "
                f"asset={asset_type}:{file_name}"
            )
            if incident_path:
                detail += f" forensic={incident_path}"
            return detail

        if phase == "materialize_asset_substep" and str(update.get("stage") or "") == "done":
            status = str(update.get("status") or "")
            if status not in {"timeout", "unavailable", "storm_skip"}:
                return None
            substep = str(update.get("substep") or "-")
            asset_type = str(update.get("asset_type") or "-")
            file_name = str(update.get("file_name") or "").strip() or "-"
            timeout_s = float(update.get("timeout_s") or 0.0)
            elapsed = float(update.get("elapsed_s") or 0.0)
            detail = (
                f"status=in progress {prefix}export_progress: asset substep {status} substep={substep} "
                f"asset={asset_type}:{file_name}"
            )
            if timeout_s > 0:
                detail += f" timeout={timeout_s:.1f}s"
            if elapsed > 0:
                detail += f" elapsed={elapsed:.1f}s"
            detail += " continuing=1"
            return detail
        return None

    def _format_root_export_download_progress(
        self,
        update: dict[str, object],
        *,
        prefix: str,
    ) -> str | None:
        stage = str(update.get("stage") or "progress")
        total = int(update.get("candidate_total") or update.get("download_total") or 0)
        completed = int(update.get("completed") or update.get("download_completed") or 0)
        failed = int(update.get("failed") or update.get("download_failed") or 0)
        inflight = int(update.get("active") or update.get("download_inflight") or 0)
        queued = int(update.get("queued") or 0)
        cached = int(update.get("cached") or update.get("download_cached") or 0)
        eager = int(update.get("eager_remote_candidates") or 0)
        token = int(update.get("public_token_candidates") or 0)
        context = int(update.get("context_candidates") or 0)
        timeout_count = int(update.get("timeout_count") or 0)
        forward_context_timeouts = int(update.get("forward_context_timeout_count") or 0)
        forward_context_empty = int(update.get("forward_context_empty_count") or 0)
        forward_context_errors = int(update.get("forward_context_error_count") or 0)
        forward_context_unavailable = int(update.get("forward_context_unavailable_count") or 0)
        forward_timeout_storm_skips = int(update.get("forward_timeout_storm_skip_count") or 0)
        last_asset_type = str(update.get("last_asset_type") or "").strip()
        last_file_name = str(update.get("last_file_name") or "").strip()
        last_status = str(update.get("last_status") or "").strip()
        if stage == "done" and not total:
            return ""
        status = {
            "start": "in progress",
            "progress": "in progress",
            "done": "success",
            "error": "failed",
        }.get(stage, "in progress")
        parts = [f"status={status}", f"{prefix}remote_downloads(subqueue): {stage}"]
        parts.append(f"candidates={total}")
        parts.append(f"ok={completed}")
        parts.append(f"cached={cached}")
        parts.append(f"failed={failed}")
        parts.append(f"queued={queued}")
        parts.append(f"inflight={inflight}")
        if stage == "start":
            parts.append(f"sources=eager:{eager}/token:{token}/context:{context}")
        if last_asset_type and last_status:
            last_label = last_asset_type
            if last_file_name:
                last_label = f"{last_label}:{last_file_name}"
            parts.append(f"last={last_status}@{last_label}")
        diag_parts: list[str] = []
        if timeout_count > 0:
            diag_parts.append(f"timeouts={timeout_count}")
        if forward_context_timeouts > 0:
            diag_parts.append(f"forward_meta_timeout={forward_context_timeouts}")
        if forward_context_empty > 0:
            diag_parts.append(f"forward_meta_empty={forward_context_empty}")
        if forward_context_errors > 0:
            diag_parts.append(f"forward_meta_error={forward_context_errors}")
        if forward_context_unavailable > 0:
            diag_parts.append(f"forward_meta_unavailable={forward_context_unavailable}")
        if forward_timeout_storm_skips > 0:
            diag_parts.append(f"forward_timeout_breaker={forward_timeout_storm_skips}")
        if diag_parts:
            parts.append("diag=" + ",".join(diag_parts))
        return " ".join(parts)

    def _build_export_snapshot(self, parsed: ParsedExportCommand, *, target: ChatTarget, progress_callback=None):
        from qq_data_core import ExportRequest, format_export_datetime

        requested_window = parsed.data_count or parsed.limit or 100
        history_page_size = max(100, min(requested_window, 500))
        request = ExportRequest(
            chat_type=target.chat_type,
            chat_id=target.chat_id,
            chat_name=target.display_name,
            limit=parsed.data_count or parsed.limit,
            include_raw=parsed.include_raw,
        )
        gateway = self._require_gateway()
        if parsed.interval is None:
            if parsed.data_count:
                return gateway.fetch_snapshot_tail(
                    request,
                    data_count=parsed.data_count,
                    page_size=history_page_size,
                    progress_callback=progress_callback,
                )
            return gateway.fetch_snapshot(request, progress_callback=progress_callback)

        if interval_is_full_history(parsed.interval):
            snapshot = gateway.fetch_full_snapshot(
                request,
                page_size=history_page_size,
                progress_callback=progress_callback,
            )
            resolved_since = snapshot.metadata.get("resolved_since")
            resolved_until = snapshot.metadata.get("resolved_until")
            if resolved_since:
                snapshot.metadata["resolved_since"] = format_export_datetime(
                    datetime.fromisoformat(str(resolved_since))
                )
            if resolved_until:
                snapshot.metadata["resolved_until"] = format_export_datetime(
                    datetime.fromisoformat(str(resolved_until))
                )
            return snapshot

        bounds = None
        if interval_needs_history_bounds(parsed.interval):
            special_kinds = interval_special_kinds(parsed.interval)
            bounds = gateway.get_history_bounds(
                request,
                page_size=history_page_size,
                need_earliest="earliest_content" in special_kinds,
                need_final="final_content" in special_kinds,
                progress_callback=progress_callback,
            )
        interval_start, interval_end = resolve_interval(parsed.interval, bounds=bounds)
        interval_request = request.model_copy(
            update={
                "since": interval_start,
                "until": interval_end,
            }
        )
        if parsed.data_count:
            snapshot = gateway.fetch_snapshot_tail_between(
                interval_request,
                data_count=parsed.data_count,
                page_size=history_page_size,
                progress_callback=progress_callback,
            )
        else:
            snapshot = gateway.fetch_snapshot_between(
                interval_request,
                page_size=history_page_size,
                progress_callback=progress_callback,
            )
        snapshot.metadata["resolved_since"] = format_export_datetime(min(interval_start, interval_end))
        snapshot.metadata["resolved_until"] = format_export_datetime(max(interval_start, interval_end))
        snapshot.metadata["interval_mode"] = "closed"
        return snapshot

    def _lookup_targets_for_completion(
        self,
        chat_type: str,
        keyword: str | None,
        limit: int,
    ) -> list[ChatTarget]:
        lookup_started = monotonic()
        gateway_started = monotonic()
        gateway = self._require_gateway()
        gateway_elapsed_ms = (monotonic() - gateway_started) * 1000.0
        cached_count_started = monotonic()
        cached_target_count = gateway.count_cached_targets(chat_type)
        cached_count_elapsed_ms = (monotonic() - cached_count_started) * 1000.0
        if cached_target_count > 0:
            cached_search_started = monotonic()
            targets = gateway.list_cached_targets(chat_type, keyword, limit=limit)
            cached_search_elapsed_ms = (monotonic() - cached_search_started) * 1000.0
            self._kickoff_target_cache_prime_if_needed(chat_type)
            total_elapsed_ms = (monotonic() - lookup_started) * 1000.0
            self._logger.info(
                "completion_target_lookup chat_type=%s keyword=%r mode=cached cached_targets=%s "
                "gateway_ms=%.1f cached_count_ms=%.1f cached_search_ms=%.1f total_ms=%.1f result_count=%s",
                chat_type,
                keyword,
                cached_target_count,
                gateway_elapsed_ms,
                cached_count_elapsed_ms,
                cached_search_elapsed_ms,
                total_elapsed_ms,
                len(targets),
            )
            return targets

        prime_started = monotonic()
        self._prime_target_cache(chat_type, quiet=True)
        prime_elapsed_ms = (monotonic() - prime_started) * 1000.0
        gateway = self._require_gateway()
        lookup_search_started = monotonic()
        targets = gateway.list_targets(chat_type, keyword, limit=limit)
        lookup_search_elapsed_ms = (monotonic() - lookup_search_started) * 1000.0
        total_elapsed_ms = (monotonic() - lookup_started) * 1000.0
        self._logger.info(
            "completion_target_lookup chat_type=%s keyword=%r mode=sync_prime cached_targets=0 "
            "gateway_ms=%.1f cached_count_ms=%.1f prime_ms=%.1f lookup_ms=%.1f total_ms=%.1f result_count=%s",
            chat_type,
            keyword,
            gateway_elapsed_ms,
            cached_count_elapsed_ms,
            prime_elapsed_ms,
            lookup_search_elapsed_ms,
            total_elapsed_ms,
            len(targets),
        )
        return targets

    def _require_gateway(self) -> NapCatGateway:
        from qq_data_integrations.napcat.gateway import NapCatGateway

        if self._gateway is None:
            with self._gateway_init_lock:
                if self._gateway is None:
                    started = monotonic()
                    self._gateway = NapCatGateway(self._settings)
                    self._logger.info(
                        "gateway_initialized duration_ms=%.1f http_url=%s fast_history_mode=%s",
                        (monotonic() - started) * 1000.0,
                        self._settings.http_url,
                        self._settings.fast_history_mode,
                    )
        return self._gateway

    def _require_service(self) -> "ChatExportService":
        from qq_data_core import ChatExportService

        if self._service is None:
            self._service = ChatExportService()
        return self._service

    def _require_runtime_starter(self):
        from qq_data_integrations.napcat.runtime import NapCatRuntimeStarter

        if self._runtime_starter is None:
            self._runtime_starter = NapCatRuntimeStarter(self._settings)
        return self._runtime_starter

    def _require_bootstrapper(self):
        from qq_data_integrations.napcat.bootstrap import NapCatBootstrapper

        if self._bootstrapper is None:
            self._bootstrapper = NapCatBootstrapper(
                self._settings,
                runtime_starter=self._require_runtime_starter(),
                settings_loader=NapCatSettings.from_env,
            )
        return self._bootstrapper

    def _kickoff_startup_capture_if_needed(self) -> None:
        if not self._defer_startup_capture:
            return
        if self._startup_capture_path is not None:
            return
        if self._startup_capture_thread is not None and self._startup_capture_thread.is_alive():
            return

        settings = self._settings.model_copy(deep=True)
        terminal_probe = self._terminal_probe
        ui_decision = self._ui_decision

        def _worker() -> None:
            try:
                from qq_data_cli.startup_capture import capture_startup_snapshot

                path = capture_startup_snapshot(
                    settings,
                    terminal_probe=terminal_probe,
                    ui_decision=ui_decision,
                    capture_profile="startup",
                )
                with self._startup_capture_lock:
                    self._startup_capture_path = path
                if path is not None:
                    self._logger.info("startup_capture_path=%s", path)
            except Exception:
                self._logger.exception("startup_capture_background_failed")

        self._startup_capture_thread = threading.Thread(
            target=_worker,
            name="startup-capture",
            daemon=True,
        )
        self._startup_capture_thread.start()

    def _refresh_settings(self) -> None:
        self._settings = NapCatSettings.from_env()
        self._terminal_probe = probe_terminal_environment()
        self._ui_decision = resolve_cli_ui_mode(
            self._terminal_probe,
            requested_mode=read_requested_cli_ui_mode(),
        )
        self._ui_profile = build_cli_ui_profile(self._ui_decision)
        self._runtime_starter = None
        self._bootstrapper = None
        if self._gateway is not None:
            self._gateway.close()
            self._gateway = None
        if self._webui_client is not None:
            self._webui_client.close()
            self._webui_client = None
        self._login_service = None
        self._completion_primed_at.clear()
        self._completion_prime_failed_at.clear()
        with self._target_completion_runtime_warm_lock:
            self._target_completion_runtime_warm_thread = None
        with self._target_cache_prime_lock:
            self._target_cache_prime_threads = {
                "group": None,
                "private": None,
            }
        with self._quick_login_candidates_lock:
            self._quick_login_candidates_cache.clear()
            self._quick_login_candidates_cached_at = None
            self._quick_login_candidates_prime_failed_at = None
            self._quick_login_candidates_prime_thread = None
            self._quick_login_cache_notice_shown = False

    def _ensure_endpoint_ready(self, endpoint: str, *, quick_login_uin: str | None = None) -> None:
        result = self._require_bootstrapper().ensure_endpoint(
            endpoint,
            quick_login_uin=quick_login_uin,
        )
        if result.already_running:
            self._assert_expected_runtime_session(endpoint)
            return
        if result.ready:
            if result.attempted_start or result.attempted_configure:
                self._console.print(result.message)
                self._refresh_settings()
            self._assert_expected_runtime_session(endpoint)
            return
        raise RuntimeError(result.message)

    def _assert_expected_runtime_session(self, endpoint: str) -> None:
        if endpoint == "webui":
            return
        expected_uin = str(self._settings.quick_login_uin or "").strip()
        if not expected_uin:
            return
        try:
            from qq_data_integrations.napcat.login import detect_session_mismatch

            mismatch_message = detect_session_mismatch(
                self._require_login_service(),
                expected_uin=expected_uin,
            )
        except Exception as exc:
            self._logger.debug(
                "session_mismatch_check_skipped endpoint=%s error=%s",
                endpoint,
                exc,
            )
            return
        if mismatch_message:
            raise RuntimeError(mismatch_message)

    def _prime_target_cache(self, chat_type: str, *, quiet: bool, endpoint_ready: bool = False) -> None:
        total_started = monotonic()
        if self._completion_cache_is_fresh(
            self._completion_primed_at,
            chat_type,
            ttl_s=self.COMPLETION_PRIMED_TTL_S,
        ):
            self._logger.info(
                "completion_prime_skipped chat_type=%s reason=fresh_cache quiet=%s total_ms=%.1f",
                chat_type,
                quiet,
                (monotonic() - total_started) * 1000.0,
            )
            return
        if quiet and self._completion_cache_is_fresh(
            self._completion_prime_failed_at,
            chat_type,
            ttl_s=self.COMPLETION_PRIME_RETRY_COOLDOWN_S,
        ):
            self._logger.info(
                "completion_prime_skipped chat_type=%s reason=retry_cooldown quiet=%s total_ms=%.1f",
                chat_type,
                quiet,
                (monotonic() - total_started) * 1000.0,
            )
            return

        gateway = self._require_gateway()
        count_started = monotonic()
        has_cached_targets = gateway.count_cached_targets(chat_type) > 0
        cached_count_elapsed_ms = (monotonic() - count_started) * 1000.0

        try:
            ensure_elapsed_ms = 0.0
            if not endpoint_ready:
                ensure_started = monotonic()
                self._ensure_endpoint_ready("onebot_http")
                ensure_elapsed_ms = (monotonic() - ensure_started) * 1000.0
            gateway = self._require_gateway()
            if not quiet and not has_cached_targets:
                label = "群聊" if chat_type == "group" else "好友"
                self._console.print(f"runtime_note: 正在从 NapCat 预加载{label}缓存...")
            list_started = monotonic()
            targets = gateway.list_targets(chat_type, refresh=not has_cached_targets, limit=32)
            list_elapsed_ms = (monotonic() - list_started) * 1000.0
        except Exception as exc:
            self._logger.warning(
                "completion_prime_failed chat_type=%s quiet=%s has_cached_targets=%s cached_count_ms=%.1f "
                "total_ms=%.1f error=%s",
                chat_type,
                quiet,
                has_cached_targets,
                cached_count_elapsed_ms,
                (monotonic() - total_started) * 1000.0,
                str(exc or "").strip() or exc.__class__.__name__,
            )
            if has_cached_targets:
                self._completion_prime_failed_at[chat_type] = monotonic()
                return
            self._completion_prime_failed_at[chat_type] = monotonic()
            if not quiet:
                raise
            return

        self._mark_completion_primed(chat_type)
        self._logger.info(
            "completion_prime_ready chat_type=%s quiet=%s has_cached_targets=%s cached_count_ms=%.1f "
            "ensure_ms=%.1f list_ms=%.1f total_ms=%.1f result_count=%s",
            chat_type,
            quiet,
            has_cached_targets,
            cached_count_elapsed_ms,
            ensure_elapsed_ms,
            list_elapsed_ms,
            (monotonic() - total_started) * 1000.0,
            len(targets),
        )

    def _mark_completion_primed(self, chat_type: str) -> None:
        self._completion_primed_at[chat_type] = monotonic()
        self._completion_prime_failed_at.pop(chat_type, None)

    def _kickoff_target_cache_prime_if_needed(self, chat_type: str) -> None:
        if self._completion_cache_is_fresh(
            self._completion_primed_at,
            chat_type,
            ttl_s=self.COMPLETION_PRIMED_TTL_S,
        ):
            return
        if self._completion_cache_is_fresh(
            self._completion_prime_failed_at,
            chat_type,
            ttl_s=self.COMPLETION_PRIME_RETRY_COOLDOWN_S,
        ):
            return
        with self._target_cache_prime_lock:
            thread = self._target_cache_prime_threads.get(chat_type)
            if thread is not None and thread.is_alive():
                return

            def _worker() -> None:
                started = monotonic()
                try:
                    self._prime_target_cache(chat_type, quiet=True)
                except Exception:
                    self._logger.exception(
                        "completion_prime_background_failed chat_type=%s",
                        chat_type,
                    )
                finally:
                    self._logger.info(
                        "completion_prime_background_done chat_type=%s total_ms=%.1f",
                        chat_type,
                        (monotonic() - started) * 1000.0,
                    )
                    with self._target_cache_prime_lock:
                        self._target_cache_prime_threads[chat_type] = None

            thread = threading.Thread(
                target=_worker,
                name=f"target-prime-{chat_type}",
                daemon=True,
            )
            self._target_cache_prime_threads[chat_type] = thread
            thread.start()

    def _kickoff_target_completion_runtime_warm(self) -> None:
        with self._target_completion_runtime_warm_lock:
            thread = self._target_completion_runtime_warm_thread
            if thread is not None and thread.is_alive():
                return

            def _worker() -> None:
                started = monotonic()
                try:
                    gateway = self._require_gateway()
                    group_cached = gateway.count_cached_targets("group")
                    private_cached = gateway.count_cached_targets("private")
                    self._logger.info(
                        "completion_runtime_warm_ready total_ms=%.1f cached_groups=%s cached_friends=%s",
                        (monotonic() - started) * 1000.0,
                        group_cached,
                        private_cached,
                    )
                except Exception:
                    self._logger.exception("completion_runtime_warm_failed")
                finally:
                    with self._target_completion_runtime_warm_lock:
                        self._target_completion_runtime_warm_thread = None

            thread = threading.Thread(
                target=_worker,
                name="completion-runtime-warm",
                daemon=True,
            )
            self._target_completion_runtime_warm_thread = thread
            thread.start()

    @staticmethod
    def _completion_cache_is_fresh(
        cache: dict[str, float],
        chat_type: str,
        *,
        ttl_s: float,
    ) -> bool:
        marked_at = cache.get(chat_type)
        if marked_at is None:
            return False
        if monotonic() - marked_at <= ttl_s:
            return True
        cache.pop(chat_type, None)
        return False

    def _require_login_service(self) -> NapCatQrLoginService:
        from qq_data_integrations.napcat.login import NapCatQrLoginService
        from qq_data_integrations.napcat.webui_client import NapCatWebUiClient

        if self._login_service is None:
            self._webui_client = NapCatWebUiClient(
                self._settings.webui_url,
                raw_token=self._settings.webui_token,
                use_system_proxy=self._settings.use_system_proxy,
            )
            self._login_service = NapCatQrLoginService(self._webui_client)
        return self._login_service

    def _build_session(self) -> PromptSession:
        from qq_data_cli.completion import SlashCommandCompleter
        from qq_data_cli.export_input import ExportCommandLexer, ExportDateDisplayProcessor

        history_path = self._settings.state_dir / "cli_history.txt"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        if self._completer is None:
            self._completer = SlashCommandCompleter(
                target_lookup=self._lookup_targets_for_completion,
                quick_login_lookup=self._lookup_quick_login_candidates_for_completion,
            )
        session_kwargs: dict[str, Any] = {
            "completer": self._completer,
            "history": FileHistory(str(history_path)),
            "key_bindings": _build_key_bindings(),
            "lexer": ExportCommandLexer(),
            "input_processors": [ExportDateDisplayProcessor()],
            "complete_while_typing": self._ui_profile.complete_while_typing,
        }
        if self._ui_profile.use_highlight_style:
            session_kwargs["style"] = Style.from_dict({"export-date-literal": "bg:#ffffff #000000"})
        if self._ui_profile.show_completion_menu:
            session_kwargs["reserve_space_for_menu"] = _completion_menu_reserve_lines(
                self._ui_profile,
                self._terminal_probe,
            )
            session_kwargs["complete_style"] = _completion_style_for_ui_profile(self._ui_profile)
        session = PromptSession(
            "> ",
            **session_kwargs,
        )
        session.default_buffer.on_text_changed += self._handle_buffer_text_changed
        return session

    def _collect_debug_preflight_evidence(self) -> dict[str, Any]:
        from qq_data_integrations.napcat.diagnostics import collect_debug_preflight_evidence

        return collect_debug_preflight_evidence(self._settings)

    def _lookup_quick_login_candidates_for_completion(
        self,
        keyword: str | None,
        limit: int,
    ):
        from qq_data_integrations.napcat.models import NapCatQuickLoginAccount

        keyword_text = str(keyword or "").strip().casefold()
        seen: set[str] = set()
        results: list[NapCatQuickLoginAccount] = []

        def _append(uin: str | None, nick_name: str | None = None) -> None:
            value = str(uin or "").strip()
            if not value or value in seen:
                return
            if keyword_text:
                nick = str(nick_name or "").strip()
                if keyword_text not in value.casefold() and keyword_text not in nick.casefold():
                    return
            seen.add(value)
            results.append(NapCatQuickLoginAccount(uin=value, nick_name=nick_name))

        with self._quick_login_candidates_lock:
            cached_candidates = list(self._quick_login_candidates_cache)
            cache_fresh = self._quick_login_candidates_cache_is_fresh()
        for uin, nick_name in cached_candidates:
            _append(uin, nick_name)
        _append(self._settings.quick_login_uin)
        if not cache_fresh:
            self._kickoff_quick_login_candidates_prime_if_needed(announce=False)
        return results[:limit]

    def _quick_login_candidates_cache_is_fresh(self) -> bool:
        cached_at = self._quick_login_candidates_cached_at
        if cached_at is None:
            return False
        if monotonic() - cached_at <= self.QUICK_LOGIN_CACHE_TTL_S:
            return True
        self._quick_login_candidates_cached_at = None
        self._quick_login_candidates_cache.clear()
        return False

    def _kickoff_quick_login_candidates_prime_if_needed(self, *, announce: bool) -> None:
        with self._quick_login_candidates_lock:
            if self._quick_login_candidates_cache_is_fresh():
                return
            failed_at = self._quick_login_candidates_prime_failed_at
            if (
                failed_at is not None
                and monotonic() - failed_at <= self.QUICK_LOGIN_CACHE_RETRY_COOLDOWN_S
            ):
                return
            thread = self._quick_login_candidates_prime_thread
            if thread is not None and thread.is_alive():
                return
            if announce and not self._quick_login_cache_notice_shown:
                self._quick_login_cache_notice_shown = True
                self._console.print("startup_cache: 正在后台预加载 NapCat quick-login QQ 号补全，/login 会优先走 NapCat 候选缓存。")

            def _worker() -> None:
                try:
                    service = self._require_login_service()
                    normalized = self._collect_quick_login_candidates_from_service(service)
                    if normalized:
                        self._logger.info(
                            "quick_login_completion_cache_ready source=napcat candidates=%s",
                            ",".join(uin for uin, _nick in normalized),
                        )
                        with self._quick_login_candidates_lock:
                            self._quick_login_candidates_cache = normalized
                            self._quick_login_candidates_cached_at = monotonic()
                            self._quick_login_candidates_prime_failed_at = None
                        return
                    self._logger.info(
                        "quick_login_completion_cache_empty source=napcat"
                    )
                    with self._quick_login_candidates_lock:
                        self._quick_login_candidates_cache.clear()
                        self._quick_login_candidates_cached_at = None
                        self._quick_login_candidates_prime_failed_at = monotonic()
                except Exception as exc:
                    self._logger.info(
                        "quick_login_completion_cache_prime_failed error=%s",
                        exc,
                    )
                    with self._quick_login_candidates_lock:
                        self._quick_login_candidates_cache.clear()
                        self._quick_login_candidates_cached_at = None
                        self._quick_login_candidates_prime_failed_at = monotonic()
                finally:
                    with self._quick_login_candidates_lock:
                        self._quick_login_candidates_prime_thread = None

            self._quick_login_candidates_prime_thread = threading.Thread(
                target=_worker,
                name="quick-login-completion-prime",
                daemon=True,
            )
            self._quick_login_candidates_prime_thread.start()

    def _wait_briefly_for_quick_login_candidates_prime(self) -> None:
        thread = self._quick_login_candidates_prime_thread
        if thread is None or not thread.is_alive():
            return
        thread.join(timeout=self.QUICK_LOGIN_STARTUP_PRIME_WAIT_S)

    def _warm_napcat_service_for_startup(self) -> None:
        self._console.print("startup_napcat: 正在预热 NapCatQQ Service...")
        self._logger.info(
            "startup_napcat_warm_begin quick_login_uin=%s",
            self._settings.quick_login_uin or "",
        )
        try:
            result = self._require_bootstrapper().ensure_endpoint(
                "webui",
                timeout_seconds=8.0,
                poll_interval=0.5,
                quick_login_uin=self._settings.quick_login_uin,
            )
        except Exception as exc:
            self._logger.info("startup_napcat_warm_failed error=%s", exc)
            self._console.print(
                f"startup_napcat: WebUI 预热失败，但 CLI 仍可继续使用。detail={exc}"
            )
            return
        if result.ready:
            self._logger.info(
                "startup_napcat_warm_ready attempted_start=%s attempted_configure=%s already_running=%s",
                result.attempted_start,
                result.attempted_configure,
                result.already_running,
            )
            self._console.print(
                f"startup_napcat: {result.message or 'NapCatQQ Service ready.'}"
            )
            return
        self._logger.info(
            "startup_napcat_warm_not_ready message=%s",
            result.message,
        )
        self._console.print(
            f"startup_napcat: {result.message or 'NapCatQQ Service 未就绪，后续可继续用 /login 排查。'}"
        )

    def _handle_buffer_text_changed(self, buffer) -> None:
        if not _should_auto_refresh_completion(buffer.text):
            return
        try:
            if buffer.complete_state is not None:
                buffer.cancel_completion()
            buffer.start_completion(select_first=False)
        except Exception:
            self._logger.debug("completion_auto_refresh_failed", exc_info=True)

    def _collect_quick_login_candidates_from_service(
        self,
        service: NapCatQrLoginService,
    ) -> list[tuple[str, str | None]]:
        normalized: list[tuple[str, str | None]] = []
        seen: set[str] = set()

        def _append(uin: str | None, nick_name: str | None = None) -> None:
            value = str(uin or "").strip()
            if not value or value in seen:
                return
            seen.add(value)
            normalized.append((value, str(nick_name or "").strip() or None))

        try:
            candidates = service.get_quick_login_candidates()
        except Exception as exc:
            self._logger.debug(
                "quick_login_completion_candidates_fetch_failed error=%s",
                exc,
            )
            candidates = []
        for candidate in candidates:
            _append(candidate.uin, candidate.nick_name)
        try:
            info = service.get_ready_login_info() or service.get_login_info()
        except Exception as exc:
            self._logger.debug(
                "quick_login_completion_login_info_fetch_failed error=%s",
                exc,
            )
            info = None
        if info is not None:
            _append(getattr(info, "uin", None), getattr(info, "nick", None))
        return normalized


class _RootExportProgressDisplay:
    def __init__(self, console: Console, *, target_label: str, batch_prefix: str | None) -> None:
        self._console = console
        self._target_label = target_label
        self._batch_prefix = batch_prefix or ""
        self._live: Live | None = None
        self._progress_line = "Preparing export..."
        self._download_line = ""

    def start(self) -> None:
        if self._live is not None:
            return
        self._live = Live(
            self._renderable(),
            console=self._console,
            refresh_per_second=8,
            transient=True,
            auto_refresh=False,
        )
        self._live.start()
        self._live.refresh()

    def update_progress(self, text: str) -> None:
        self._progress_line = text
        if self._live is None:
            self.start()
            return
        self._live.update(self._renderable(), refresh=True)

    def update_download_progress(self, text: str) -> None:
        self._download_line = text
        if self._live is None:
            self.start()
            return
        self._live.update(self._renderable(), refresh=True)

    def stop(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None
        self._download_line = ""

    def _renderable(self) -> Panel:
        header = self._target_label
        if self._batch_prefix:
            header = f"{self._batch_prefix} {header}"
        lines: list[Text] = [build_rich_status_text(header), build_rich_status_text(self._progress_line)]
        if self._download_line:
            lines.append(build_rich_status_text(self._download_line))
        return Panel(
            Group(*lines),
            title="Export Progress",
            border_style="cyan",
        )


def _parse_options(
    argv: list[str],
    *,
    allowed_options: set[str] | None = None,
    command_name: str | None = None,
) -> tuple[list[str], dict[str, Any]]:
    positionals: list[str] = []
    options: dict[str, Any] = {}
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("--"):
            positionals.append(token)
            index += 1
            continue

        key = token[2:]
        option_name = key.split("=", 1)[0]
        if allowed_options is not None and option_name not in allowed_options:
            supported = ", ".join(f"--{name}" for name in sorted(allowed_options))
            prefix = f"{command_name} " if command_name else ""
            raise ValueError(
                f"{prefix}不支持参数 --{option_name}。支持的参数：{supported or '无'}"
            )
        if "=" in key:
            option_name, option_value = key.split("=", 1)
            options[option_name] = option_value
            index += 1
            continue

        next_value = argv[index + 1] if index + 1 < len(argv) else None
        if next_value is not None and not next_value.startswith("--"):
            options[key] = next_value
            index += 2
            continue

        options[key] = True
        index += 1

    return positionals, options


def _friendly_command_parse_error(exc: Exception) -> str:
    message = str(exc or "").strip()
    lowered = message.casefold()
    if "quotation" in lowered or "quote" in lowered:
        return (
            "命令里似乎有未闭合的引号。请补上引号，"
            "或改用 QQ 号 / 从补全列表选择目标后再回车。"
        )
    return f"命令格式无法解析：{message or '请检查输入'}。可输入 /help 查看示例。"


def _friendly_command_failure(exc: Exception) -> str:
    message = str(exc or "").strip() or exc.__class__.__name__
    lowered = message.casefold()
    if lowered.startswith("usage:"):
        usage = message[6:].strip() or "请检查命令参数"
        return f"命令参数不完整：{usage}。可输入 /help 查看示例。"
    if isinstance(exc, ValueError):
        return f"命令无法执行：{message}。程序仍可继续使用；可输入 /help 查看示例。"
    log_path = get_cli_log_path()
    if log_path:
        return f"命令执行失败：{message}。程序仍可继续使用；如需排查，请查看日志：{log_path}"
    return f"命令执行失败：{message}。程序仍可继续使用。"


def _friendly_watch_crash_message(exc: Exception) -> str:
    message = str(exc or "").strip() or exc.__class__.__name__
    log_path = get_cli_log_path()
    if log_path:
        return f"监视窗口意外关闭：{message}。程序仍可继续使用；如需排查，请查看日志：{log_path}"
    return f"监视窗口意外关闭：{message}。程序仍可继续使用。"


def _render_root_help_lines() -> list[str]:
    return [
        "常用命令：",
        "  /friends [关键词] [--refresh] [--limit N]",
        "  /groups [关键词] [--refresh] [--limit N]",
        "  /watch group|friend <名称或QQ号> [--refresh] [--limit N]",
        "  /export group|friend <名称或QQ号> [<time-a> <time-b>] [data_count=NN] [asTXT|asJSONL]",
        "  /export group_asBatch=<名称1,名称2,...> [<time-a> <time-b>] [data_count=NN]",
        "  /export friend_asBatch=<名称1,名称2,...> [<time-a> <time-b>] [data_count=NN]",
        "  /export_onlyText ...    /export_TextImage ...    /export_TextImageEmoji ...",
        "  /login [QQ号] [--refresh] [--timeout N] [--poll N] [--quick-uin QQ号]",
        "  /status    /doctor    /terminal-doctor    /fixture-export <fixture_json> <out_path> [jsonl|txt]    /quit",
        "",
        "默认行为：",
        "  - root /export 与 export-history 默认导出 jsonl",
        "  - 如需 txt，可在命令末尾加 asTXT，或使用 --format txt",
        "  - 名称里有空格时，请用引号包起来；也可直接输入 QQ 号",
        "  - 如果终端显示错位，可先运行 /terminal-doctor，再尝试用 --ui compat 重启 CLI",
        "",
        "示例：",
        "  /watch friend 1507833383",
        "  /watch group \"蕾米二次元萌萌群\"",
        "  /export friend 1507833383 asTXT",
        "  /export group \"蕾米二次元萌萌群\" @final_content @earliest_content data_count=2000",
        "  /export_onlyText friend \"paprika\" 2026-03-01_00-00-00 2026-03-15_00-00-00",
    ]


def _parse_int_option(options: dict[str, Any], key: str, *, default: int) -> int:
    value = options.get(key)
    if value in {None, False}:
        return default
    return int(str(value))


def _normalize_chat_type(value: str) -> str:
    lowered = value.lower()
    if lowered == "group":
        return "group"
    if lowered == "friend":
        return "private"
    raise ValueError("chat type must be group or friend")


def _build_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @Condition
    def can_roll_export_date() -> bool:
        app = get_app_or_none()
        if app is None:
            return False
        buffer = app.current_buffer
        if buffer.complete_state is not None:
            return False
        return roll_export_date_token(
            buffer.text,
            cursor_position=buffer.cursor_position,
            delta=0,
        ) is not None

    @bindings.add(" ")
    def _(event) -> None:
        buffer = event.app.current_buffer
        buffer.insert_text(" ")
        if _should_start_completion_on_space(buffer.text):
            buffer.start_completion(select_first=False)

    @bindings.add(",")
    def _(event) -> None:
        buffer = event.app.current_buffer
        buffer.insert_text(",")
        if _should_start_completion_on_comma(buffer.text):
            buffer.start_completion(select_first=False)

    @bindings.add("/")
    def _(event) -> None:
        buffer = event.app.current_buffer
        buffer.insert_text("/")
        if buffer.text == "/":
            buffer.start_completion(select_first=False)

    @bindings.add("tab")
    def _(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state:
            if _is_login_completion_context(buffer.text):
                _navigate_completion_menu_without_inserting(buffer, direction=1)
                return
            buffer.complete_next()
        else:
            buffer.start_completion(select_first=_should_select_first_completion(buffer.text))

    @bindings.add("down", filter=has_completions)
    def _(event) -> None:
        buffer = event.app.current_buffer
        if _is_login_completion_context(buffer.text):
            _navigate_completion_menu_without_inserting(buffer, direction=1)
            return
        buffer.complete_next()

    @bindings.add("down", filter=~has_completions & can_roll_export_date)
    def _(event) -> None:
        _roll_export_date_in_buffer(event.app.current_buffer, delta=-1)

    @bindings.add("up", filter=has_completions)
    def _(event) -> None:
        buffer = event.app.current_buffer
        if _is_login_completion_context(buffer.text):
            _navigate_completion_menu_without_inserting(buffer, direction=-1)
            return
        buffer.complete_previous()

    @bindings.add("up", filter=~has_completions & can_roll_export_date)
    def _(event) -> None:
        _roll_export_date_in_buffer(event.app.current_buffer, delta=1)

    @bindings.add("escape", filter=has_completions)
    def _(event) -> None:
        event.app.current_buffer.cancel_completion()

    @bindings.add("left")
    def _(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state is not None:
            buffer.cancel_completion()
        target = move_export_date_cursor(
            buffer.text,
            cursor_position=buffer.cursor_position,
            direction="left",
        )
        if target is not None:
            buffer.cursor_position = target
            return
        buffer.cursor_left()

    @bindings.add("right")
    def _(event) -> None:
        buffer = event.app.current_buffer
        if buffer.complete_state is not None:
            buffer.cancel_completion()
        target = move_export_date_cursor(
            buffer.text,
            cursor_position=buffer.cursor_position,
            direction="right",
        )
        if target is not None:
            buffer.cursor_position = target
            return
        buffer.cursor_right()

    @bindings.add("enter", filter=has_completions)
    def _(event) -> None:
        buffer = event.app.current_buffer
        completion = _get_selected_completion(buffer)
        if completion is not None:
            if completion_application_is_noop(buffer, completion):
                buffer.cancel_completion()
                buffer.validate_and_handle()
                return
            _accept_completion(buffer, completion)
            _start_completion_followup(buffer, accepted_text=completion.text)
            return
        buffer.cancel_completion()
        buffer.validate_and_handle()

    @bindings.add("enter", filter=~has_completions)
    def _(event) -> None:
        buffer = event.app.current_buffer
        buffer.validate_and_handle()

    return bindings


def _get_selected_completion(buffer) -> Any | None:
    state = buffer.complete_state
    if state is None:
        return None
    if state.current_completion is not None:
        return state.current_completion
    completions = getattr(state, "completions", None) or []
    if completions:
        return completions[0]
    return None


def _accept_completion(buffer, completion) -> None:
    buffer.cancel_completion()
    if completion.start_position < 0:
        buffer.delete_before_cursor(count=-completion.start_position)
    buffer.insert_text(completion.text, fire_event=False)


def _start_completion_followup(buffer, *, accepted_text: str | None = None) -> None:
    followup = _completion_followup(buffer.text, accepted_text=accepted_text)
    if followup == "space_then_complete":
        buffer.insert_text(" ", fire_event=False)
        buffer.start_completion(select_first=False)
    elif followup == "same_token_complete":
        buffer.start_completion(select_first=False)
    elif followup == "cancel":
        buffer.cancel_completion()


def _completion_followup(text: str, *, accepted_text: str | None = None) -> str | None:
    accepted = (accepted_text or "").strip()
    accepted_normalized = accepted.casefold()
    if accepted_normalized in {"astxt", "asjsonl", "data_count="}:
        return "cancel"
    normalized = text.strip()
    if normalized in {"/watch", "/groups", "/friends"} or normalized.casefold() in EXPORT_COMMAND_PROFILES:
        return "space_then_complete"
    if normalized in {
        "/watch group",
        "/watch friend",
    } or normalized.casefold() in {
        f"{command} group"
        for command in EXPORT_COMMAND_PROFILES
    } | {
        f"{command} friend"
        for command in EXPORT_COMMAND_PROFILES
    }:
        return "space_then_complete"
    if accepted_normalized in {"group_asbatch=", "friend_asbatch="}:
        return "same_token_complete"
    tokens = _split_cli_tokens(normalized)
    if _needs_same_token_export_followup(tokens, watch_mode=False):
        return "same_token_complete"
    if len(tokens) == 2 and _is_batch_export_token(tokens[1]):
        return "cancel"
    if len(tokens) == 3 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and tokens[1] in {"group", "friend"}:
        return "space_then_complete"
    if len(tokens) == 4 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and tokens[1] in {"group", "friend"}:
        return "space_then_complete"
    if len(tokens) == 5 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and tokens[1] in {"group", "friend"}:
        return "space_then_complete"
    if len(tokens) in {2, 3, 4} and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and _is_batch_export_token(tokens[1]):
        return "space_then_complete"
    return "cancel"


def _is_login_completion_context(text: str) -> bool:
    tokens = _split_cli_tokens(text.rstrip())
    return bool(tokens) and tokens[0].casefold() == "/login"


def _should_auto_refresh_completion(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped.startswith("/"):
        return False
    if " " not in stripped:
        return True
    if _is_login_completion_context(stripped):
        return True
    return False


def _navigate_completion_menu_without_inserting(buffer, *, direction: int) -> None:
    state = buffer.complete_state
    if state is None:
        return
    completions = getattr(state, "completions", None) or []
    if not completions:
        return
    index = getattr(state, "complete_index", None)
    last_index = len(completions) - 1
    if direction >= 0:
        next_index = 0 if index is None else (None if index >= last_index else index + 1)
    else:
        next_index = last_index if index is None else (None if index <= 0 else index - 1)
    state.go_to_index(next_index)
    buffer.complete_state = state
    app = get_app_or_none()
    if app is not None:
        app.invalidate()


def _completion_menu_reserve_lines(ui_profile: Any, terminal_probe: TerminalProbe) -> int:
    mode = getattr(ui_profile, "mode", None)
    if mode == "compat":
        terminal_lines = max(0, int(getattr(terminal_probe, "lines", 0) or 0))
        dynamic_reserve = terminal_lines // 3 if terminal_lines else 0
        return min(12, max(8, dynamic_reserve))
    return 8


def _completion_style_for_ui_profile(ui_profile: Any) -> CompleteStyle:
    mode = getattr(ui_profile, "mode", None)
    if mode == "compat":
        return CompleteStyle.COLUMN
    return CompleteStyle.COLUMN


def _should_start_completion_on_space(text: str) -> bool:
    tokens = _split_cli_tokens(text)
    if not text.endswith(" "):
        return False
    if tokens in [
        ["/watch"],
        ["/groups"],
        ["/friends"],
        ["/watch", "group"],
        ["/watch", "friend"],
    ]:
        return True
    if not tokens or tokens[0].casefold() not in EXPORT_COMMAND_PROFILES:
        return False
    if len(tokens) == 1:
        return True
    if len(tokens) == 2 and tokens[1] in {"group", "friend"}:
        return True
    if len(tokens) == 2 and _is_batch_export_token(tokens[1]):
        return True
    if len(tokens) in {3, 4} and _is_batch_export_token(tokens[1]):
        return True
    return len(tokens) in {3, 4, 5} and tokens[1] in {"group", "friend"}


def _should_start_completion_on_comma(text: str) -> bool:
    tokens = _split_cli_tokens(text)
    return len(tokens) == 2 and bool(tokens) and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and _is_batch_export_token(tokens[1])


def _should_select_first_completion(text: str) -> bool:
    normalized = text.rstrip()
    tokens = _split_cli_tokens(normalized)
    if tokens and tokens[0].casefold() == "/login":
        return False
    if normalized in {"/watch", "/groups", "/friends"} or normalized.casefold() in EXPORT_COMMAND_PROFILES:
        return False
    if normalized in {
        "/watch group",
        "/watch friend",
    } or normalized.casefold() in {
        f"{command} group"
        for command in EXPORT_COMMAND_PROFILES
    } | {
        f"{command} friend"
        for command in EXPORT_COMMAND_PROFILES
    }:
        return False
    if len(tokens) >= 2 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and _is_batch_export_token(tokens[1]):
        return False
    if len(tokens) in {3, 4, 5} and tokens[0].casefold() in EXPORT_COMMAND_PROFILES and tokens[1] in {"group", "friend"}:
        return False
    return True


def _roll_export_date_in_buffer(buffer, *, delta: int) -> None:
    updated = roll_export_date_token(
        buffer.text,
        cursor_position=buffer.cursor_position,
        delta=delta,
    )
    if updated is None:
        return
    new_text, new_cursor = updated
    buffer.document = buffer.document.__class__(text=new_text, cursor_position=new_cursor)


def _split_cli_tokens(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _needs_same_token_export_followup(tokens: list[str], *, watch_mode: bool) -> bool:
    from qq_data_core import is_explicit_datetime_literal

    if not tokens or tokens[0].casefold() not in EXPORT_COMMAND_PROFILES:
        return False
    if watch_mode:
        export_tokens = _strip_export_format_alias(tokens[1:])
    else:
        if len(tokens) < 2:
            return False
        if tokens[1] in {"group", "friend"}:
            if len(tokens) < 3:
                return False
            export_tokens = _strip_export_format_alias(tokens[3:])
        elif _is_batch_export_token(tokens[1]):
            export_tokens = _strip_export_format_alias(tokens[2:])
        else:
            return False
    if not export_tokens:
        return False
    return is_explicit_datetime_literal(export_tokens[-1]) and export_tokens[-1].endswith("_00-00-00")


def _is_batch_export_token(token: str) -> bool:
    lowered = token.casefold()
    return lowered.startswith("group_asbatch=") or lowered.startswith("friend_asbatch=")


def _strip_export_format_alias(tokens: list[str]) -> list[str]:
    if tokens and tokens[-1].casefold() in {"astxt", "asjsonl"}:
        return tokens[:-1]
    return tokens
