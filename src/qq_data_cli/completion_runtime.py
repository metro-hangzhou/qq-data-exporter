from __future__ import annotations

from typing import Any


def completion_application_is_noop(buffer: Any, completion: Any) -> bool:
    text = str(getattr(buffer, "text", ""))
    cursor_position = int(getattr(buffer, "cursor_position", 0) or 0)
    completion_text = str(getattr(completion, "text", ""))
    start_position = int(getattr(completion, "start_position", 0) or 0)

    before = text[:cursor_position]
    after = text[cursor_position:]
    if start_position < 0:
        delete_count = min(len(before), -start_position)
        before = before[:-delete_count]

    new_text = before + completion_text + after
    new_cursor_position = len(before) + len(completion_text)
    return new_text == text and new_cursor_position == cursor_position
