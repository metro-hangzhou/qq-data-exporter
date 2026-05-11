from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from qq_data_cli.export_commands import parse_watch_export_command
from qq_data_cli.terminal_compat import CliUiProfile
from qq_data_cli.watch_view import (
    FixedThumbScrollbarMargin,
    WatchConversationView,
    _build_watch_entry,
    build_watch_header,
    render_watch_transcript_line,
)
from qq_data_core import (
    ChatExportService,
    ExportBundleResult,
    ExportRequest,
    NormalizedMessage,
    NormalizedSegment,
    SourceChatSnapshot,
    WatchRequest,
)
from qq_data_integrations.napcat import ChatTarget, NapCatSettings


class _FakeGateway:
    def __init__(self) -> None:
        self.history_before_calls: list[tuple[str | None, int | None]] = []

    def fetch_snapshot(self, request: ExportRequest) -> SourceChatSnapshot:
        return SourceChatSnapshot(
            chat_type=request.chat_type,
            chat_id=request.chat_id,
            chat_name=request.chat_name,
            metadata={"requested_count": request.limit or 20},
            messages=[
                {
                    "message_id": str(index),
                    "message_seq": str(index),
                    "time": 1736563400 + index,
                    "user_id": "42",
                    "sender": {"nickname": "菜鸡"},
                    "message": [{"type": "text", "data": {"text": f"hello {index}"}}],
                }
                for index in range(11, 23)
            ],
        )

    def fetch_history_before(
        self,
        request: ExportRequest,
        *,
        before_message_seq: str | None,
        count: int | None = None,
    ) -> SourceChatSnapshot:
        self.history_before_calls.append((before_message_seq, count))
        if before_message_seq == "11":
            messages = [
                {
                    "message_id": str(index),
                    "message_seq": str(index),
                    "time": 1736563400 + index,
                    "user_id": "42",
                    "sender": {"nickname": "菜鸡"},
                    "message": [{"type": "text", "data": {"text": f"older {index}"}}],
                }
                for index in range(1, 11)
            ]
        else:
            messages = []
        return SourceChatSnapshot(
            chat_type=request.chat_type,
            chat_id=request.chat_id,
            chat_name=request.chat_name,
            metadata={"requested_count": count or request.limit or 20},
            messages=messages,
        )

    async def watch(self, request: WatchRequest):
        if False:
            yield request


def test_build_watch_header_for_group() -> None:
    target = ChatTarget(chat_type="group", chat_id="10001", name="Alpha")
    assert build_watch_header(target) == "群聊 · Alpha (10001)"


def test_build_watch_header_for_friend_with_remark() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡", remark="测试备注")
    assert build_watch_header(target) == "好友 · 菜鸡 (42) 备注名 测试备注"


def test_build_watch_header_for_blank_like_friend_name() -> None:
    target = ChatTarget(chat_type="private", chat_id="1507833383", name="\u3164\u3164\u3164\u3164")
    assert build_watch_header(target) == "好友 · <空白昵称> (1507833383)"


def test_render_watch_transcript_line_formats_sender_and_placeholders() -> None:
    message = NormalizedMessage(
        chat_type="private",
        chat_id="42",
        peer_id="42",
        sender_id="42",
        sender_name="菜鸡",
        sender_card=None,
        timestamp_ms=1736553827000,
        timestamp_iso="2026-01-11T10:43:47+08:00",
        content="hello [image:test.jpg]",
        text_content="hello",
        segments=[
            NormalizedSegment(type="text", text="hello"),
            NormalizedSegment(type="image", token="[image:test.jpg]", file_name="test.jpg"),
        ],
    )

    assert render_watch_transcript_line(message) == "[2026-01-11 10:43:47] 菜鸡 (42): hello [image]"


def test_render_watch_transcript_line_marks_blank_like_nickname_as_visible_placeholder() -> None:
    message = NormalizedMessage(
        chat_type="private",
        chat_id="42",
        peer_id="42",
        sender_id="42",
        sender_name="\u3164\u3164\u3164",
        sender_card=None,
        timestamp_ms=1736553827000,
        timestamp_iso="2026-01-11T10:43:47+08:00",
        content="hello",
        text_content="hello",
        segments=[NormalizedSegment(type="text", text="hello")],
    )

    assert render_watch_transcript_line(message) == "[2026-01-11 10:43:47] <空白昵称> (42): hello"


def test_watch_view_loads_initial_history_into_timeline() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._load_initial_history()
        view._refresh_view_state()

        assert "hello 11" in view._get_timeline_text()
        assert view._history_exhausted is False
        assert callable(view._message_area.window.always_hide_cursor)
        assert view._app.layout.container.__class__.__name__ == "FloatContainer"


