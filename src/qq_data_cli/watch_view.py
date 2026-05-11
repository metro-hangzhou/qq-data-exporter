from __future__ import annotations

import asyncio
import shlex
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions, to_filter
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import Margin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output.base import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from qq_data_cli.completion import WatchCommandCompleter
from qq_data_cli.completion_runtime import completion_application_is_noop
from qq_data_cli.export_commands import (
    EXPORT_COMMAND_PROFILES,
    interval_is_full_history,
    interval_needs_history_bounds,
    interval_special_kinds,
    parse_watch_export_command,
    resolve_interval,
)
from qq_data_cli.export_cleanup import cleanup_gateway_media_cache
from qq_data_cli.export_input import (
    ExportCommandLexer,
    ExportDateDisplayProcessor,
    move_export_date_cursor,
    roll_export_date_token,
)
from qq_data_cli.logging_utils import get_cli_log_path, get_cli_logger
from qq_data_cli.status_display import colorize_status_fields_for_ansi, format_prefetch_media_progress_line
from qq_data_cli.target_display import (
    format_display_name,
    format_target_name,
    format_target_remark,
    terminal_safe_text,
)
from qq_data_cli.terminal_compat import CliUiProfile
from qq_data_core import (
    apply_export_profile,
    build_export_content_summary,
    ChatExportService,
    ExportPerfTraceWriter,
    ExportBundleResult,
    ExportRequest,
    format_export_content_summary,
    NormalizedMessage,
    WatchRequest,
    build_default_output_path,
    format_export_datetime,
    is_explicit_datetime_literal,
    normalize_message,
    render_debug_content,
    trim_snapshot_to_last_messages,
)
from qq_data_core.models import EXPORT_TIMEZONE
from qq_data_integrations.napcat import ChatTarget, NapCatGateway, NapCatSettings


@dataclass(slots=True)
class WatchTimelineEntry:
    sort_key: tuple[int, str, str]
    text: str
    dedupe_key: str


class FixedThumbScrollbarMargin(Margin):
    def __init__(self, *, thumb_height: int = 4) -> None:
        self._thumb_height = max(1, thumb_height)

    def get_width(self, get_ui_content) -> int:
        return 1

    def create_margin(self, window_render_info, width: int, height: int) -> list[tuple[str, str]]:
        if height <= 0:
            return []

        content_height = max(1, window_render_info.content_height)
        window_height = max(1, window_render_info.window_height)
        max_scroll = max(0, content_height - window_height)
        thumb_height = min(height, self._thumb_height)
        thumb_top = 0
        if max_scroll > 0 and height > thumb_height:
            fraction = window_render_info.vertical_scroll / float(max_scroll)
            thumb_top = int(round((height - thumb_height) * fraction))

        result: list[tuple[str, str]] = []
        for row in range(height):
            in_thumb = thumb_top <= row < thumb_top + thumb_height
            if in_thumb:
                style = "class:scrollbar.button"
                if row == thumb_top + thumb_height - 1:
                    style = "class:scrollbar.button,scrollbar.end"
            else:
                style = "class:scrollbar.background"
                if row == thumb_top - 1:
                    style = "class:scrollbar.background,scrollbar.start"
            result.append((style, " "))
            if row < height - 1:
                result.append(("", "\n"))
        return result


def _default_watch_help_text() -> str:
    return (
        "scroll PgUp/PgDn/Up/Down/Home/End  "
        "/export* [time-a time-b] [data_count=NN] [asTXT|asJSONL]  /exit"
    )


def _wrap_terminal_text(text: str, *, width: int) -> list[str]:
    if width <= 1:
        return [""]
    wrapped_lines: list[str] = []
    for raw_line in str(text or "").splitlines() or [""]:
        current = 0
        result: list[str] = []
        emitted = False
        for char in raw_line:
            char_width = max(1, get_cwidth(char))
            if current + char_width > width and result:
                wrapped_lines.append("".join(result))
                result = [char]
                current = char_width
                emitted = True
                continue
            result.append(char)
            current += char_width
        if result or not emitted:
            wrapped_lines.append("".join(result))
    return wrapped_lines or [""]


def _watch_content_width(*, terminal_columns: int, reserve_scrollbar: bool) -> int:
    width = max(20, int(terminal_columns))
    if reserve_scrollbar:
        width = max(4, width - 1)
    return width


