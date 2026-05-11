from __future__ import annotations

from datetime import datetime

from prompt_toolkit.document import Document

from qq_data_cli.completion import WatchCommandCompleter
from qq_data_cli.watch_view import _watch_completion_followup
from qq_data_core.models import EXPORT_TIMEZONE


def test_watch_completion_suggests_commands() -> None:
    completer = WatchCommandCompleter()

    completions = list(completer.get_completions(Document("/e"), None))

    assert [item.text for item in completions] == [
        "/exit",
        "/export",
        "/export_onlyText",
        "/export_TextImage",
        "/export_TextImageEmoji",
    ]


def test_watch_completion_suggests_time_functions_after_export() -> None:
    completer = WatchCommandCompleter()

    completions = list(completer.get_completions(Document("/export_TextImage @"), None))

    assert [item.text for item in completions] == ["@final_content", "@earliest_content"]


def test_watch_completion_suggests_second_time_slot_after_first_expression() -> None:
    completer = WatchCommandCompleter(
        now_provider=lambda: datetime(2026, 3, 7, 13, 36, 51, tzinfo=EXPORT_TIMEZONE)
    )

    completions = list(completer.get_completions(Document("/export @final_content "), None))

    assert [item.text for item in completions] == [
        "2026-03-06_00-00-00",
        "@final_content",
        "@earliest_content",
    ]


def test_watch_completion_suggests_explicit_date_after_numeric_prefix() -> None:
    completer = WatchCommandCompleter(
        now_provider=lambda: datetime(2026, 3, 7, 13, 36, 51, tzinfo=EXPORT_TIMEZONE)
    )

    completions = list(completer.get_completions(Document("/export 2"), None))

    assert len(completions) == 1
    assert completions[0].text == "2026-03-07_00-00-00"


def test_watch_completion_suggests_format_aliases_after_two_dates() -> None:
    completer = WatchCommandCompleter()

    completions = list(
        completer.get_completions(
            Document("/export 2026-03-01_00-00-00 2026-03-07_13-36-51 "),
            None,
        )
    )

    assert [item.text for item in completions] == ["asTXT", "asJSONL"]


def test_watch_completion_suggests_time_stage_for_explicit_midnight_token() -> None:
    completer = WatchCommandCompleter(
        now_provider=lambda: datetime(2026, 3, 7, 13, 36, 51, tzinfo=EXPORT_TIMEZONE)
    )

    completions = list(completer.get_completions(Document("/export 2026-03-07_00-00-00"), None))

    assert [item.text for item in completions] == ["2026-03-07_13-36-51"]


def test_watch_completion_suggests_data_count_option() -> None:
    completer = WatchCommandCompleter()

    completions = list(completer.get_completions(Document("/export_onlyText --d"), None))

    assert [item.text for item in completions] == ["--data-count"]


def test_watch_completion_suggests_inline_data_count() -> None:
    completer = WatchCommandCompleter()

    completions = list(completer.get_completions(Document("/export d"), None))

    assert [item.text for item in completions] == ["data_count="]


def test_watch_completion_followup_cancels_after_terminal_export_tokens() -> None:
    assert (
        _watch_completion_followup(
            "/export @final_content @earliest_content asJSONL",
            accepted_text="asJSONL",
        )
        == "cancel"
    )
    assert (
        _watch_completion_followup(
            "/export data_count=",
            accepted_text="data_count=",
        )
        == "cancel"
    )