def test_watch_view_reports_unclosed_quote_without_crashing() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._handle_command('/export "broken')

        assert "引号" in view._notice_text


def test_watch_view_compat_profile_disables_risky_terminal_features() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    profile = CliUiProfile(
        mode="compat",
        show_completion_menu=False,
        complete_while_typing=False,
        watch_full_screen=False,
        use_custom_scrollbar=False,
        use_highlight_style=False,
    )
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            ui_profile=profile,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        floats = view._app.layout.container.floats

        assert view._app.full_screen is False
        assert len(floats) == 0
        assert not any(isinstance(margin, FixedThumbScrollbarMargin) for margin in view._message_area.window.right_margins)
        assert bool(view._command_input.buffer.complete_while_typing()) is False
        assert view._app.style is None


def test_watch_view_plain_text_command_gets_friendly_hint() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._handle_command("hello")

        assert view._notice_text == "当前窗口只接受 /export*、/help 和 /exit。"


def test_watch_view_unknown_command_gets_recovery_hint() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._handle_command("/watc")

        assert view._notice_text == "未识别的命令：/watc。当前窗口可用 /export*、/help、/exit。"


def test_watch_view_help_uses_localized_notice() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    settings = NapCatSettings()
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=settings,
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._handle_command("/help")

        assert f"默认导出目录：{settings.export_dir}" in view._notice_text
        assert "默认格式：jsonl" in view._notice_text
        assert "asTXT" in view._notice_text


def test_watch_export_starts_in_background_without_blocking_ui() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        def fake_create_background_task(coro):
            coro.close()
            return SimpleNamespace(done=lambda: False, cancel=lambda: None)

        view._app.create_background_task = fake_create_background_task
        view._handle_export("/export", [])

        assert view._notice_text.startswith("导出已开始，目标目录：")
        assert "导出已开始，目标目录：" in view._get_status_line()


def test_watch_export_rejects_second_export_with_friendly_notice() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )
        view._export_task = SimpleNamespace(done=lambda: False)

        view._handle_export("/export", [])

        assert view._notice_text == "已有导出任务在运行，请等当前任务完成后再试。"


def test_watch_view_history_load_error_is_recovery_oriented() -> None:
    class BrokenGateway(_FakeGateway):
        def fetch_history_before(self, request: ExportRequest, *, before_message_seq: str | None, count: int | None = None):
            raise RuntimeError("boom")

    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=BrokenGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )
        view._oldest_history_anchor = "11"

        added = view._load_older_history()

        assert added == 0
        assert "加载更早历史失败" in view._notice_text
        assert "当前窗口仍可继续使用" in view._notice_text


def test_watch_export_background_job_reports_success() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    settings = NapCatSettings()
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=settings,
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )
        view._service.write_bundle = lambda snapshot, output_path, **kwargs: ExportBundleResult(  # type: ignore[method-assign]
            data_path=output_path,
            manifest_path=output_path.with_suffix(".manifest.json"),
            assets_dir=output_path.parent / f"{output_path.stem}_assets",
            record_count=len(snapshot.messages),
            copied_asset_count=3,
            reused_asset_count=1,
            missing_asset_count=0,
            assets=[],
        )

        parsed = parse_watch_export_command("/export", [], {}, default_limit=80)
        asyncio.run(view._run_export(parsed))

        assert view._notice_text.startswith("Exported 12 -> ")
        assert "Exported 12 -> " in view._get_status_line()
        assert "export_summary:" in view._get_help_line()
        assert "content_export=" in view._get_help_line()
        assert "asset_materialization=" in view._get_help_line()
        assert "/export*" in view._get_help_line()


def test_watch_export_notice_survives_scroll_status_refresh() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._notice_text = "Exported 12 -> group_42.txt | 1.2s"
        view._help_text = "\n".join(
            [
                "export_summary:",
                "  profile=all msgs=12 source_msgs=12 requested_data_count=-",
                "  content_export=[text:12/12, system:0/0, share:0/0, forward:0/0, image:3/3, video:0/0, speech:0/0, file:0/0, emoji:0/0, sticker:0/0, reply:0/0, unsupported:0/0]",
                "  asset_materialization=[image:3/3 miss=0 err=0, video:0/0 miss=0 err=0, speech:0/0 miss=0 err=0, file:0/0 miss=0 err=0, sticker.static:0/0 miss=0 err=0, sticker.dynamic:0/0 miss=0 err=0, sticker:0/0 miss=0 err=0]",
            ]
        )
        view._load_initial_history()
        view._refresh_message_area()
        view._scroll_relative(1)

        assert view._notice_text.startswith("Exported 12 -> ")
        assert "Exported 12 -> " in view._get_status_line()
        assert "export_summary:" in view._get_help_line()
        assert "/export*" in view._get_help_line()