class WatchConversationView:
    def __init__(
        self,
        *,
        settings: NapCatSettings,
        gateway: NapCatGateway,
        service: ChatExportService,
        target: ChatTarget,
        request: WatchRequest,
        history_limit: int = 80,
        ui_profile: CliUiProfile | None = None,
        application_input: Input | None = None,
        application_output: Output | None = None,
        live_events_enabled: bool = True,
        initial_notice_text: str = "",
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        self._service = service
        self._target = target
        self._request = request
        self._ui_profile = ui_profile
        self._history_limit = max(1, history_limit)
        self._entries: list[WatchTimelineEntry] = []
        self._seen_keys: set[str] = set()
        self._status_text = "Loading history..."
        self._notice_text = str(initial_notice_text or "")
        self._help_text = _default_watch_help_text()
        self._live_events_enabled = bool(live_events_enabled)
        self._follow_tail = True
        self._scroll_top = 0
        self._history_page_size = max(20, self._history_limit)
        self._oldest_history_anchor: str | None = None
        self._history_exhausted = False
        self._download_notice_text = ""
        self._reserve_timeline_scrollbar_column = True
        self._last_timeline_content_width: int | None = None
        self._message_area = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            line_numbers=False,
            wrap_lines=False,
            focusable=False,
        )
        self._message_area.window.get_vertical_scroll = lambda window: self._scroll_top
        self._message_area.window.always_hide_cursor = to_filter(True)
        if self._ui_profile is None or self._ui_profile.use_custom_scrollbar:
            self._message_area.window.right_margins = [FixedThumbScrollbarMargin()]
        self._command_input = TextArea(
            height=1,
            prompt="watch> ",
            multiline=False,
            wrap_lines=False,
            completer=WatchCommandCompleter(),
            complete_while_typing=(
                True if self._ui_profile is None else self._ui_profile.complete_while_typing
            ),
            lexer=ExportCommandLexer(),
            input_processors=[ExportDateDisplayProcessor()],
            accept_handler=self._accept_command,
        )
        floats = []
        if self._ui_profile is None or self._ui_profile.show_completion_menu:
            floats = [
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=8, scroll_offset=1),
                )
            ]
        app_style = (
            Style.from_dict({"export-date-literal": "bg:#ffffff #000000"})
            if self._ui_profile is None or self._ui_profile.use_highlight_style
            else None
        )
        self._app = Application(
            layout=Layout(
                FloatContainer(
                    content=HSplit(
                        [
                            Window(
                                content=FormattedTextControl(self._get_header_text),
                                height=1,
                            ),
                            Window(
                                content=FormattedTextControl(self._get_status_line),
                                height=self._get_status_height,
                            ),
                            self._message_area,
                            Window(
                                content=FormattedTextControl(self._get_help_line),
                                height=self._get_help_height,
                            ),
                            self._command_input,
                        ]
                    ),
                    floats=floats,
                ),
                focused_element=self._command_input,
            ),
            key_bindings=self._build_key_bindings(),
            full_screen=True if self._ui_profile is None else self._ui_profile.watch_full_screen,
            mouse_support=False,
            style=app_style,
            input=application_input,
            output=application_output,
            before_render=self._before_render,
        )
        self._pump_task: asyncio.Task[None] | None = None
        self._export_task: asyncio.Task[None] | None = None
        self._history_load_task: asyncio.Task[None] | None = None
        self._export_started_monotonic: float | None = None
        self._export_perf_trace: ExportPerfTraceWriter | None = None
        self._last_export_progress_emit = 0.0
        self._last_export_progress_current = -1
        self._logger = get_cli_logger("watch")
        self._exit_reason = "unknown"

    async def run(self) -> None:
        self._load_initial_history()
        self._refresh_message_area()
        self._refresh_view_state()
        self._status_text = self._build_status_text()
        self._invalidate()
        self._logger.info(
            "watch_run_start chat_type=%s chat_id=%s chat_name=%s history_limit=%s log_path=%s",
            self._request.chat_type,
            self._request.chat_id,
            self._request.chat_name,
            self._history_limit,
            get_cli_log_path() or "",
        )
        try:
            await self._app.run_async(pre_run=self._start_background_tasks)
            if self._exit_reason == "unknown":
                self._exit_reason = "application_return"
        finally:
            self._logger.info(
                "watch_run_end chat_type=%s chat_id=%s reason=%s",
                self._request.chat_type,
                self._request.chat_id,
                self._exit_reason,
            )
            await self._stop_background_tasks()

    def _load_initial_history(self) -> None:
        snapshot = self._gateway.fetch_snapshot(
            ExportRequest(
                chat_type=self._request.chat_type,
                chat_id=self._request.chat_id,
                chat_name=self._request.chat_name,
                limit=self._history_limit,
            )
        )
        self._ingest_snapshot(snapshot)
        self._history_exhausted = not snapshot.messages or not self._oldest_history_anchor

    def _start_background_tasks(self) -> None:
        if not self._live_events_enabled:
            self._pump_task = None
            return
        self._pump_task = self._app.create_background_task(self._pump_live_events())

    async def _stop_background_tasks(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._pump_task
        if self._history_load_task is not None:
            if not self._history_load_task.done():
                self._logger.info(
                    "watch_history_load_waiting_for_completion_on_shutdown chat_id=%s",
                    self._request.chat_id,
                )
            with suppress(asyncio.CancelledError):
                await self._history_load_task
        if self._export_task is not None:
            if not self._export_task.done():
                self._logger.info(
                    "watch_export_waiting_for_completion_on_shutdown chat_id=%s",
                    self._request.chat_id,
                )
            with suppress(asyncio.CancelledError):
                await self._export_task

    async def _pump_live_events(self) -> None:
        try:
            async for event in self._gateway.watch(self._request):
                entry = _build_watch_entry(
                    event,
                    chat_type=self._request.chat_type,
                    chat_id=self._request.chat_id,
                    chat_name=self._request.chat_name,
                )
                self._append_entry(entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._logger.exception("watch_live_events_failed chat_id=%s", self._request.chat_id)
            self._notice_text = _friendly_watch_runtime_notice(
                "实时监听暂时出错",
                exc,
                suffix="当前窗口仍可继续使用，可稍后重试。",
            )
            self._invalidate()

    def _append_entry(self, entry: WatchTimelineEntry, *, invalidate: bool = True) -> bool:
        if entry.dedupe_key in self._seen_keys:
            return False
        self._seen_keys.add(entry.dedupe_key)
        self._entries.append(entry)
        self._entries.sort(key=lambda item: item.sort_key)
        if invalidate:
            self._status_text = self._build_status_text()
            self._refresh_message_area()
            self._refresh_view_state()
            self._invalidate()
        return True

    def _ingest_snapshot(self, snapshot) -> int:
        normalized = self._service.build_snapshot(snapshot)
        messages = sorted(
            normalized.messages,
            key=lambda item: (item.timestamp_ms, item.message_seq or "", item.message_id or ""),
        )
        added = 0
        for message in messages:
            if self._append_entry(_build_message_entry(message), invalidate=False):
                added += 1
        if messages:
            self._oldest_history_anchor = _message_anchor(messages[0]) or self._oldest_history_anchor
        return added

    def _refresh_view_state(self) -> None:
        if self._follow_tail:
            self._scroll_to_end()
        else:
            self._clamp_scroll_top()
        self._sync_cursor_to_view()

    def _refresh_message_area(self) -> None:
        self._last_timeline_content_width = self._timeline_content_width()
        transcript = self._get_timeline_text()
        self._message_area.buffer.set_document(
            Document(text=transcript, cursor_position=0),
            bypass_readonly=True,
        )
        self._clamp_scroll_top()
        self._sync_cursor_to_view()

    def _refresh_message_area_for_resize(self) -> None:
        width = self._timeline_content_width()
        if self._last_timeline_content_width == width:
            return

        previous_follow_tail = self._follow_tail
        previous_scroll_top = self._scroll_top
        self._refresh_message_area()
        if not previous_follow_tail:
            self._follow_tail = False
            self._scroll_top = previous_scroll_top
            self._clamp_scroll_top()
            self._sync_cursor_to_view()

    def _before_render(self, _app: Application[Any]) -> None:
        self._refresh_message_area_for_resize()

    def _scroll_to_end(self) -> None:
        line_count = max(1, self._message_area.buffer.document.line_count)
        self._scroll_top = max(0, line_count - self._visible_window_height())
        self._sync_cursor_to_view()

    def _scroll_relative(self, delta: int) -> None:
        previous_scroll_top = self._scroll_top
        if delta < 0 and previous_scroll_top + delta < 0:
            self._queue_load_older_history(previous_scroll_top=previous_scroll_top, requested_delta=delta)
            return

        line_count = max(1, self._message_area.buffer.document.line_count)
        max_scroll = max(0, line_count - self._visible_window_height())
        requested_scroll = previous_scroll_top + delta
        self._scroll_top = min(max_scroll, max(0, requested_scroll))
        self._follow_tail = self._scroll_top >= max_scroll
        self._sync_cursor_to_view()
        self._status_text = self._build_status_text()
        self._invalidate()

    def _queue_load_older_history(self, *, previous_scroll_top: int, requested_delta: int) -> None:
        if self._history_exhausted:
            self._notice_text = "更早历史已经全部加载完了。"
            self._invalidate()
            return
        if self._history_load_task is not None and not self._history_load_task.done():
            self._notice_text = "正在加载更早历史，请稍等..."
            self._invalidate()
            return
        self._notice_text = "正在加载更早历史..."
        self._invalidate()
        self._history_load_task = self._app.create_background_task(
            self._load_older_history_async(
                previous_scroll_top=previous_scroll_top,
                requested_delta=requested_delta,
            )
        )

    async def _load_older_history_async(self, *, previous_scroll_top: int, requested_delta: int) -> None:
        try:
            previous_anchor = self._oldest_history_anchor
            if not previous_anchor:
                self._history_exhausted = True
                self._status_text = self._build_status_text()
                self._invalidate()
                return
            snapshot = await asyncio.to_thread(self._fetch_older_history_before, previous_anchor)
            added = self._ingest_snapshot(snapshot)
            if added <= 0 or self._oldest_history_anchor == previous_anchor:
                self._history_exhausted = True
            if added > 0:
                self._refresh_message_area()
                line_count = max(1, self._message_area.buffer.document.line_count)
                max_scroll = max(0, line_count - self._visible_window_height())
                requested_scroll = previous_scroll_top + requested_delta + added
                self._scroll_top = min(max_scroll, max(0, requested_scroll))
                self._follow_tail = self._scroll_top >= max_scroll
                self._sync_cursor_to_view()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._notice_text = _friendly_watch_runtime_notice(
                "加载更早历史失败",
                exc,
                suffix="当前窗口仍可继续使用，可稍后再试。",
            )
        finally:
            self._status_text = self._build_status_text()
            self._invalidate()
            self._history_load_task = None

    def _fetch_older_history_before(self, previous_anchor: str):
        history_gateway = NapCatGateway(self._settings)
        try:
            return history_gateway.fetch_history_before(
                ExportRequest(
                    chat_type=self._request.chat_type,
                    chat_id=self._request.chat_id,
                    chat_name=self._request.chat_name,
                    limit=self._history_page_size,
                ),
                before_message_seq=previous_anchor,
                count=self._history_page_size,
            )
        finally:
            history_gateway.close()

    def _page_delta(self) -> int:
        render_info = self._message_area.window.render_info
        if render_info is None:
            return 10
        return max(5, render_info.window_height - 2)

    def _accept_command(self, buffer) -> bool:
        raw = buffer.text.strip()
        buffer.text = ""
        if not raw:
            return False
        self._handle_command(raw)
        return False

    def _handle_command(self, raw: str) -> None:
        if not raw.startswith("/"):
            self._notice_text = "当前窗口只接受 /export*、/help 和 /exit。"
            self._invalidate()
            return

        try:
            argv = shlex.split(raw)
        except ValueError as exc:
            self._notice_text = _friendly_watch_parse_error(exc)
            self._invalidate()
            return
        if not argv:
            return
        command = argv[0].lower()
        if command == "/exit":
            if self._export_task is not None and not self._export_task.done():
                self._notice_text = "导出仍在运行，当前窗口会在导出完成后再退出。"
                self._invalidate()
                return
            self._exit_reason = "command_exit"
            self._app.exit()
            return
        if command == "/help":
            self._notice_text = (
                f"默认导出目录：{self._settings.export_dir}；默认格式：jsonl；"
                "如需 txt，可用 asTXT 或 --format txt。"
            )
            self._invalidate()
            return
        if command in EXPORT_COMMAND_PROFILES:
            self._handle_export(command, argv[1:])
            return

        self._notice_text = f"未识别的命令：{command}。当前窗口可用 /export*、/help、/exit。"
        self._invalidate()

    def _handle_export(self, command: str, argv: list[str]) -> None:
        positionals, options = _parse_options(
            argv,
            allowed_options={
                "format",
                "out",
                "limit",
                "data-count",
                "include-raw",
                "strict-missing",
            },
            command_name=command,
        )
        parsed = parse_watch_export_command(command, positionals, options, default_limit=max(200, self._history_limit))
        if parsed.fmt not in {"jsonl", "txt"}:
            self._notice_text = "当前窗口导出仅支持 txt/jsonl；可用 asTXT、asJSONL 或 --format 指定。"
            self._invalidate()
            return

        if self._export_task is not None and not self._export_task.done():
            self._notice_text = "已有导出任务在运行，请等当前任务完成后再试。"
            self._invalidate()
            return

        self._notice_text = f"导出已开始，目标目录：{self._settings.export_dir}"
        self._export_started_monotonic = time.monotonic()
        self._last_export_progress_emit = 0.0
        self._last_export_progress_current = -1
        self._invalidate()
        self._logger.info(
            "watch_export_queued chat_id=%s profile=%s fmt=%s data_count=%s interval=%s out_path=%s",
            self._request.chat_id,
            parsed.profile,
            parsed.fmt,
            parsed.data_count,
            parsed.interval,
            parsed.out_path,
        )
        self._export_task = self._app.create_background_task(self._run_export(parsed))

    async def _run_export(self, parsed) -> None:
        self._logger.info(
            "watch_export_start chat_id=%s profile=%s fmt=%s out_path=%s",
            self._request.chat_id,
            parsed.profile,
            parsed.fmt,
            parsed.out_path,
        )
        loop = asyncio.get_running_loop()
        self._export_perf_trace = ExportPerfTraceWriter(
            self._settings.state_dir,
            chat_type=self._request.chat_type,
            chat_id=self._request.chat_id,
            mode="watch_export",
        )
        self._export_perf_trace.write_event(
            "export_start",
            {
                "chat_name": self._request.chat_name,
                "format": parsed.fmt,
                "limit": parsed.limit,
                "data_count": parsed.data_count,
                "profile": parsed.profile,
                "include_raw": parsed.include_raw,
                "target_dir": str(self._settings.export_dir),
            },
        )

        def progress_callback(update: dict[str, Any]) -> None:
            if self._export_perf_trace is not None:
                self._export_perf_trace.write_event(str(update.get("phase") or "progress"), update)
            loop.call_soon_threadsafe(self._apply_export_progress, update)

        try:
            bundle, record_count, content_summary, cleanup_stats = await asyncio.to_thread(
                self._execute_export_sync,
                parsed,
                progress_callback,
            )
        except asyncio.CancelledError:
            self._logger.info("watch_export_cancelled chat_id=%s", self._request.chat_id)
            if self._export_perf_trace is not None:
                self._export_perf_trace.write_event("export_cancelled", {})
                self._export_perf_trace.close()
                self._export_perf_trace = None
            self._export_started_monotonic = None
            raise
        except Exception as exc:
            self._logger.exception("watch_export_failed chat_id=%s", self._request.chat_id)
            if self._export_perf_trace is not None:
                self._export_perf_trace.write_event(
                    "export_failed",
                    {
                        "error": str(exc),
                    },
                )
            self._notice_text = _friendly_watch_runtime_notice(
                "导出失败",
                exc,
                suffix="监视窗口仍可继续使用；如需排查，请查看日志。",
            )
            self._invalidate()
            if self._export_perf_trace is not None:
                self._export_perf_trace.close()
                self._export_perf_trace = None
            self._export_started_monotonic = None
            return

        summary = (
            self._export_perf_trace.build_summary(record_count=record_count)
            if self._export_perf_trace is not None
            else {}
        )
        if self._export_perf_trace is not None:
            self._export_perf_trace.write_event(
                "export_cleanup_remote_cache",
                cleanup_stats,
            )
            self._export_perf_trace.write_event(
                "export_complete",
                {
                    "out_path": str(bundle.data_path),
                    "manifest_path": str(bundle.manifest_path),
                    "copied_asset_count": bundle.copied_asset_count,
                    "reused_asset_count": bundle.reused_asset_count,
                    "missing_asset_count": bundle.missing_asset_count,
                    "remote_cache_cleanup": cleanup_stats,
                    "content_summary": content_summary,
                    **summary,
                },
            )
            self._export_perf_trace.close()
            report_path = self._export_perf_trace.persist_report(record_count=record_count)
            trace_path = self._export_perf_trace.path
            self._export_perf_trace = None
        else:
            trace_path = None
            report_path = None
        elapsed_s = float(summary.get("elapsed_s") or 0.0)
        pages_scanned = int(summary.get("pages_scanned") or 0)
        retry_events = int(summary.get("retry_events") or 0)
        suffix = f" in {elapsed_s:.1f}s pages={pages_scanned} retries={retry_events}"
        if trace_path is not None:
            suffix += f" trace={trace_path}"
        if report_path is not None:
            suffix += f" report={report_path}"
        path_name = bundle.data_path.name
        perf_parts = [f"{elapsed_s:.1f}s"]
        if pages_scanned > 0:
            perf_parts.append(f"pages={pages_scanned}")
        if retry_events > 0:
            perf_parts.append(f"retries={retry_events}")
        self._notice_text = f"Exported {record_count} -> {path_name} | {' '.join(perf_parts)}"
        self._help_text = "\n".join(format_export_content_summary(content_summary))
        self._export_started_monotonic = None
        self._logger.info(
            "watch_export_succeeded chat_id=%s records=%s data_path=%s manifest=%s missing_assets=%s",
            self._request.chat_id,
            record_count,
            bundle.data_path,
            bundle.manifest_path,
            bundle.missing_asset_count,
        )
        self._invalidate()

    def _apply_export_progress(self, update: dict[str, Any]) -> None:
        phase = str(update.get("phase") or "")
        now = time.monotonic()
        if phase == "download_assets":
            self._download_notice_text = self._format_watch_download_progress(update)
            self._invalidate()
            return
        if phase == "materialize_asset_substep" and str(update.get("stage") or "") == "done":
            status = str(update.get("status") or "")
            if status in {"timeout", "unavailable", "storm_skip"}:
                substep = str(update.get("substep") or "-")
                asset_type = str(update.get("asset_type") or "-")
                file_name = str(update.get("file_name") or "").strip() or "-"
                timeout_s = float(update.get("timeout_s") or 0.0)
                elapsed = float(update.get("elapsed_s") or 0.0)
                detail = (
                    f"Export asset substep {status}... substep={substep} "
                    f"asset={asset_type}:{file_name}"
                )
                if timeout_s > 0:
                    detail += f" timeout={timeout_s:.1f}s"
                if elapsed > 0:
                    detail += f" elapsed={elapsed:.1f}s"
                detail += " continuing=1"
                self._notice_text = detail
                self._invalidate()
                return
        if phase == "materialize_assets":
            current = int(update.get("current") or 0)
            total = int(update.get("total") or 0)
            if (
                current < total
                and current > self._last_export_progress_current
                and current - self._last_export_progress_current < 8
                and now - self._last_export_progress_emit < 0.25
            ):
                return
            self._last_export_progress_current = current
            self._last_export_progress_emit = now
        pages_scanned = int(update.get("pages_scanned") or 0)
        elapsed_s = 0.0
        if self._export_started_monotonic is not None:
            elapsed_s = max(0.0, time.monotonic() - self._export_started_monotonic)
        rate_suffix = ""
        if phase in {"interval_scan", "interval_tail_scan", "full_scan", "tail_scan"} and elapsed_s > 0:
            record_count = int(update.get("collected_messages") or update.get("matched_messages") or 0)
            if record_count > 0:
                rate_suffix = f" rate={record_count / elapsed_s:.1f}/s elapsed={elapsed_s:.1f}s"
        if phase == "page_retry":
            requested_count = int(update.get("requested_count") or 0)
            next_page_size = int(update.get("next_page_size") or 0)
            reason = str(update.get("reason") or "retry")
            mode = str(update.get("mode") or "history")
            self._notice_text = (
                f"Export retrying slow page... mode={mode} reason={reason} "
                f"page_size={requested_count}->{next_page_size}"
            )
            self._invalidate()
            return
        if phase == "bounds_scan":
            earliest = update.get("earliest_content_at")
            final = update.get("final_content_at")
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            if earliest is not None and final is None:
                self._notice_text = (
                    f"Resolving @earliest_content... pages={pages_scanned} "
                    f"earliest={format_export_datetime(earliest)} page={page_duration_s:.2f}s elapsed={elapsed_s:.1f}s"
                )
            elif final is not None and earliest is None:
                self._notice_text = (
                    f"Resolving @final_content... pages={pages_scanned} "
                    f"final={format_export_datetime(final)} page={page_duration_s:.2f}s elapsed={elapsed_s:.1f}s"
                )
            else:
                self._notice_text = (
                    f"Resolving history bounds... pages={pages_scanned} "
                    f"earliest={format_export_datetime(earliest)} "
                    f"final={format_export_datetime(final)} "
                    f"page={page_duration_s:.2f}s elapsed={elapsed_s:.1f}s"
                )
        elif phase == "interval_scan":
            oldest = update.get("oldest_content_at")
            newest = update.get("newest_content_at")
            matched_messages = int(update.get("matched_messages") or 0)
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            page_size = int(update.get("page_size") or 0)
            detail = (
                f"pages={pages_scanned} matched={matched_messages} "
                f"page_size={page_size} page={page_duration_s:.2f}s{rate_suffix}"
            )
            if oldest is not None and newest is not None:
                detail += (
                    f" page_window={format_export_datetime(oldest)}.."
                    f"{format_export_datetime(newest)}"
                )
            self._notice_text = f"Export scanning interval... {detail}"
        elif phase == "interval_tail_scan":
            oldest = update.get("oldest_content_at")
            newest = update.get("newest_content_at")
            matched_messages = int(update.get("matched_messages") or 0)
            requested_data_count = int(update.get("requested_data_count") or 0)
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            page_size = int(update.get("page_size") or 0)
            detail = (
                f"pages={pages_scanned} matched={matched_messages}/{requested_data_count} "
                f"page_size={page_size} page={page_duration_s:.2f}s{rate_suffix}"
            )
            if oldest is not None and newest is not None:
                detail += (
                    f" page_window={format_export_datetime(oldest)}.."
                    f"{format_export_datetime(newest)}"
                )
            self._notice_text = f"Export scanning interval tail... {detail}"
        elif phase == "tail_scan":
            oldest = update.get("oldest_content_at")
            newest = update.get("newest_content_at")
            matched_messages = int(update.get("matched_messages") or 0)
            requested_data_count = int(update.get("requested_data_count") or 0)
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            page_size = int(update.get("page_size") or 0)
            detail = (
                f"pages={pages_scanned} matched={matched_messages}/{requested_data_count} "
                f"page_size={page_size} page={page_duration_s:.2f}s{rate_suffix}"
            )
            if oldest is not None and newest is not None:
                detail += (
                    f" page_window={format_export_datetime(oldest)}.."
                    f"{format_export_datetime(newest)}"
                )
            self._notice_text = f"Export scanning recent tail... {detail}"
        elif phase == "full_scan":
            earliest = update.get("earliest_content_at")
            collected_messages = int(update.get("collected_messages") or 0)
            page_duration_s = float(update.get("page_duration_s") or 0.0)
            page_size = int(update.get("page_size") or 0)
            detail = (
                f"pages={pages_scanned} collected={collected_messages} "
                f"page_size={page_size} page={page_duration_s:.2f}s{rate_suffix}"
            )
            if earliest is not None:
                detail += f" earliest={format_export_datetime(earliest)}"
            self._notice_text = f"Export scanning full history... {detail}"
        elif phase == "forward_expand":
            processed = int(update.get("processed_forwards") or 0)
            total = int(update.get("total_forwards") or 0)
            resolved = int(update.get("resolved_forwards") or 0)
            detail = f"{processed}/{total} resolved={resolved}"
            if elapsed_s > 0 and processed > 0:
                detail += f" rate={processed / elapsed_s:.1f}/s elapsed={elapsed_s:.1f}s"
            self._notice_text = f"Export expanding forwarded detail... {detail}"
        elif phase == "write_data_file":
            stage = str(update.get("stage") or "start")
            record_count = int(update.get("record_count") or 0)
            if stage == "done":
                self._notice_text = f"Export wrote data file... records={record_count}"
            else:
                self._notice_text = f"Export writing data file... records={record_count}"
        elif phase in {"prefetch_media", "prefetch_media_prepare", "prefetch_media_chunk"}:
            progress_text = format_prefetch_media_progress_line(update)
            if progress_text:
                self._notice_text = progress_text
        elif phase == "materialize_assets":
            current = int(update.get("current") or 0)
            total = int(update.get("total") or 0)
            asset_type = str(update.get("asset_type") or "-")
            asset_role = str(update.get("asset_role") or "").strip()
            role_suffix = f".{asset_role}" if asset_role else ""
            copied = int(update.get("copied_assets") or 0)
            reused = int(update.get("reused_assets") or 0)
            missing = int(update.get("missing_assets") or 0)
            errors = int(update.get("error_assets") or 0)
            if elapsed_s > 0 and current > 0:
                rate_suffix = f" rate={current / elapsed_s:.1f}/s elapsed={elapsed_s:.1f}s"
            self._notice_text = (
                "Export materializing assets... "
                f"{current}/{total} {asset_type}{role_suffix} "
                f"copied={copied} reused={reused} missing={missing} err={errors}{rate_suffix}"
            )
        self._invalidate()

    def _format_watch_download_progress(self, update: dict[str, Any]) -> str | None:
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
        last_asset_type = str(update.get("last_asset_type") or "").strip()
        last_file_name = str(update.get("last_file_name") or "").strip()
        last_status = str(update.get("last_status") or "").strip()
        if stage in {"done", "complete"} and not total:
            return ""
        parts = [f"remote_downloads(subqueue): {stage}"]
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
        return " ".join(parts)

    def _execute_export_sync(self, parsed, progress_callback=None) -> tuple[ExportBundleResult, int, dict[str, object], dict[str, Any]]:
        export_gateway = NapCatGateway(self._settings)
        history_page_size = max(100, parsed.limit, min(parsed.data_count or 0, 500))
        out_path = parsed.out_path or build_default_output_path(
            self._settings.export_dir,
            chat_type=self._request.chat_type,
            chat_id=self._request.chat_id,
            fmt=parsed.fmt,
        )
        cleanup_done = False
        try:
            def emit_stage(stage: str, status: str, *, elapsed_s: float | None = None, **payload: Any) -> None:
                if progress_callback is None:
                    return
                update: dict[str, Any] = {
                    "phase": "pipeline_stage",
                    "stage": stage,
                    "status": status,
                    **payload,
                }
                if elapsed_s is not None:
                    update["elapsed_s"] = round(elapsed_s, 4)
                    update["elapsed_ms"] = int(round(elapsed_s * 1000))
                progress_callback(update)

            request = ExportRequest(
                chat_type=self._request.chat_type,
                chat_id=self._request.chat_id,
                chat_name=self._request.chat_name,
                limit=parsed.data_count or parsed.limit,
                include_raw=parsed.include_raw,
            )
            fetch_started = time.monotonic()
            emit_stage(
                "watch.fetch_snapshot",
                "start",
                history_page_size=history_page_size,
                data_count=parsed.data_count,
                interval=parsed.interval,
            )
            if parsed.interval is None:
                if parsed.data_count:
                    snapshot = export_gateway.fetch_snapshot_tail(
                        request,
                        data_count=parsed.data_count,
                        page_size=history_page_size,
                        progress_callback=progress_callback,
                    )
                else:
                    snapshot = export_gateway.fetch_snapshot(request, progress_callback=progress_callback)
            else:
                if interval_is_full_history(parsed.interval):
                    snapshot = export_gateway.fetch_full_snapshot(
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
                else:
                    bounds = None
                    if interval_needs_history_bounds(parsed.interval):
                        special_kinds = interval_special_kinds(parsed.interval)
                        bounds = export_gateway.get_history_bounds(
                            request,
                            page_size=history_page_size,
                            need_earliest="earliest_content" in special_kinds,
                            need_final="final_content" in special_kinds,
                            progress_callback=progress_callback,
                        )
                    interval_start, interval_end = resolve_interval(parsed.interval, bounds=bounds)
                    interval_request = request.model_copy(update={"since": interval_start, "until": interval_end})
                    if parsed.data_count:
                        snapshot = export_gateway.fetch_snapshot_tail_between(
                            interval_request,
                            data_count=parsed.data_count,
                            page_size=history_page_size,
                            progress_callback=progress_callback,
                        )
                    else:
                        snapshot = export_gateway.fetch_snapshot_between(
                            interval_request,
                            page_size=history_page_size,
                            progress_callback=progress_callback,
                        )
                    snapshot.metadata["resolved_since"] = format_export_datetime(min(interval_start, interval_end))
                    snapshot.metadata["resolved_until"] = format_export_datetime(max(interval_start, interval_end))
                    snapshot.metadata["interval_mode"] = "closed"
            emit_stage(
                "watch.fetch_snapshot",
                "done",
                elapsed_s=time.monotonic() - fetch_started,
                source_history_source=str(snapshot.metadata.get("source") or ""),
                source_message_count=len(snapshot.messages),
            )
            normalize_started = time.monotonic()
            emit_stage("watch.normalize_snapshot", "start", source_message_count=len(snapshot.messages))
            normalized = self._service.build_snapshot(snapshot, include_raw=parsed.include_raw)
            emit_stage(
                "watch.normalize_snapshot",
                "done",
                elapsed_s=time.monotonic() - normalize_started,
                normalized_message_count=len(normalized.messages),
            )
            trim_started = time.monotonic()
            emit_stage("watch.trim_snapshot", "start", normalized_message_count=len(normalized.messages), data_count=parsed.data_count)
            normalized = trim_snapshot_to_last_messages(normalized, data_count=parsed.data_count)
            emit_stage(
                "watch.trim_snapshot",
                "done",
                elapsed_s=time.monotonic() - trim_started,
                trimmed_message_count=len(normalized.messages),
            )
            profile_started = time.monotonic()
            emit_stage("watch.apply_export_profile", "start", message_count=len(normalized.messages), profile=parsed.profile)
            normalized = apply_export_profile(normalized, parsed.profile)
            emit_stage(
                "watch.apply_export_profile",
                "done",
                elapsed_s=time.monotonic() - profile_started,
                profiled_message_count=len(normalized.messages),
            )
            bundle_started = time.monotonic()
            emit_stage("watch.write_bundle", "start", normalized_message_count=len(normalized.messages))
            bundle = self._service.write_bundle(
                normalized,
                out_path,
                fmt=parsed.fmt,
                media_resolution_mode="napcat_only",
                media_download_manager=(
                    export_gateway.build_media_download_manager()
                    if hasattr(export_gateway, "build_media_download_manager")
                    else None
                ),
                progress_callback=progress_callback,
            )
            emit_stage(
                "watch.write_bundle",
                "done",
                elapsed_s=time.monotonic() - bundle_started,
                copied_asset_count=bundle.copied_asset_count,
                reused_asset_count=bundle.reused_asset_count,
                missing_asset_count=bundle.missing_asset_count,
                error_asset_count=bundle.error_asset_count,
            )
            cleanup_started = time.monotonic()
            emit_stage("watch.cleanup_remote_cache", "start")
            cleanup_stats = cleanup_gateway_media_cache(
                export_gateway,
                logger=self._logger,
            )
            emit_stage("watch.cleanup_remote_cache", "done", elapsed_s=time.monotonic() - cleanup_started, **cleanup_stats)
            cleanup_done = True
            summary_started = time.monotonic()
            emit_stage("watch.build_export_content_summary", "start", record_count=len(normalized.messages))
            content_summary = build_export_content_summary(
                normalized,
                bundle,
                profile=parsed.profile,
                fmt=parsed.fmt,
                strict_missing=parsed.strict_missing,
            )
            emit_stage(
                "watch.build_export_content_summary",
                "done",
                elapsed_s=time.monotonic() - summary_started,
                record_count=len(normalized.messages),
            )
            return bundle, len(normalized.messages), content_summary, cleanup_stats
        finally:
            if not cleanup_done:
                cleanup_gateway_media_cache(
                    export_gateway,
                    logger=self._logger,
                )
            export_gateway.close()

    def _build_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @Condition
        def can_roll_export_date() -> bool:
            app = get_app_or_none()
            if app is None:
                return False
            buffer = self._command_input.buffer
            if buffer.complete_state is not None:
                return False
            return (
                app.layout.current_control == self._command_input.control
                and roll_export_date_token(
                    buffer.text,
                    cursor_position=buffer.cursor_position,
                    delta=0,
                ) is not None
            )

        @bindings.add("pageup")
        def _(event) -> None:
            self._scroll_relative(-self._page_delta())

        @bindings.add("pagedown")
        def _(event) -> None:
            self._scroll_relative(self._page_delta())

        @bindings.add(" ")
        def _(event) -> None:
            buffer = self._command_input.buffer
            buffer.insert_text(" ")
            if _watch_should_start_completion_on_space(buffer.text):
                buffer.start_completion(select_first=False)

        @bindings.add("/")
        def _(event) -> None:
            buffer = self._command_input.buffer
            buffer.insert_text("/")
            if buffer.text == "/":
                buffer.start_completion(select_first=False)

        @bindings.add("tab")
        def _(event) -> None:
            buffer = self._command_input.buffer
            if buffer.complete_state:
                buffer.complete_next()
            else:
                buffer.start_completion(select_first=_watch_should_select_first_completion(buffer.text))

        @bindings.add("down", filter=has_completions)
        def _(event) -> None:
            self._command_input.buffer.complete_next()

        @bindings.add("up", filter=has_completions)
        def _(event) -> None:
            self._command_input.buffer.complete_previous()

        @bindings.add("up", filter=can_roll_export_date)
        def _(event) -> None:
            self._roll_command_date(delta=1)

        @bindings.add("up", filter=~has_completions & ~can_roll_export_date)
        def _(event) -> None:
            self._scroll_relative(-1)

        @bindings.add("down", filter=~has_completions & ~can_roll_export_date)
        def _(event) -> None:
            self._scroll_relative(1)

        @bindings.add("down", filter=can_roll_export_date)
        def _(event) -> None:
            self._roll_command_date(delta=-1)

        @bindings.add("escape", filter=has_completions)
        def _(event) -> None:
            self._command_input.buffer.cancel_completion()

        @bindings.add("left")
        def _(event) -> None:
            buffer = self._command_input.buffer
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
            buffer = self._command_input.buffer
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
            buffer = self._command_input.buffer
            completion = _get_selected_completion(buffer)
            if completion is None:
                buffer.cancel_completion()
                return
            if completion_application_is_noop(buffer, completion):
                buffer.cancel_completion()
                buffer.validate_and_handle()
                return
            _accept_completion(buffer, completion)
            _start_watch_completion_followup(buffer, accepted_text=completion.text)

        @bindings.add("end")
        def _(event) -> None:
            self._follow_tail = True
            self._scroll_to_end()
            self._status_text = self._build_status_text()
            self._invalidate()

        @bindings.add("home")
        def _(event) -> None:
            self._follow_tail = False
            self._scroll_top = 0
            self._sync_cursor_to_view()
            self._status_text = self._build_status_text()
            self._invalidate()

        @bindings.add("c-c")
        def _(event) -> None:
            self._exit_reason = "ctrl_c"
            self._app.exit()

        return bindings

    def _get_header_text(self) -> str:
        return build_watch_header(self._target)

    def _roll_command_date(self, *, delta: int) -> None:
        updated = roll_export_date_token(
            self._command_input.buffer.text,
            cursor_position=self._command_input.buffer.cursor_position,
            delta=delta,
        )
        if updated is None:
            return
        new_text, new_cursor = updated
        self._command_input.buffer.document = Document(text=new_text, cursor_position=new_cursor)

    def _get_status_line(self):
        lines: list[str] = []
        if self._notice_text:
            lines.append(self._notice_text)
        if self._download_notice_text:
            lines.append(self._download_notice_text)
        lines.append(self._status_text)
        rendered = "\n".join(self._wrap_lines(lines))
        return ANSI(colorize_status_fields_for_ansi(rendered))

    def _get_help_line(self) -> str:
        if self._help_text != _default_watch_help_text():
            lines = [self._help_text, _default_watch_help_text()]
        else:
            lines = [self._help_text]
        return "\n".join(self._wrap_lines(lines))

    def _get_timeline_text(self) -> str:
        if not self._entries:
            return "No messages yet."
        width = self._timeline_content_width()
        wrapped: list[str] = []
        for entry in self._entries:
            wrapped.extend(_wrap_terminal_text(entry.text, width=width))
        return "\n".join(wrapped)

    def _build_status_text(self) -> str:
        total = len(self._entries)
        top = self._scroll_top + 1
        bottom = min(total, self._scroll_top + self._visible_window_height())
        follow = "on" if self._follow_tail else "off"
        history = "end" if self._history_exhausted else "more"
        return f"E={total} V={top}-{bottom} F={follow} H={history}"

    def _terminal_width(self) -> int:
        try:
            return max(20, int(self._app.output.get_size().columns))
        except Exception:
            return 120

    def _timeline_content_width(self) -> int:
        return _watch_content_width(
            terminal_columns=self._terminal_width(),
            reserve_scrollbar=self._reserve_timeline_scrollbar_column,
        )

    def _wrap_lines(self, lines: list[str]) -> list[str]:
        width = self._terminal_width()
        wrapped: list[str] = []
        for line in lines:
            wrapped.extend(_wrap_terminal_text(line, width=width))
        return wrapped or [""]

    def _get_status_height(self) -> int:
        lines = [self._notice_text, self._status_text] if self._notice_text else [self._status_text]
        return max(1, len(self._wrap_lines(lines)))

    def _get_help_height(self) -> int:
        lines = [self._help_text, _default_watch_help_text()] if self._help_text != _default_watch_help_text() else [self._help_text]
        return max(1, len(self._wrap_lines(lines)))

    def _visible_window_height(self) -> int:
        render_info = self._message_area.window.render_info
        if render_info is None:
            return 10
        return max(1, render_info.window_height)

    def _clamp_scroll_top(self) -> None:
        line_count = max(1, self._message_area.buffer.document.line_count)
        max_scroll = max(0, line_count - self._visible_window_height())
        self._scroll_top = min(max_scroll, max(0, self._scroll_top))

    def _sync_cursor_to_view(self) -> None:
        document = self._message_area.buffer.document
        if document.line_count <= 0:
            self._message_area.buffer.cursor_position = 0
            return
        target_line = min(self._scroll_top, max(0, document.line_count - 1))
        target_index = document.translate_row_col_to_index(target_line, 0)
        self._message_area.buffer.cursor_position = target_index

    def _invalidate(self) -> None:
        self._app.invalidate()


def build_watch_header(target: ChatTarget) -> str:
    if target.chat_type == "group":
        return f"群聊 · {format_target_name(target)} ({target.chat_id})"
    if target.remark and target.remark != target.name:
        return (
            f"好友 · {format_target_name(target)} ({target.chat_id}) "
            f"备注名 {format_target_remark(target)}"
        )
    return f"好友 · {format_target_name(target)} ({target.chat_id})"


def render_watch_transcript_line(message: NormalizedMessage) -> str:
    timestamp = message.timestamp_iso.replace("T", " ")[:19]
    sender = format_display_name(
        message.sender_card or message.sender_name or message.sender_id,
        kind="昵称",
    )
    content = terminal_safe_text(render_debug_content(message) or _describe_empty_message(message))
    return f"[{timestamp}] {sender} ({message.sender_id}): {content}"


def _build_message_entry(message: NormalizedMessage) -> WatchTimelineEntry:
    dedupe_key = "|".join(
        [
            message.message_id or "",
            message.message_seq or "",
            str(message.timestamp_ms),
            message.sender_id,
            message.content,
        ]
    )
    return WatchTimelineEntry(
        sort_key=(message.timestamp_ms, message.message_seq or "", dedupe_key),
        text=render_watch_transcript_line(message),
        dedupe_key=dedupe_key,
    )


def _build_watch_entry(
    event: dict[str, Any],
    *,
    chat_type: str,
    chat_id: str,
    chat_name: str | None,
) -> WatchTimelineEntry:
    if event.get("post_type") in {"message", "message_sent"}:
        message = normalize_message(
            event,
            chat_type=chat_type,
            chat_id=chat_id,
            chat_name=chat_name,
        )
        return _build_message_entry(message)
    return _build_notice_entry(event, chat_type=chat_type)


def _build_notice_entry(event: dict[str, Any], *, chat_type: str) -> WatchTimelineEntry:
    timestamp_ms = int(event.get("time") or 0) * 1000
    timestamp = _format_event_time(event)
    notice_type = str(event.get("notice_type") or "unknown")
    sub_type = str(event.get("sub_type") or "").strip()
    chat_scope = "群聊" if chat_type == "group" else "好友"
    text = _render_notice_text(event, notice_type=notice_type, sub_type=sub_type, chat_scope=chat_scope)
    dedupe_key = "|".join(
        [
            "notice",
            notice_type,
            sub_type,
            str(event.get("message_id") or ""),
            str(event.get("group_id") or event.get("peer_id") or event.get("user_id") or ""),
            str(event.get("operator_id") or ""),
            str(event.get("time") or ""),
        ]
    )
    return WatchTimelineEntry(
        sort_key=(timestamp_ms, notice_type, dedupe_key),
        text=f"[{timestamp}] [system] {text}",
        dedupe_key=dedupe_key,
    )


def _format_event_time(event: dict[str, Any]) -> str:
    timestamp = int(event.get("time") or 0)
    if timestamp <= 0:
        return "0000-00-00 00:00:00"
    return datetime.fromtimestamp(timestamp, tz=EXPORT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _render_notice_text(
    event: dict[str, Any],
    *,
    notice_type: str,
    sub_type: str,
    chat_scope: str,
) -> str:
    user_id = _id_text(event.get("user_id"))
    operator_id = _id_text(event.get("operator_id"))
    target_id = _id_text(event.get("target_id"))
    message_id = _id_text(event.get("message_id"))
    if notice_type == "friend_recall":
        return f"好友 {user_id} 撤回了一条消息 (message_id={message_id})"
    if notice_type == "group_recall":
        return f"{operator_id} 撤回了 {user_id} 的消息 (message_id={message_id})"
    if notice_type == "group_upload":
        file = event.get("file") or {}
        return f"{user_id} 上传了群文件 {file.get('name') or '[unknown file]'}"
    if notice_type == "online_file_send":
        peer_id = _id_text(event.get("peer_id"))
        return f"在线文件发送状态更新: peer={peer_id}, sub_type={sub_type or 'unknown'}"
    if notice_type == "online_file_receive":
        peer_id = _id_text(event.get("peer_id"))
        return f"在线文件接收状态更新: peer={peer_id}, sub_type={sub_type or 'unknown'}"
    if notice_type == "group_admin":
        action = "被设为管理员" if sub_type == "set" else "被取消管理员"
        return f"{user_id} {action}"
    if notice_type == "group_increase":
        action = "邀请入群" if sub_type == "invite" else "通过审核入群"
        return f"{operator_id} 使 {user_id} {action}"
    if notice_type == "group_decrease":
        if sub_type == "leave":
            return f"{user_id} 退出了群聊"
        if sub_type == "kick_me":
            return f"机器人被 {operator_id} 移出了群聊"
        return f"{user_id} 被 {operator_id} 移出了群聊"
    if notice_type == "group_ban":
        if sub_type == "lift_ban":
            return f"{operator_id} 解除了 {user_id} 的禁言"
        return f"{operator_id} 将 {user_id} 禁言 {event.get('duration') or 0} 秒"
    if notice_type == "friend_add":
        return f"新增好友 {user_id}"
    if notice_type == "group_msg_emoji_like":
        likes = event.get("likes") or []
        summaries = []
        if isinstance(likes, list):
            for item in likes:
                if isinstance(item, dict):
                    summaries.append(f"{item.get('emoji_id')}x{item.get('count')}")
        joined = ", ".join(summaries) if summaries else "unknown"
        return f"{user_id} 对消息 {message_id} 添加了表情回应 {joined}"
    if notice_type == "essence":
        action = "设为精华" if sub_type != "delete" else "取消精华"
        sender_id = _id_text(event.get("sender_id"))
        return f"{operator_id} 将 {sender_id} 的消息 {message_id} {action}"
    if notice_type == "group_card":
        return f"{user_id} 更新了群名片: {event.get('card_old') or ''} -> {event.get('card_new') or ''}"
    if notice_type == "notify":
        if sub_type == "poke":
            return f"{user_id} 戳了 {target_id}"
        if sub_type == "lucky_king":
            return f"{user_id} 发出的红包中，{target_id} 成为运气王"
        if sub_type == "honor":
            return f"{user_id} 获得群荣誉 {event.get('honor_type') or 'unknown'}"
        if sub_type == "group_name":
            return f"{chat_scope} 名称更新为 {event.get('name_new') or '[unknown]'}"
        if sub_type == "input_status":
            return f"{event.get('status_text') or '对方正在输入...'}"
    return f"{chat_scope} 系统事件 notice_type={notice_type} sub_type={sub_type or '-'}"


def _describe_empty_message(message: NormalizedMessage) -> str:
    if message.extra.get("is_recalled"):
        return "[recalled message]"
    if message.extra.get("is_system_message"):
        return "[system message]"
    if any(segment.type == "unsupported" for segment in message.segments):
        tokens = [segment.token for segment in message.segments if segment.token]
        return " ".join(tokens) if tokens else "[unsupported message]"
    return "[empty]"


def _id_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "unknown"


def _message_anchor(message: NormalizedMessage) -> str | None:
    return message.message_seq or message.message_id or None


def _friendly_watch_parse_error(exc: Exception) -> str:
    message = str(exc or "").strip()
    lowered = message.casefold()
    if "quotation" in lowered or "quote" in lowered:
        return "命令里的引号似乎没有闭合。请补上引号后重试，或改用 QQ 号。"
    return f"命令格式无法解析：{message or '请检查输入'}。可输入 /help 查看格式。"


def _friendly_watch_runtime_notice(prefix: str, exc: Exception, *, suffix: str) -> str:
    message = str(exc or "").strip() or exc.__class__.__name__
    log_path = get_cli_log_path()
    if log_path:
        return f"{prefix}：{message}。{suffix} 日志：{log_path}"
    return f"{prefix}：{message}。{suffix}"




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


def _parse_int_option(options: dict[str, Any], key: str, *, default: int) -> int:
    value = options.get(key)
    if value in {None, False}:
        return default
    return int(str(value))


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


def _start_watch_completion_followup(buffer, *, accepted_text: str | None = None) -> None:
    followup = _watch_completion_followup(buffer.text, accepted_text=accepted_text)
    if followup == "space_then_complete":
        buffer.insert_text(" ", fire_event=False)
        buffer.start_completion(select_first=False)
    elif followup == "same_token_complete":
        buffer.start_completion(select_first=False)
    elif followup == "cancel":
        buffer.cancel_completion()


def _watch_completion_followup(text: str, *, accepted_text: str | None = None) -> str | None:
    accepted = (accepted_text or "").strip()
    accepted_normalized = accepted.casefold()
    if accepted_normalized in {"astxt", "asjsonl", "data_count="}:
        return "cancel"
    normalized = text.strip()
    if normalized.casefold() in EXPORT_COMMAND_PROFILES:
        return "space_then_complete"
    tokens = _split_watch_tokens(normalized)
    if _needs_same_token_watch_followup(tokens):
        return "same_token_complete"
    if len(tokens) == 2 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES:
        return "space_then_complete"
    if len(tokens) == 3 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES:
        return "space_then_complete"
    return "cancel"


def _watch_should_start_completion_on_space(text: str) -> bool:
    if not text.endswith(" "):
        return False
    tokens = _split_watch_tokens(text)
    return (
        (len(tokens) == 1 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES)
        or (len(tokens) == 2 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES)
        or (len(tokens) == 3 and tokens[0].casefold() in EXPORT_COMMAND_PROFILES)
    )


def _watch_should_select_first_completion(text: str) -> bool:
    normalized = text.rstrip()
    if normalized.casefold() in EXPORT_COMMAND_PROFILES:
        return False
    tokens = _split_watch_tokens(normalized)
    if len(tokens) in {2, 3} and tokens[0].casefold() in EXPORT_COMMAND_PROFILES:
        return False
    return True


def _split_watch_tokens(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _needs_same_token_watch_followup(tokens: list[str]) -> bool:
    if not tokens or tokens[0].casefold() not in EXPORT_COMMAND_PROFILES:
        return False
    export_tokens = _strip_watch_format_alias(tokens[1:])
    if not export_tokens:
        return False
    return is_explicit_datetime_literal(export_tokens[-1]) and export_tokens[-1].endswith("_00-00-00")


def _strip_watch_format_alias(tokens: list[str]) -> list[str]:
    if tokens and tokens[-1].casefold() in {"astxt", "asjsonl"}:
        return tokens[:-1]
    return tokens
