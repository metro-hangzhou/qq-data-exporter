from __future__ import annotations

from types import SimpleNamespace

from qq_data_cli.completion_runtime import completion_application_is_noop
from qq_data_cli.repl import (
    _completion_followup,
    _get_selected_completion,
    _should_select_first_completion,
    _should_start_completion_on_space,
)


def test_get_selected_completion_prefers_current_completion() -> None:
    state = SimpleNamespace(current_completion="selected", completions=["first", "second"])
    buffer = SimpleNamespace(complete_state=state)
    assert _get_selected_completion(buffer) == "selected"


def test_get_selected_completion_falls_back_to_first_completion() -> None:
    state = SimpleNamespace(current_completion=None, completions=["first", "second"])
    buffer = SimpleNamespace(complete_state=state)
    assert _get_selected_completion(buffer) == "first"


def test_completion_application_is_noop_when_selected_completion_matches_full_text() -> None:
    buffer = SimpleNamespace(text="/watch group 蕾米二次元萌萌群", cursor_position=len("/watch group 蕾米二次元萌萌群"))
    completion = SimpleNamespace(text="蕾米二次元萌萌群", start_position=-8)
    assert completion_application_is_noop(buffer, completion) is True


def test_completion_application_is_not_noop_for_partial_target() -> None:
    buffer = SimpleNamespace(text="/watch group 蕾", cursor_position=len("/watch group 蕾"))
    completion = SimpleNamespace(text="蕾米二次元萌萌群", start_position=-1)
    assert completion_application_is_noop(buffer, completion) is False


def test_space_triggers_target_completion_for_watch_and_export() -> None:
    assert _should_start_completion_on_space("/watch ")
    assert _should_start_completion_on_space("/watch friend ")
    assert _should_start_completion_on_space("/watch group ")
    assert _should_start_completion_on_space("/export ")
    assert _should_start_completion_on_space("/export friend ")
    assert _should_start_completion_on_space("/export group ")
    assert _should_start_completion_on_space("/friends ")
    assert _should_start_completion_on_space("/groups ")


def test_completion_followup_chains_command_and_kind_completion() -> None:
    assert _completion_followup("/watch") == "space_then_complete"
    assert _completion_followup("/export friend") == "space_then_complete"
    assert _completion_followup("/export friend 菜鸡") == "space_then_complete"
    assert _completion_followup("/export friend 菜鸡 @final_content") == "space_then_complete"
    assert _completion_followup("/export friend 菜鸡 2026-03-07_00-00-00") == "same_token_complete"
    assert _completion_followup("/watch friend 菜鸡") == "cancel"


def test_completion_followup_cancels_after_terminal_export_tokens() -> None:
    assert (
        _completion_followup(
            "/export friend 菜鸡 @final_content @earliest_content asJSONL",
            accepted_text="asJSONL",
        )
        == "cancel"
    )
    assert (
        _completion_followup(
            "/export friend 菜鸡 data_count=",
            accepted_text="data_count=",
        )
        == "cancel"
    )


def test_completion_followup_keeps_first_item_unselected_for_context_popups() -> None:
    assert _should_select_first_completion("/watch") is False
    assert _should_select_first_completion("/watch friend") is False
    assert _should_select_first_completion("/export friend 菜鸡") is False
    assert _should_select_first_completion("/watch friend 菜") is True
