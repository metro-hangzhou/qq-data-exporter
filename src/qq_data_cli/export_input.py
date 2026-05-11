from __future__ import annotations

import re
from typing import Callable

from prompt_toolkit.lexers import Lexer
from prompt_toolkit.layout.processors import Processor, Transformation, TransformationInput

from qq_data_core import roll_explicit_datetime_literal

_DATE_TOKEN_RE = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}")
_EXPORT_OPTIONS_WITH_VALUE = {"--format", "--out", "--limit", "--data-count", "--timeout", "--poll"}
_FORMAT_ALIASES = {"astxt", "asjsonl"}
_RIGHT_FIELD_JUMPS = {
    4: 7,
    7: 10,
    10: 13,
    13: 16,
    16: 19,
}
_LEFT_FIELD_JUMPS = {
    5: 4,
    8: 7,
    11: 10,
    14: 13,
    17: 16,
}


def _iter_token_spans(text: str):
    length = len(text)
    position = 0
    while position < length:
        while position < length and text[position].isspace():
            position += 1
        if position >= length:
            break
        start = position
        in_quote: str | None = None
        while position < length:
            char = text[position]
            if in_quote is not None:
                if char == "\\" and position + 1 < length:
                    position += 2
                    continue
                if char == in_quote:
                    in_quote = None
                position += 1
                continue
            if char in {"'", '"'}:
                in_quote = char
                position += 1
                continue
            if char.isspace():
                break
            position += 1
        end = position
        yield start, end, text[start:end]


def _iter_export_date_token_spans(text: str):
    if not text.lstrip().startswith("/export"):
        return
    tokens = list(_iter_token_spans(text))
    if not tokens:
        return

    remaining_target_tokens = 0
    for index, (start, end, token) in enumerate(tokens[1:], start=1):
        lowered = token.casefold()
        if remaining_target_tokens > 0:
            remaining_target_tokens -= 1
            continue
        if token.startswith("--"):
            option_name, separator, _option_value = token.partition("=")
            if not separator and option_name.casefold() in _EXPORT_OPTIONS_WITH_VALUE:
                remaining_target_tokens = 1
            continue
        if index == 1 and lowered in {"group", "friend"}:
            remaining_target_tokens = 1
            continue
        if index == 1 and (
            lowered.startswith("group_asbatch=") or lowered.startswith("friend_asbatch=")
        ):
            continue
        if lowered in _FORMAT_ALIASES:
            continue
        if _DATE_TOKEN_RE.fullmatch(token):
            yield start, end, token


class ExportCommandLexer(Lexer):
    def lex_document(self, document) -> Callable[[int], list[tuple[str, str]]]:
        line = document.lines[0] if document.lines else ""
        fragments = _highlight_export_date_tokens(line)

        def get_line(lineno: int) -> list[tuple[str, str]]:
            if lineno != 0:
                return []
            return fragments

        return get_line


class ExportDateDisplayProcessor(Processor):
    def apply_transformation(self, transformation_input: TransformationInput) -> Transformation:
        if transformation_input.lineno != 0:
            return Transformation(transformation_input.fragments)

        text = transformation_input.document.lines[0] if transformation_input.document.lines else ""
        if not text.lstrip().startswith("/export"):
            return Transformation(transformation_input.fragments)

        fragments, source_to_display_map = _build_export_display_fragments(text)
        display_length = sum(len(fragment_text) for _, fragment_text in fragments)
        display_to_source_map = _build_display_to_source_map(source_to_display_map, display_length)

        def source_to_display(position: int) -> int:
            position = min(max(0, position), len(source_to_display_map) - 1)
            return source_to_display_map[position]

        def display_to_source(position: int) -> int:
            position = min(max(0, position), len(display_to_source_map) - 1)
            return display_to_source_map[position]

        return Transformation(
            fragments=fragments,
            source_to_display=source_to_display,
            display_to_source=display_to_source,
        )


def roll_export_date_token(text: str, *, cursor_position: int, delta: int) -> tuple[str, int] | None:
    token_range = find_export_date_token_range(text, cursor_position)
    if token_range is None:
        return None

    start, end = token_range
    token = text[start:end]
    rolled = roll_explicit_datetime_literal(token, cursor_index=cursor_position - start, delta=delta)
    updated = text[:start] + rolled + text[end:]
    new_cursor_position = start + min(len(rolled), max(0, cursor_position - start))
    return updated, new_cursor_position


