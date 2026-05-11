from __future__ import annotations

from datetime import datetime

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_formatted_text

from qq_data_cli.completion import SlashCommandCompleter
from qq_data_core.models import EXPORT_TIMEZONE
from qq_data_integrations.napcat.models import NapCatQuickLoginAccount


def _empty_target_lookup(_chat_type: str, _keyword: str | None, _limit: int):
    return []


def _quick_login_lookup(keyword: str | None, _limit: int):
    candidates = [
        NapCatQuickLoginAccount(uin="3956020260", nick_name="wiki"),
        NapCatQuickLoginAccount(uin="1507833383", nick_name="ㅤㅤㅤㅤㅤㅤㅤㅤ"),
        NapCatQuickLoginAccount(uin="498603055", nick_name="498603055"),
    ]
    if not keyword:
        return candidates
    lowered = keyword.casefold()
    return [
        candidate
        for candidate in candidates
        if lowered in candidate.uin.casefold() or lowered in (candidate.nick_name or "").casefold()
    ]


def test_login_completion_suggests_quick_login_uins_after_command() -> None:
    completer = SlashCommandCompleter(
        target_lookup=_empty_target_lookup,
        quick_login_lookup=_quick_login_lookup,
        now_provider=lambda: datetime.now(EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(Document("/login "), None)
    )

    completion_texts = {item.text for item in completions}
    assert "3956020260" in completion_texts
    assert "1507833383" in completion_texts
    assert "498603055" in completion_texts
    assert "--refresh" not in completion_texts


def test_login_completion_suggests_quick_login_uins_for_quick_uin_option() -> None:
    completer = SlashCommandCompleter(
        target_lookup=_empty_target_lookup,
        quick_login_lookup=_quick_login_lookup,
        now_provider=lambda: datetime.now(EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(Document("/login --quick-uin "), None)
    )

    completion_texts = {item.text for item in completions}
    assert "3956020260" in completion_texts
    assert "1507833383" in completion_texts


def test_login_completion_filters_quick_login_uin_by_prefix_when_digits_are_typed() -> None:
    completer = SlashCommandCompleter(
        target_lookup=_empty_target_lookup,
        quick_login_lookup=_quick_login_lookup,
        now_provider=lambda: datetime.now(EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(Document("/login 3"), None)
    )

    completion_texts = {item.text for item in completions}
    assert "3956020260" in completion_texts
    assert "1507833383" not in completion_texts
    assert "498603055" not in completion_texts


def test_login_completion_suggests_inline_quick_uin_values_without_trailing_space() -> None:
    completer = SlashCommandCompleter(
        target_lookup=_empty_target_lookup,
        quick_login_lookup=_quick_login_lookup,
        now_provider=lambda: datetime.now(EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(Document("/login --quick-uin"), None)
    )

    completion_texts = {item.text for item in completions}
    assert "--quick-uin 3956020260" in completion_texts
    assert "--quick-uin 1507833383" in completion_texts


def test_login_completion_only_shows_options_after_explicit_dash_prefix() -> None:
    completer = SlashCommandCompleter(
        target_lookup=_empty_target_lookup,
        quick_login_lookup=_quick_login_lookup,
        now_provider=lambda: datetime.now(EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(Document("/login --"), None)
    )

    completion_texts = {item.text for item in completions}
    assert "--refresh" in completion_texts
    assert "--quick-uin" in completion_texts
    assert "3956020260" not in completion_texts


def test_login_completion_shows_blank_like_nick_as_blank_id() -> None:
    completer = SlashCommandCompleter(
        target_lookup=_empty_target_lookup,
        quick_login_lookup=_quick_login_lookup,
        now_provider=lambda: datetime.now(EXPORT_TIMEZONE),
    )

    completions = list(
        completer.get_completions(Document("/login "), None)
    )

    blank_candidate = next(item for item in completions if item.text == "1507833383")
    assert "".join(fragment for _style, fragment in to_formatted_text(blank_candidate.display_meta)) == "<空白ID>"
