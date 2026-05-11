from __future__ import annotations

from qq_data_cli.export_input import (
    find_export_date_token_range,
    move_export_date_cursor,
    render_export_date_literal_display,
    roll_export_date_token,
)


def test_roll_export_date_token_updates_only_active_token() -> None:
    text = "/export friend 菜鸡 2026-03-07_12-30-45 @final_content"
    cursor = text.index("30-45") + 1

    updated = roll_export_date_token(text, cursor_position=cursor, delta=1)

    assert updated is not None
    assert updated[0] == "/export friend 菜鸡 2026-03-07_12-31-45 @final_content"


def test_find_export_date_token_range_ignores_non_export_commands() -> None:
    assert find_export_date_token_range("/watch friend 菜鸡 2026-03-07_12-30-45", 25) is None


def test_render_export_date_literal_display_adds_unit_suffixes() -> None:
    assert (
        render_export_date_literal_display("2026-03-01_00-00-00")
        == "2026y-03mo-01d_00h_00m_00s"
    )


def test_move_export_date_cursor_skips_over_unit_boundaries() -> None:
    text = "/export friend 菜鸡 2026-03-01_00-00-00"
    token_start = text.index("2026-03-01_00-00-00")

    assert move_export_date_cursor(
        text,
        cursor_position=token_start + 4,
        direction="right",
    ) == token_start + 7
    assert move_export_date_cursor(
        text,
        cursor_position=token_start + 5,
        direction="left",
    ) == token_start + 4
