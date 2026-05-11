from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .models import EXPORT_TIMEZONE

EXPORT_TIME_FORMAT = "%Y-%m-%d_%H-%M-%S"
SPECIAL_TIME_EXPRESSIONS = ["@final_content", "@earliest_content"]
_DATE_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
_PARSEABLE_DATE_LITERAL_RE = re.compile(
    r"^(?P<year>\d{4})-"
    r"(?P<month>\d{1,2})-"
    r"(?P<day>\d{1,2})_"
    r"(?P<hour>\d{1,2})-"
    r"(?P<minute>\d{1,2})-"
    r"(?P<second>\d{1,2})$"
)
_OFFSET_RE = re.compile(r"([+-])(\d+)([wdhms])")


class TimeExpressionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedTimeExpression:
    raw: str
    base_kind: Literal["literal", "final_content", "earliest_content"]
    literal: datetime | None = None
    offset: timedelta = timedelta(0)


def is_explicit_datetime_literal(value: str) -> bool:
    return bool(_DATE_LITERAL_RE.fullmatch(value.strip()))


def is_parseable_datetime_literal(value: str) -> bool:
    return bool(_PARSEABLE_DATE_LITERAL_RE.fullmatch(value.strip()))


def format_export_datetime(value: datetime) -> str:
    return value.astimezone(EXPORT_TIMEZONE).strftime(EXPORT_TIME_FORMAT)


def parse_time_expression(value: str) -> ParsedTimeExpression:
    raw = value.strip()
    if not raw:
        raise TimeExpressionError("Missing time expression")

    if is_parseable_datetime_literal(raw):
        return ParsedTimeExpression(
            raw=raw,
            base_kind="literal",
            literal=_parse_datetime_literal(raw),
        )

    for symbol, kind in (
        ("@final_content", "final_content"),
        ("@earliest_content", "earliest_content"),
    ):
        if raw == symbol:
            return ParsedTimeExpression(raw=raw, base_kind=kind, offset=timedelta(0))
        if raw.startswith(symbol + "+") or raw.startswith(symbol + "-"):
            offset = _parse_offset_chain(raw[len(symbol):], raw=raw)
            return ParsedTimeExpression(raw=raw, base_kind=kind, offset=offset)

    raise TimeExpressionError(
        "Time expression must be YYYY-MM-DD_HH-MM-SS "
        "(single-digit month/day/time fields are also accepted), "
        "@final_content, or @earliest_content with optional +/- offsets."
    )


def resolve_time_expression(
    expression: ParsedTimeExpression,
    *,
    earliest_content_at: datetime | None,
    final_content_at: datetime | None,
) -> datetime:
    if expression.base_kind == "literal":
        assert expression.literal is not None
        return expression.literal.astimezone(EXPORT_TIMEZONE)

    if expression.base_kind == "earliest_content":
        if earliest_content_at is None:
            raise TimeExpressionError("@earliest_content is unavailable because the chat has no messages.")
        return earliest_content_at.astimezone(EXPORT_TIMEZONE) + expression.offset

    if final_content_at is None:
        raise TimeExpressionError("@final_content is unavailable because the chat has no messages.")
    return final_content_at.astimezone(EXPORT_TIMEZONE) + expression.offset


def roll_explicit_datetime_literal(value: str, *, cursor_index: int, delta: int) -> str:
    if not is_explicit_datetime_literal(value):
        raise TimeExpressionError(f"{value!r} is not an explicit datetime literal.")

    dt = datetime.strptime(value, EXPORT_TIME_FORMAT).replace(tzinfo=EXPORT_TIMEZONE)
    component = _component_for_cursor(cursor_index)
    if component == "year":
        dt = _replace_year(dt, delta)
    elif component == "month":
        dt = _replace_month(dt, delta)
    elif component == "day":
        dt += timedelta(days=delta)
    elif component == "hour":
        dt += timedelta(hours=delta)
    elif component == "minute":
        dt += timedelta(minutes=delta)
    else:
        dt += timedelta(seconds=delta)
    return format_export_datetime(dt)


def _parse_offset_chain(value: str, *, raw: str) -> timedelta:
    if not value:
        return timedelta(0)

    position = 0
    total = timedelta(0)
    while position < len(value):
        match = _OFFSET_RE.match(value, position)
        if match is None:
            raise TimeExpressionError(f"Invalid time offset expression {raw!r}")
        sign = -1 if match.group(1) == "-" else 1
        amount = int(match.group(2))
        unit = match.group(3)
        total += sign * _offset_timedelta(amount, unit)
        position = match.end()
    return total


def _offset_timedelta(amount: int, unit: str) -> timedelta:
    if unit == "w":
        return timedelta(weeks=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "s":
        return timedelta(seconds=amount)
    raise TimeExpressionError(f"Unsupported time offset unit {unit!r}")


def _parse_datetime_literal(value: str) -> datetime:
    match = _PARSEABLE_DATE_LITERAL_RE.fullmatch(value.strip())
    if match is None:
        raise TimeExpressionError(f"Invalid datetime literal {value!r}")
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=EXPORT_TIMEZONE,
        )
    except ValueError as exc:
        raise TimeExpressionError(str(exc)) from exc


def _component_for_cursor(cursor_index: int) -> str:
    if cursor_index <= 4:
        return "year"
    if cursor_index <= 7:
        return "month"
    if cursor_index <= 10:
        return "day"
    if cursor_index <= 13:
        return "hour"
    if cursor_index <= 16:
        return "minute"
    return "second"


def _replace_year(value: datetime, delta: int) -> datetime:
    target_year = value.year + delta
    max_day = calendar.monthrange(target_year, value.month)[1]
    return value.replace(year=target_year, day=min(value.day, max_day))


def _replace_month(value: datetime, delta: int) -> datetime:
    zero_based_month = value.month - 1 + delta
    target_year = value.year + zero_based_month // 12
    target_month = zero_based_month % 12 + 1
    max_day = calendar.monthrange(target_year, target_month)[1]
    return value.replace(year=target_year, month=target_month, day=min(value.day, max_day))
