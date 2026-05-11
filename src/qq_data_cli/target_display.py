from __future__ import annotations

import unicodedata
from typing import Any

from qq_data_integrations.napcat import ChatTarget

_TERMINAL_SPACE_LIKE_CHARS = {
    "\u00a0",
    "\u1680",
    "\u2000",
    "\u2001",
    "\u2002",
    "\u2003",
    "\u2004",
    "\u2005",
    "\u2006",
    "\u2007",
    "\u2008",
    "\u2009",
    "\u200a",
    "\u202f",
    "\u205f",
    "\u3000",
    "\u3164",
    "\u2800",
}


def terminal_safe_text(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""

    sanitized: list[str] = []
    for char in text:
        if char in {"\r", "\n"}:
            sanitized.append(" ")
            continue
        if char == "\t":
            sanitized.append("    ")
            continue
        if char in _TERMINAL_SPACE_LIKE_CHARS:
            sanitized.append(" ")
            continue
        if unicodedata.category(char).startswith("C"):
            continue
        sanitized.append(char)
    return "".join(sanitized)


def is_blank_like_text(value: Any) -> bool:
    raw = str(value or "")
    if not raw:
        return False
    return terminal_safe_text(raw).strip() == ""


def format_display_name(value: Any, *, kind: str = "名称") -> str:
    raw = str(value or "")
    if not raw:
        return ""
    safe = terminal_safe_text(raw)
    if safe.strip():
        return safe
    return f"<空白{kind}>"


def format_target_name(target: ChatTarget) -> str:
    return format_display_name(target.name, kind="昵称")


def format_target_remark(target: ChatTarget) -> str:
    if not target.remark:
        return ""
    return format_display_name(target.remark, kind="备注")


def format_target_label(target: ChatTarget) -> str:
    display_name = format_display_name(target.display_name, kind="昵称")
    if display_name == target.chat_id:
        return target.chat_id
    return f"{display_name} ({target.chat_id})"
