from __future__ import annotations

from datetime import datetime, timedelta

from qq_data_core import (
    EXPORT_TIME_FORMAT,
    TimeExpressionError,
    format_export_datetime,
    parse_time_expression,
    resolve_time_expression,
    roll_explicit_datetime_literal,
)
from qq_data_core.models import EXPORT_TIMEZONE


def test_parse_and_resolve_special_time_expression_with_offsets() -> None:
    expression = parse_time_expression("@final_content-7d-5h-30s")
    final = datetime(2026, 3, 7, 12, 0, 0, tzinfo=EXPORT_TIMEZONE)

    resolved = resolve_time_expression(
        expression,
        earliest_content_at=None,
        final_content_at=final,
    )

    assert resolved == final - timedelta(days=7, hours=5, seconds=30)


def test_roll_explicit_datetime_literal_by_cursor_field() -> None:
    rolled = roll_explicit_datetime_literal(
        "2026-03-07_12-30-45",
        cursor_index=14,
        delta=1,
    )

    assert rolled == "2026-03-07_12-31-45"


def test_parse_time_expression_rejects_invalid_input() -> None:
    try:
        parse_time_expression("2026/03/07 12:30:45")
    except TimeExpressionError:
        pass
    else:
        raise AssertionError("expected invalid time expression to fail")


def test_format_export_datetime_uses_expected_layout() -> None:
    value = datetime(2026, 3, 7, 12, 30, 45, tzinfo=EXPORT_TIMEZONE)
    assert format_export_datetime(value) == value.strftime(EXPORT_TIME_FORMAT)
