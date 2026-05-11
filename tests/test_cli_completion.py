from __future__ import annotations

from datetime import datetime

from prompt_toolkit.document import Document

import qq_data_cli.completion as completion_module
from qq_data_cli.completion import SlashCommandCompleter
from qq_data_core.models import EXPORT_TIMEZONE
from qq_data_integrations.napcat import ChatTarget


def test_completion_suggests_group_targets_and_formats() -> None:
    def lookup(chat_type: str, keyword: str | None, limit: int):
        assert chat_type == "group"
        assert limit == 6
        return [
            ChatTarget(chat_type="group", chat_id="10001", name="Alpha Group", member_count=3),
            ChatTarget(chat_type="group", chat_id="10002", name="Alpine Lab", member_count=5),
        ]

    completer = SlashCommandCompleter(target_lookup=lookup)

    target_completions = list(completer.get_completions(Document("/export group Al"), None))
    assert [item.text for item in target_completions] == ["'Alpha Group'", "'Alpine Lab'"]

    format_completions = list(completer.get_completions(Document("/export group 10001 --format t"), None))
    assert [item.text for item in format_completions] == ["txt"]


def test_completion_uses_friend_remark_when_available() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="42", name="Original", remark="Best Friend")
        ]
    )

    completions = list(completer.get_completions(Document("/watch friend B"), None))

    assert [item.text for item in completions] == ["'Best Friend'"]


def test_completion_uses_chat_id_for_blank_like_target_name() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="1507833383", name="\u3164\u3164\u3164\u3164")
        ]
    )

    completions = list(completer.get_completions(Document("/watch friend "), None))

    assert len(completions) == 1
    assert completions[0].text == "1507833383"
    assert str(completions[0].display) == "FormattedText([('', '<空白昵称> (1507833383)')])"


def test_completion_logs_lookup_failure_once(monkeypatch) -> None:
    warnings: list[tuple[object, ...]] = []

    class FakeLogger:
        def warning(self, *args) -> None:
            warnings.append(args)

    monkeypatch.setattr(completion_module, "get_cli_logger", lambda name=None: FakeLogger())

    def lookup(chat_type: str, keyword: str | None, limit: int):
        raise RuntimeError("boom")

    completer = SlashCommandCompleter(target_lookup=lookup)

    first = list(completer.get_completions(Document("/watch friend abc"), None))
    second = list(completer.get_completions(Document("/watch friend abc"), None))

    assert first == []
    assert second == []
    assert len(warnings) == 1
    assert "completion_lookup_failed" in warnings[0][0]


def test_completion_suggests_chat_kinds_after_watch_and_export_command() -> None:
    completer = SlashCommandCompleter(target_lookup=lambda chat_type, keyword, limit: [])

    watch_space = list(completer.get_completions(Document("/watch "), None))
    watch_partial = list(completer.get_completions(Document("/watch f"), None))
    export_space = list(completer.get_completions(Document("/export "), None))
    export_image_space = list(completer.get_completions(Document("/export_TextImage "), None))

    assert [item.text for item in watch_space] == ["group", "friend"]
    assert [item.text for item in watch_partial] == ["friend"]
    assert [item.text for item in export_space] == ["group", "friend", "group_asBatch=", "friend_asBatch="]
    assert [item.text for item in export_image_space] == ["group", "friend", "group_asBatch=", "friend_asBatch="]


def test_completion_suggests_export_command_aliases() -> None:
    completer = SlashCommandCompleter(target_lookup=lambda chat_type, keyword, limit: [])

    completions = list(completer.get_completions(Document("/export_"), None))

    assert [item.text for item in completions] == [
        "/export_onlyText",
        "/export_TextImage",
        "/export_TextImageEmoji",
    ]


def test_completion_suggests_terminal_doctor_command() -> None:
    completer = SlashCommandCompleter(target_lookup=lambda chat_type, keyword, limit: [])

    completions = list(completer.get_completions(Document("/terminal"), None))

    assert [item.text for item in completions] == ["/terminal-doctor"]


def test_completion_suggests_export_time_functions_after_target() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
        ]
    )

    completions = list(completer.get_completions(Document("/export friend 菜鸡 @"), None))

    assert [item.text for item in completions] == ["@final_content", "@earliest_content"]


def test_completion_suggests_explicit_date_after_numeric_prefix() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
        ],
        now_provider=lambda: datetime(2026, 3, 7, 13, 36, 51, tzinfo=EXPORT_TIMEZONE),
    )

    completions = list(completer.get_completions(Document("/export friend 菜鸡 2"), None))

    assert len(completions) == 1
    assert completions[0].text == "2026-03-07_00-00-00"
    assert str(completions[0].display) == "FormattedText([('', '2026y-03mo-07d_00h_00m_00s')])"


def test_completion_suggests_output_alias_after_second_date() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
        ]
    )

    completions = list(
        completer.get_completions(
            Document("/export friend 菜鸡 2026-03-01_00-00-00 2026-03-07_13-36-51 "),
            None,
        )
    )

    assert [item.text for item in completions] == ["asTXT", "asJSONL"]


def test_completion_suggests_data_count_option_after_target() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
        ]
    )

    completions = list(completer.get_completions(Document("/export_onlyText friend 菜鸡 --d"), None))

    assert [item.text for item in completions] == ["--data-count"]


def test_completion_suggests_inline_data_count_after_target() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [
            ChatTarget(chat_type="private", chat_id="42", name="菜鸡")
        ]
    )

    completions = list(completer.get_completions(Document("/export friend 菜鸡 d"), None))

    assert [item.text for item in completions] == ["data_count="]


def test_completion_suggests_batch_targets_with_fuzzy_lookup() -> None:
    def lookup(chat_type: str, keyword: str | None, limit: int):
        assert chat_type == "group"
        assert keyword == "蕾"
        return [
            ChatTarget(chat_type="group", chat_id="1", name="蕾米二次元萌萌群"),
            ChatTarget(chat_type="group", chat_id="2", name="蕾米技术群"),
        ]

    completer = SlashCommandCompleter(target_lookup=lookup)

    completions = list(completer.get_completions(Document("/export group_asBatch=蕾"), None))

    assert [item.text for item in completions] == ["蕾米二次元萌萌群", "蕾米技术群"]
    assert all("group_asBatch=" not in str(item.display) for item in completions)


def test_completion_excludes_already_selected_batch_targets() -> None:
    def lookup(chat_type: str, keyword: str | None, limit: int):
        assert chat_type == "group"
        assert keyword == "蕾"
        return [
            ChatTarget(chat_type="group", chat_id="1", name="蕾米二次元萌萌群"),
            ChatTarget(chat_type="group", chat_id="2", name="蕾米技术群"),
        ]

    completer = SlashCommandCompleter(target_lookup=lookup)

    completions = list(
        completer.get_completions(
            Document("/export group_asBatch=蕾米二次元萌萌群,蕾"),
            None,
        )
    )

    assert [item.text for item in completions] == ["蕾米技术群"]


def test_completion_suggests_time_after_batch_target_token() -> None:
    completer = SlashCommandCompleter(
        target_lookup=lambda chat_type, keyword, limit: [],
        now_provider=lambda: datetime(2026, 3, 10, 12, 0, 0, tzinfo=EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(
            Document("/export group_asBatch=蕾米二次元萌萌群,哈基米开发群 "),
            None,
        )
    )

    assert [item.text for item in completions[:3]] == [
        "2026-03-10_00-00-00",
        "@final_content",
        "@earliest_content",
    ]
