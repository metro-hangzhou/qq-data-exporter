from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import re
import shlex
from typing import TYPE_CHECKING

from qq_data_core import ExportProfile
from qq_data_core import TimeExpressionError, parse_time_expression, resolve_time_expression
from qq_data_core import normalize_export_format

if TYPE_CHECKING:
    from qq_data_integrations.napcat.models import ChatHistoryBounds

FORMAT_MARKERS = {
    "astxt": "txt",
    "asjsonl": "jsonl",
}

EXPORT_COMMAND_PROFILES: dict[str, ExportProfile] = {
    "/export": "all",
    "/export_onlytext": "only_text",
    "/export_textimage": "text_image",
    "/export_textimageemoji": "text_image_emoji",
}
_BATCH_TARGET_RE = re.compile(r"^(group|friend)_asbatch=(.+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExportIntervalSpec:
    start_token: str
    end_token: str


@dataclass(frozen=True, slots=True)
class ParsedExportCommand:
    chat_type: str | None
    target_query: str | None
    batch_target_queries: tuple[str, ...]
    interval: ExportIntervalSpec | None
    fmt: str
    out_path: Path | None
    limit: int
    data_count: int | None
    profile: ExportProfile
    include_raw: bool
    refresh: bool
    strict_missing: str | None


def parse_root_export_command(
    command: str,
    positionals: list[str],
    options: dict[str, object],
    *,
    default_limit: int,
) -> ParsedExportCommand:
    chat_type, target_query, batch_target_queries, consumed_positionals = _parse_target_selection(positionals)
    if chat_type is None:
        raise ValueError(
            "Usage: /export group|friend <name-or-id> [<time-a> <time-b>] "
            "[asTXT|asJSONL] [--format jsonl|txt] [--out PATH] [--limit N] "
            "or /export group_asBatch=<name1,name2,...> "
            "or /export friend_asBatch=<name1,name2,...>"
        )
    if batch_target_queries and options.get("out"):
        out_path = Path(str(options["out"]))
        if out_path.suffix:
            raise ValueError("Batch export --out must point to a directory, not a single file.")

    interval_tokens, alias_fmt = _split_format_alias(positionals[consumed_positionals:])
    interval_tokens, inline_data_count = _extract_data_count_token(interval_tokens)
    interval = _parse_interval(interval_tokens)
    return ParsedExportCommand(
        chat_type=chat_type,
        target_query=target_query,
        batch_target_queries=batch_target_queries,
        interval=interval,
        fmt=normalize_export_format(alias_fmt or str(options.get("format") or "").lower() or "jsonl"),
        out_path=Path(str(options["out"])) if options.get("out") else None,
        limit=_parse_limit(options.get("limit"), default_limit=default_limit),
        data_count=_parse_optional_int(options.get("data-count"), inline_data_count),
        profile=_parse_export_profile(command),
        include_raw=bool(options.get("include-raw")),
        refresh=bool(options.get("refresh")),
        strict_missing=str(options.get("strict-missing") or "").strip() or None,
    )


def parse_watch_export_command(
    command: str,
    positionals: list[str],
    options: dict[str, object],
    *,
    default_limit: int,
) -> ParsedExportCommand:
    interval_tokens, alias_fmt = _split_format_alias(positionals)
    interval_tokens, inline_data_count = _extract_data_count_token(interval_tokens)
    interval = _parse_interval(interval_tokens)
    return ParsedExportCommand(
        chat_type=None,
        target_query=None,
        batch_target_queries=(),
        interval=interval,
        fmt=normalize_export_format(alias_fmt or str(options.get("format") or "").lower() or "jsonl"),
        out_path=Path(str(options["out"])) if options.get("out") else None,
        limit=_parse_limit(options.get("limit"), default_limit=default_limit),
        data_count=_parse_optional_int(options.get("data-count"), inline_data_count),
        profile=_parse_export_profile(command),
        include_raw=bool(options.get("include-raw")),
        refresh=False,
        strict_missing=str(options.get("strict-missing") or "").strip() or None,
    )


def _parse_target_selection(
    positionals: list[str],
) -> tuple[str | None, str | None, tuple[str, ...], int]:
    if not positionals:
        return None, None, (), 0

    batch_match = _BATCH_TARGET_RE.match(positionals[0])
    if batch_match is not None:
        batch_type = batch_match.group(1).lower()
        raw_batch_value = batch_match.group(2)
        consumed = 1
        while consumed < len(positionals):
            token = positionals[consumed]
            if _is_batch_suffix_terminator(token):
                break
            raw_batch_value += f" {token}"
            consumed += 1
        targets = _parse_batch_queries(raw_batch_value)
        if not targets:
            raise ValueError("Batch export target list cannot be empty.")
        return batch_type, None, targets, consumed

    if len(positionals) < 2:
        return None, None, (), 0
    return positionals[0], positionals[1], (), 2


def _parse_batch_queries(raw: str) -> tuple[str, ...]:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escape = False
    for char in raw:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\" and quote is not None:
            escape = True
            current.append(char)
            continue
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            current.append(char)
            continue
        if char == ",":
            value = "".join(current).strip()
            if value:
                parts.append(_unquote_batch_query(value))
            current = []
            continue
        current.append(char)
    value = "".join(current).strip()
    if value:
        parts.append(_unquote_batch_query(value))
    return tuple(part for part in parts if part)


def _is_batch_suffix_terminator(token: str) -> bool:
    lowered = token.casefold().strip()
    if not lowered:
        return False
    if lowered in FORMAT_MARKERS:
        return True
    if lowered.startswith("data_count=") or lowered.startswith("datacount="):
        return True
    try:
        parse_time_expression(token)
    except TimeExpressionError:
        return False
    return True


def _unquote_batch_query(value: str) -> str:
    try:
        tokens = shlex.split(value)
    except ValueError:
        return value.strip()
    if len(tokens) == 1:
        return tokens[0].strip()
    return value.strip()


def resolve_interval(
    interval: ExportIntervalSpec,
    *,
    bounds: ChatHistoryBounds | None,
) -> tuple[datetime, datetime]:
    start_dt = resolve_time_expression(
        parse_time_expression(interval.start_token),
        earliest_content_at=bounds.earliest_content_at if bounds else None,
        final_content_at=bounds.final_content_at if bounds else None,
    )
    end_dt = resolve_time_expression(
        parse_time_expression(interval.end_token),
        earliest_content_at=bounds.earliest_content_at if bounds else None,
        final_content_at=bounds.final_content_at if bounds else None,
    )
    return (start_dt, end_dt)


def interval_needs_history_bounds(interval: ExportIntervalSpec) -> bool:
    return any(
        parse_time_expression(token).base_kind != "literal"
        for token in (interval.start_token, interval.end_token)
    )


def interval_special_kinds(interval: ExportIntervalSpec) -> set[str]:
    return {
        expression.base_kind
        for expression in (
            parse_time_expression(interval.start_token),
            parse_time_expression(interval.end_token),
        )
        if expression.base_kind != "literal"
    }


def interval_is_full_history(interval: ExportIntervalSpec) -> bool:
    start = parse_time_expression(interval.start_token)
    end = parse_time_expression(interval.end_token)
    return (
        {start.base_kind, end.base_kind} == {"final_content", "earliest_content"}
        and start.offset == timedelta(0)
        and end.offset == timedelta(0)
    )


def _parse_interval(tokens: list[str]) -> ExportIntervalSpec | None:
    if not tokens:
        return None
    if len(tokens) != 2:
        raise ValueError("Export time range must provide exactly two time expressions.")
    try:
        parse_time_expression(tokens[0])
        parse_time_expression(tokens[1])
    except TimeExpressionError as exc:
        raise ValueError(str(exc)) from exc
    return ExportIntervalSpec(start_token=tokens[0], end_token=tokens[1])


def _split_format_alias(tokens: list[str]) -> tuple[list[str], str | None]:
    if not tokens:
        return tokens, None
    alias = FORMAT_MARKERS.get(tokens[-1].strip().casefold())
    if alias is None:
        return tokens, None
    return tokens[:-1], alias


def _parse_limit(value: object, *, default_limit: int) -> int:
    if value in {None, False}:
        return default_limit
    return int(str(value))


def _parse_optional_int(primary: object, fallback: int | None) -> int | None:
    if primary not in {None, False}:
        return int(str(primary))
    return fallback


def _extract_data_count_token(tokens: list[str]) -> tuple[list[str], int | None]:
    remaining: list[str] = []
    data_count: int | None = None
    for token in tokens:
        lowered = token.casefold()
        if lowered.startswith("data_count=") or lowered.startswith("datacount="):
            _, value = token.split("=", 1)
            data_count = int(value)
            continue
        remaining.append(token)
    return remaining, data_count


def _parse_export_profile(command: str) -> ExportProfile:
    lowered = command.casefold()
    if lowered not in EXPORT_COMMAND_PROFILES:
        raise ValueError(f"Unsupported export command: {command}")
    return EXPORT_COMMAND_PROFILES[lowered]