def test_watch_view_renders_recall_notice_as_system_line() -> None:
    entry = _build_watch_entry(
        {
            "time": 1736563427,
            "post_type": "notice",
            "notice_type": "friend_recall",
            "user_id": 42,
            "message_id": 123456,
        },
        chat_type="private",
        chat_id="42",
        chat_name="菜鸡",
    )

    assert "[system]" in entry.text
    assert "撤回了一条消息" in entry.text


def test_watch_view_syncs_cursor_with_scroll_top() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._message_area.buffer.set_document(
            view._message_area.buffer.document.__class__(text="\n".join(f"line {i}" for i in range(50)), cursor_position=0),
            bypass_readonly=True,
        )
        view._scroll_top = 12
        view._sync_cursor_to_view()

        assert view._message_area.buffer.document.cursor_position_row == 12


def test_watch_view_progress_uses_page_window_label_for_tail_scan() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._export_started_monotonic = 0.0
        view._apply_export_progress(
            {
                "phase": "tail_scan",
                "pages_scanned": 2,
                "matched_messages": 300,
                "requested_data_count": 300,
                "page_size": 300,
                "page_duration_s": 0.09,
                "oldest_content_at": datetime.fromisoformat("2025-09-30T12:59:04+08:00"),
                "newest_content_at": datetime.fromisoformat("2025-10-04T04:09:35+08:00"),
            }
        )

        assert "page_window=" in view._notice_text
        assert " window=" not in view._notice_text


def test_watch_view_progress_reports_asset_materialization() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._export_started_monotonic = 0.0
        view._apply_export_progress(
            {
                "phase": "materialize_assets",
                "current": 17,
                "total": 91,
                "asset_type": "image",
                "copied_assets": 9,
                "reused_assets": 2,
                "missing_assets": 6,
                "error_assets": 0,
            }
        )

        assert "Export materializing assets..." in view._notice_text
        assert "17/91" in view._notice_text
        assert "copied=9" in view._notice_text


def test_watch_view_progress_reports_forward_expand() -> None:
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=_FakeGateway(),
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=20,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._export_started_monotonic = 0.0
        view._apply_export_progress(
            {
                "phase": "forward_expand",
                "processed_forwards": 7,
                "total_forwards": 10,
                "resolved_forwards": 4,
            }
        )

        assert "Export expanding forwarded detail..." in view._notice_text
        assert "7/10" in view._notice_text
        assert "resolved=4" in view._notice_text


def test_watch_view_loads_older_history_when_scrolling_above_top() -> None:
    gateway = _FakeGateway()
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=gateway,
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=12,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._load_initial_history()
        view._refresh_message_area()
        view._scroll_top = 2
        view._scroll_relative(-6)

        transcript = view._get_timeline_text()
        assert "older 1" in transcript
        assert "hello 22" in transcript
        assert view._history_exhausted is False
        assert gateway.history_before_calls == [("11", 20)]


def test_watch_view_marks_history_end_only_when_anchor_stops_advancing() -> None:
    gateway = _FakeGateway()
    target = ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
    with create_pipe_input() as pipe_input:
        view = WatchConversationView(
            settings=NapCatSettings(),
            gateway=gateway,
            service=ChatExportService(),
            target=target,
            request=WatchRequest(chat_type="private", chat_id="42", chat_name="菜鸡"),
            history_limit=12,
            application_input=pipe_input,
            application_output=DummyOutput(),
        )

        view._load_initial_history()
        view._refresh_message_area()
        view._scroll_top = 2
        view._scroll_relative(-6)
        assert view._history_exhausted is False

        view._scroll_top = 0
        view._scroll_relative(-1)
        assert view._history_exhausted is True
        assert gateway.history_before_calls == [("11", 20), ("1", 20)]


def test_fixed_thumb_scrollbar_keeps_constant_thumb_height() -> None:
    margin = FixedThumbScrollbarMargin(thumb_height=4)
    low_content = SimpleNamespace(content_height=80, window_height=20, vertical_scroll=10)
    high_content = SimpleNamespace(content_height=800, window_height=20, vertical_scroll=100)

    low = margin.create_margin(low_content, width=1, height=20)
    high = margin.create_margin(high_content, width=1, height=20)

    assert _count_scrollbar_thumb_cells(low) == 4
    assert _count_scrollbar_thumb_cells(high) == 4


def _count_scrollbar_thumb_cells(margin_fragments: list[tuple[str, str]]) -> int:
    return sum(1 for style, _ in margin_fragments if "scrollbar.button" in style)