def find_export_date_token_range(text: str, cursor_position: int) -> tuple[int, int] | None:
    if not text.lstrip().startswith("/export"):
        return None
    for start, end, _token in _iter_export_date_token_spans(text):
        if start <= cursor_position <= end:
            return start, end
    return None


def move_export_date_cursor(
    text: str,
    *,
    cursor_position: int,
    direction: str,
) -> int | None:
    token_range = find_export_date_token_range(text, cursor_position)
    if token_range is None:
        return None

    start, _ = token_range
    token_cursor = cursor_position - start
    if direction == "right":
        target = _RIGHT_FIELD_JUMPS.get(token_cursor)
    elif direction == "left":
        target = _LEFT_FIELD_JUMPS.get(token_cursor)
    else:
        raise ValueError("direction must be left or right")

    if target is None:
        return None
    return start + target


def render_export_date_literal_display(value: str) -> str:
    if not _DATE_TOKEN_RE.fullmatch(value):
        return value
    return (
        f"{value[0:4]}y-"
        f"{value[5:7]}mo-"
        f"{value[8:10]}d_"
        f"{value[11:13]}h_"
        f"{value[14:16]}m_"
        f"{value[17:19]}s"
    )


def _highlight_export_date_tokens(text: str) -> list[tuple[str, str]]:
    if not text:
        return []
    if not text.lstrip().startswith("/export"):
        return [("", text)]

    fragments: list[tuple[str, str]] = []
    position = 0
    for start, end, token in _iter_export_date_token_spans(text):
        if start > position:
            fragments.append(("", text[position:start]))
        fragments.append(("class:export-date-literal", token))
        position = end
    if position < len(text):
        fragments.append(("", text[position:]))
    return fragments


def _build_export_display_fragments(text: str) -> tuple[list[tuple[str, str]], list[int]]:
    fragments: list[tuple[str, str]] = []
    source_to_display = [0] * (len(text) + 1)
    source_position = 0
    display_position = 0

    for start, end, token in _iter_export_date_token_spans(text):
        if start < source_position:
            continue

        if start > source_position:
            plain = text[source_position:start]
            fragments.append(("", plain))
            for _ in plain:
                source_to_display[source_position] = display_position
                source_position += 1
                display_position += 1
                source_to_display[source_position] = display_position

        token_fragments, token_map = _build_date_token_display(token)
        fragments.extend(token_fragments)
        for offset, mapped_position in enumerate(token_map):
            source_to_display[start + offset] = display_position + mapped_position
        source_position = end
        display_position += len(render_export_date_literal_display(token))

    if source_position < len(text):
        plain = text[source_position:]
        fragments.append(("", plain))
        for _ in plain:
            source_to_display[source_position] = display_position
            source_position += 1
            display_position += 1
            source_to_display[source_position] = display_position

    source_to_display[len(text)] = display_position
    return fragments, source_to_display


def _build_date_token_display(token: str) -> tuple[list[tuple[str, str]], list[int]]:
    rendered = render_export_date_literal_display(token)
    fragments = [("class:export-date-literal", rendered)]
    source_to_display = [0] * (len(token) + 1)
    source_to_display[0] = 0
    source_to_display[1] = 1
    source_to_display[2] = 2
    source_to_display[3] = 3
    source_to_display[4] = 4
    source_to_display[5] = 6
    source_to_display[6] = 7
    source_to_display[7] = 8
    source_to_display[8] = 11
    source_to_display[9] = 12
    source_to_display[10] = 13
    source_to_display[11] = 15
    source_to_display[12] = 16
    source_to_display[13] = 17
    source_to_display[14] = 19
    source_to_display[15] = 20
    source_to_display[16] = 21
    source_to_display[17] = 23
    source_to_display[18] = 24
    source_to_display[19] = len(rendered)
    return fragments, source_to_display


def _build_display_to_source_map(source_to_display: list[int], display_length: int) -> list[int]:
    display_to_source = [0] * (display_length + 1)
    source_index = 0
    for display_position in range(display_length + 1):
        while (
            source_index < len(source_to_display) - 1
            and source_to_display[source_index] < display_position
        ):
            source_index += 1
        display_to_source[display_position] = source_index
    return display_to_source
