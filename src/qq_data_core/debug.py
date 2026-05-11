from __future__ import annotations

from .models import NormalizedMessage


def render_debug_content(message: NormalizedMessage) -> str:
    parts: list[str] = []
    for segment in message.segments:
        if segment.type == "text":
            text = (segment.text or "").strip()
            if text:
                parts.append(text)
            continue
        if segment.type == "image":
            parts.append("[image]")
            continue
        if segment.type in {"emoji", "sticker"}:
            parts.append("[meme or emoji]")
            continue
        if segment.type == "speech":
            parts.append("[speech audio]")
            continue
        if segment.type == "file":
            parts.append("[uploaded file]")
            continue
        if segment.type == "forward":
            parts.append("[forward message]")
            continue
        if segment.type == "share":
            title = segment.summary or "share"
            parts.append(f"[share:{title}]")
            continue
        if segment.type == "system":
            text = (segment.text or segment.summary or "").strip()
            if text:
                parts.append(text)
            continue
        if segment.type == "reply":
            continue
        if segment.token:
            parts.append(segment.token)

    return " ".join(part for part in parts if part).strip()


def render_watch_line(message: NormalizedMessage) -> str:
    channel_label = "group" if message.chat_type == "group" else "private"
    content = render_debug_content(message) or "[empty]"
    return (
        f"{channel_label}={message.chat_id} "
        f"sender={message.sender_id} "
        f"time={message.timestamp_iso} "
        f"content={content}"
    )
