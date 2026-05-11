from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from pathlib import Path

from ..models import EXPORT_TIMEZONE, NormalizedMessage, NormalizedSnapshot
from ..paths import build_timestamp_token


def _format_dt(value: datetime) -> str:
    return value.astimezone(EXPORT_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _chat_type_label(value: str) -> str:
    return "群聊" if value == "group" else "私聊"


def _display_content(message: NormalizedMessage) -> str:
    parts: list[str] = []
    if message.reply_to:
        preview = message.reply_to.preview_text or "原消息"
        parts.append(f"[回复 : {preview}]")
    for segment in message.segments:
        if segment.type == "text" and segment.text:
            parts.append(segment.text)
        elif segment.type == "image":
            parts.append(f"[图片: {segment.file_name or 'image.jpg'}]")
        elif segment.type == "file":
            parts.append(f"[文件: {segment.file_name or 'uploaded_file'}]")
        elif segment.type == "speech":
            parts.append("[语音]")
        elif segment.type == "emoji":
            parts.append(f"[表情{segment.emoji_id}]")
        elif segment.type == "sticker":
            parts.append(segment.summary or "[表情包]")
        elif segment.type == "video":
            parts.append("[视频]")
        elif segment.type == "forward":
            preview = str(
                segment.extra.get("preview_text") or segment.summary or "聊天记录"
            ).strip()
            parts.append(f"[转发聊天记录: {preview}]")
            detailed_text = str(segment.extra.get("detailed_text") or "").strip()
            if detailed_text:
                parts.append(detailed_text)
        elif segment.type == "share":
            title = segment.summary or "分享卡片"
            desc = str(segment.extra.get("desc") or "").strip()
            if desc:
                parts.append(f"[分享: {title}] {desc}")
            else:
                parts.append(f"[分享: {title}]")
        elif segment.type == "system":
            parts.append(segment.text or segment.summary or "[系统消息]")
        elif segment.type == "unsupported":
            parts.append(segment.token or "[unsupported]")
    return "\n".join(parts).strip()


def _resource_lines(message: NormalizedMessage) -> list[str]:
    lines: list[str] = []
    for segment in message.segments:
        if segment.type == "image" and segment.file_name:
            lines.append(f"  - image: {segment.file_name}")
        elif segment.type == "file" and segment.file_name:
            lines.append(f"  - file: {segment.file_name}")
        elif segment.type == "speech" and segment.file_name:
            lines.append(f"  - audio: {segment.file_name}")
    return lines


def render_txt(snapshot: NormalizedSnapshot) -> str:
    return "".join(_iter_txt_chunks(snapshot))


def _iter_txt_chunks(snapshot: NormalizedSnapshot):
    lines: list[str] = [
        "[QQ Data Exporter / NapCatQQ]",
        "",
        "===============================================",
        "           QQ聊天记录导出文件",
        "===============================================",
        "",
        f"聊天名称: {snapshot.chat_name or snapshot.chat_id}",
        f"聊天类型: {_chat_type_label(snapshot.chat_type)}",
        f"{'群ID' if snapshot.chat_type == 'group' else '好友ID'}: {snapshot.chat_id}",
        f"导出时间: {_format_dt(snapshot.exported_at)}",
        f"消息总数: {len(snapshot.messages)}",
    ]
    if snapshot.messages:
        start = datetime.fromisoformat(snapshot.messages[0].timestamp_iso)
        end = datetime.fromisoformat(snapshot.messages[-1].timestamp_iso)
        lines.append(f"时间范围: {_format_dt(start)} - {_format_dt(end)}")
    else:
        lines.append("时间范围: -")
    lines.append("")

    yield "\n".join(lines).strip() + "\n"

    for message in snapshot.messages:
        message_lines = [
            "",
            f"{message.sender_name or message.sender_id}:",
            f"发送者ID: {message.sender_id}",
            f"时间: {_format_dt(datetime.fromisoformat(message.timestamp_iso))}",
            f"内容: {_display_content(message)}",
        ]
        resource_lines = _resource_lines(message)
        if resource_lines:
            message_lines.append(f"资源: {len(resource_lines)} 个文件")
            message_lines.extend(resource_lines)
        if message.reply_to:
            message_lines.append(f"回复:  - {message.reply_to.preview_text or '原消息'}")
        yield "\n".join(message_lines) + "\n"


def write_txt(snapshot: NormalizedSnapshot, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(
        f".{output_path.stem}.{build_timestamp_token(include_pid=True)}{output_path.suffix}.tmp"
    )
    try:
        with temp_path.open("w", encoding="utf-8", newline="") as handle:
            for chunk in _iter_txt_chunks(snapshot):
                handle.write(chunk)
        temp_path.replace(output_path)
        return output_path
    finally:
        with suppress(OSError):
            temp_path.unlink()
