from __future__ import annotations

from copy import deepcopy
import json
import re
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .models import (
    EXPORT_TIMEZONE,
    NormalizedMessage,
    NormalizedSegment,
    NormalizedSnapshot,
    ReplyRef,
    SourceChatSnapshot,
)

TEXT_ELEMENT = 1
PIC_ELEMENT = 2
FILE_ELEMENT = 3
PTT_ELEMENT = 4
FACE_ELEMENT = 6
REPLY_ELEMENT = 7
MARKET_FACE_ELEMENT = 11
GRAY_TIP_ELEMENT = 8
ARK_ELEMENT = 10
FORWARD_ELEMENT = 16

FORWARD_TOKEN = "[forward message]"
XML_TAG_RE = re.compile(r"<[^>]+>")
HEX_MD5_RE = re.compile(r"^[0-9a-fA-F]{32}$")
NON_DIGIT_RE = re.compile(r"\D+")


def _safe_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _message_raw(payload: dict[str, Any]) -> dict[str, Any]:
    raw_message = payload.get("rawMessage")
    if isinstance(raw_message, dict):
        return raw_message
    raw_message = payload.get("raw_message")
    if isinstance(raw_message, dict):
        return raw_message
    return {}


def _message_sender(payload: dict[str, Any]) -> dict[str, Any]:
    return _safe_mapping(payload.get("sender"))


def _parse_timestamp(
    payload: dict[str, Any],
    *,
    fallback_timestamp: datetime | None = None,
) -> datetime:
    raw_message = _message_raw(payload)
    timestamp = payload.get("timestamp")
    if isinstance(timestamp, str):
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=EXPORT_TIMEZONE)
        return parsed.astimezone(EXPORT_TIMEZONE)
    epoch_seconds = (
        payload.get("time")
        or raw_message.get("msgTime")
    )
    if epoch_seconds is None:
        return fallback_timestamp or datetime.now(EXPORT_TIMEZONE)
    return datetime.fromtimestamp(int(epoch_seconds), tz=EXPORT_TIMEZONE)


def _clean_text(value: str | None) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n")


def _clean_numeric_id(value: Any) -> str:
    return NON_DIGIT_RE.sub("", _clean_text(str(value or "")).strip())


def _infer_md5(*values: Any) -> str | None:
    for value in values:
        text = _clean_text(str(value or "")).strip()
        if not text:
            continue
        candidate = Path(text).stem if Path(text).suffix else text
        if HEX_MD5_RE.fullmatch(candidate):
            return candidate.lower()
    return None


def _safe_summary(value: str | None) -> str:
    return _clean_text(value).replace(",", "，").replace("\n", " ").strip()


def _basename_from_path(value: str | None) -> str | None:
    if not value:
        return None
    return Path(value).name or None


def _collapse_inline_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", _clean_text(value)).strip()


def _truncate_preview(value: str | None, *, max_length: int = 280) -> str:
    text = _collapse_inline_text(value)
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def _build_qq_avatar_url(uin: Any) -> str | None:
    numeric_id = _clean_numeric_id(uin)
    if not numeric_id:
        return None
    return f"https://q.qlogo.cn/headimg_dl?dst_uin={numeric_id}&spec=0&img_type=jpg"


def _build_marketface_remote(emoji_id: str | None) -> tuple[str | None, str | None]:
    value = _clean_text(emoji_id)
    if len(value) < 2:
        return None, None
    prefix = value[:2]
    return (
        f"https://gxh.vip.qq.com/club/item/parcel/item/{prefix}/{value}/raw300.gif",
        f"{prefix}-{value}.gif",
    )


def _safe_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_xml_root(value: str | None) -> ET.Element | None:
    text = _clean_text(value).strip()
    if not text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _strip_xml_tags(value: str | None) -> str:
    text = _clean_text(value).strip()
    if not text:
        return ""
    stripped = XML_TAG_RE.sub(" ", text)
    return _collapse_inline_text(unescape(stripped))


def _parse_forward_preview_xml(xml_content: str | None) -> dict[str, Any]:
    root = _parse_xml_root(xml_content)
    if root is None:
        fallback = _truncate_preview(_strip_xml_tags(xml_content))
        return {
            "title": "聊天记录",
            "preview_lines": [fallback] if fallback else [],
            "summary_text": None,
            "source_name": None,
            "forwarded_count": None,
            "preview_text": fallback,
        }

    item = root.find("item")
    title_lines: list[str] = []
    summary_text: str | None = None
    if item is not None:
        for title in item.findall("title"):
            text = _collapse_inline_text("".join(title.itertext()))
            if text:
                title_lines.append(text)
        summary_element = item.find("summary")
        if summary_element is not None:
            summary_text = (
                _collapse_inline_text("".join(summary_element.itertext())) or None
            )

    source_element = root.find("source")
    source_name = None
    if source_element is not None:
        source_name = (
            _collapse_inline_text(
                source_element.attrib.get("name") or "".join(source_element.itertext())
            )
            or None
        )

    title = title_lines[0] if title_lines else source_name or "聊天记录"
    preview_lines = title_lines[1:] if len(title_lines) > 1 else []
    forwarded_count = _safe_int(root.attrib.get("tSum"))

    preview_parts = [title]
    preview_parts.extend(preview_lines[:4])
    if summary_text:
        preview_parts.append(summary_text)
    preview_text = _truncate_preview(" | ".join(part for part in preview_parts if part))

    return {
        "title": title,
        "preview_lines": preview_lines,
        "summary_text": summary_text,
        "source_name": source_name,
        "forwarded_count": forwarded_count,
        "preview_text": preview_text,
    }


def _gray_tip_item_text(item: dict[str, Any]) -> str:
    item_type = str(item.get("type") or "").strip().lower()
    if item_type == "nor":
        return _collapse_inline_text(item.get("txt"))
    if item_type == "qq":
        return _collapse_inline_text(item.get("nm")) or _collapse_inline_text(
            item.get("uid")
        )
    return ""


def _parse_gray_tip_payload(gray_tip: dict[str, Any]) -> dict[str, Any]:
    json_tip = gray_tip.get("jsonGrayTipElement") or {}
    json_str = _clean_text(json_tip.get("jsonStr")).strip()
    recent_abstract = _collapse_inline_text(json_tip.get("recentAbstract")) or None
    items_text: list[str] = []
    parsed_json: dict[str, Any] | None = None
    if json_str:
        try:
            parsed_json = json.loads(json_str)
        except json.JSONDecodeError:
            parsed_json = None
    if isinstance(parsed_json, dict):
        for item in parsed_json.get("items") or []:
            if isinstance(item, dict):
                piece = _gray_tip_item_text(item)
                if piece:
                    items_text.append(piece)

    raw_text = "".join(items_text).strip()
    if not raw_text:
        raw_text = recent_abstract or ""
    if not raw_text:
        xml_param = json_tip.get("xmlToJsonParam") or {}
        raw_text = _strip_xml_tags(xml_param.get("content"))

    text = _truncate_preview(raw_text, max_length=240)
    return {
        "text": text or "[system message]",
        "busi_id": json_tip.get("busiId"),
        "sub_element_type": gray_tip.get("subElementType"),
        "recent_abstract": recent_abstract,
    }


def _parse_share_payload(raw_payload: str | dict[str, Any] | None) -> dict[str, Any]:
    parsed: dict[str, Any] | None = None
    if isinstance(raw_payload, dict):
        parsed = raw_payload
    elif isinstance(raw_payload, str):
        text = _clean_text(raw_payload).strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None

    if not isinstance(parsed, dict):
        return {
            "title": None,
            "desc": None,
            "tag": None,
            "url": None,
            "summary_text": "",
        }

    meta = parsed.get("meta") or {}
    news = meta.get("news") if isinstance(meta, dict) else None
    if not isinstance(news, dict):
        news = {}
    prompt = _collapse_inline_text(parsed.get("prompt"))
    title = (
        _collapse_inline_text(news.get("title"))
        or prompt.replace("[分享]", "").strip()
        or None
    )
    desc = _collapse_inline_text(news.get("desc")) or None
    tag = _collapse_inline_text(news.get("tag")) or None
    url = news.get("jumpUrl") or None
    summary_parts = [title, desc]
    if tag and tag not in {title, desc}:
        summary_parts.append(f"标签:{tag}")
    summary_text = _truncate_preview(
        " | ".join(part for part in summary_parts if part), max_length=240
    )
    return {
        "title": title,
        "desc": desc,
        "tag": tag,
        "url": url,
        "summary_text": summary_text,
        "prompt": prompt or None,
        "raw": parsed,
    }


def _content_text_for_segment(segment: NormalizedSegment) -> str:
    if segment.type in {"text", "system"}:
        return segment.text or ""
    if segment.type == "forward":
        return str(
            segment.extra.get("detailed_text")
            or segment.extra.get("preview_text")
            or segment.summary
            or ""
        ).strip()
    if segment.type == "share":
        parts = [
            segment.summary,
            str(segment.extra.get("desc") or "").strip() or None,
            str(segment.extra.get("tag") or "").strip() or None,
        ]
        return " ".join(part for part in parts if part).strip()
    return ""


def _segment_dump(segment: NormalizedSegment) -> dict[str, Any]:
    return segment.model_dump(mode="json")


def _forward_detail_depth(nodes: list[dict[str, Any]]) -> int:
    best = 1 if nodes else 0
    for node in nodes:
        for segment in node.get("segments") or []:
            if not isinstance(segment, dict) or segment.get("type") != "forward":
                continue
            extra = segment.get("extra") or {}
            child_depth = int(extra.get("forward_depth") or 1)
            best = max(best, child_depth + 1)
    return best


def _merge_parent_context(
    *,
    message: dict[str, Any],
    inherited_parent_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    local_context = _extract_onebot_parent_context(message)
    inherited = inherited_parent_context or {}
    return {
        "message_id_raw": local_context.get("message_id_raw")
        or inherited.get("message_id_raw"),
        "peer_uid": local_context.get("peer_uid") or inherited.get("peer_uid"),
        "chat_type_raw": local_context.get("chat_type_raw")
        if local_context.get("chat_type_raw") is not None
        else inherited.get("chat_type_raw"),
        "forward_element_id": local_context.get("forward_element_id")
        or inherited.get("forward_element_id")
        or inherited.get("element_id"),
    }


def _forward_node_timestamp_iso(
    raw_node: dict[str, Any],
    raw_nested_message: dict[str, Any],
) -> str | None:
    raw_timestamp = raw_node.get("timestamp")
    if isinstance(raw_timestamp, str):
        try:
            return datetime.fromisoformat(
                raw_timestamp.replace("Z", "+00:00")
            ).astimezone(EXPORT_TIMEZONE).isoformat()
        except ValueError:
            pass
    epoch_seconds = raw_node.get("time") or raw_nested_message.get("msgTime")
    if epoch_seconds in {None, ""}:
        return None
    try:
        return datetime.fromtimestamp(int(epoch_seconds), tz=EXPORT_TIMEZONE).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _forward_node_identity(
    *,
    raw_node: dict[str, Any],
    data: dict[str, Any],
    sender: dict[str, Any],
    raw_nested_message: dict[str, Any],
) -> dict[str, Any]:
    sender_id = _clean_text(
        data.get("user_id")
        or data.get("sender_id")
        or data.get("uin")
        or raw_node.get("user_id")
        or raw_node.get("sender_id")
        or sender.get("user_id")
        or sender.get("uin")
    ).strip()
    sender_name = (
        _clean_text(
            data.get("nickname")
            or data.get("name")
            or sender.get("nickname")
            or sender.get("card")
            or raw_node.get("nickname")
            or raw_node.get("sender_name")
        ).strip()
        or None
    )

    raw_sender_id = (
        _clean_text(raw_nested_message.get("senderUin")).strip()
        or sender_id
        or None
    )
    raw_sender_name = (
        _clean_text(
            raw_nested_message.get("sendMemberName")
            or raw_nested_message.get("sendRemarkName")
            or raw_nested_message.get("sendNickName")
        ).strip()
        or sender_name
    )
    avatar_url = (
        _clean_text(
            raw_node.get("avatar_url")
            or raw_node.get("avatarUrl")
            or raw_node.get("headUrl")
            or raw_node.get("head_url")
            or raw_node.get("avatar")
            or sender.get("avatar_url")
            or sender.get("avatarUrl")
            or sender.get("headUrl")
            or sender.get("head_url")
            or sender.get("avatar")
            or raw_nested_message.get("avatar")
            or raw_nested_message.get("headUrl")
        ).strip()
        or _build_qq_avatar_url(raw_sender_id or sender_id)
    )
    alias_sender_id = sender_id if sender_id and sender_id != raw_sender_id else None
    alias_sender_name = (
        sender_name if sender_name and sender_name != raw_sender_name else None
    )
    return {
        "sender_id": sender_id or raw_sender_id or None,
        "sender_name": sender_name or raw_sender_name,
        "raw_sender_id": raw_sender_id,
        "raw_sender_name": raw_sender_name,
        "alias_sender_id": alias_sender_id,
        "alias_sender_name": alias_sender_name,
        "avatar_url": avatar_url or None,
    }


def _normalize_forward_nodes(
    raw_nodes: Any,
    *,
    inherited_parent_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str, int, int]:
    if not isinstance(raw_nodes, list):
        return [], "", 0, 0

    normalized_nodes: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            continue
        if raw_node.get("type") == "node":
            data = raw_node.get("data") or {}
            raw_nested_message = _message_raw(data)
            nested_message = {
                "message": data.get("message") or data.get("content") or [],
            }
            (
                segments,
                image_file_names,
                uploaded_file_names,
                emoji_tokens,
                content_parts,
                text_content,
                _reply_to,
            ) = _normalize_onebot_segments(
                nested_message,
                inherited_parent_context=inherited_parent_context,
            )
            sender_identity = _forward_node_identity(
                raw_node=raw_node,
                data=data,
                sender={},
                raw_nested_message=raw_nested_message,
            )
        else:
            data = raw_node
            sender = raw_node.get("sender") or {}
            raw_nested_message = _message_raw(raw_node)
            if raw_nested_message:
                nested_message = {
                    "raw_message": raw_nested_message,
                    "message_id": raw_node.get("message_id"),
                    "message_seq": raw_node.get("message_seq"),
                    "user_id": raw_node.get("user_id"),
                    "sender": raw_node.get("sender"),
                }
                (
                    segments,
                    image_file_names,
                    uploaded_file_names,
                    emoji_tokens,
                    content_parts,
                    text_content,
                    _reply_to,
                ) = _normalize_exporter_elements(nested_message)
            else:
                nested_message = {"message": raw_node.get("message") or []}
                (
                    segments,
                    image_file_names,
                    uploaded_file_names,
                    emoji_tokens,
                    content_parts,
                    text_content,
                    _reply_to,
                ) = _normalize_onebot_segments(
                    nested_message,
                    inherited_parent_context=inherited_parent_context,
                )
            sender_identity = _forward_node_identity(
                raw_node=raw_node,
                data=data,
                sender=sender,
                raw_nested_message=raw_nested_message,
            )
        content = " ".join(part for part in content_parts if part).strip()
        node_text = text_content or content
        normalized_node = {
            "sender_id": sender_identity.get("sender_id"),
            "sender_name": sender_identity.get("sender_name"),
            "raw_sender_id": sender_identity.get("raw_sender_id"),
            "raw_sender_name": sender_identity.get("raw_sender_name"),
            "alias_sender_id": sender_identity.get("alias_sender_id"),
            "alias_sender_name": sender_identity.get("alias_sender_name"),
            "avatar_url": sender_identity.get("avatar_url"),
            "content": content,
            "text_content": node_text,
            "timestamp_iso": _forward_node_timestamp_iso(raw_node, raw_nested_message),
            "image_file_names": image_file_names,
            "uploaded_file_names": uploaded_file_names,
            "emoji_tokens": emoji_tokens,
            "segments": [_segment_dump(segment) for segment in segments],
            "reply_to": _reply_to.model_dump(mode="json") if _reply_to is not None else None,
        }
        normalized_nodes.append(normalized_node)
        if node_text:
            prefix = (
                normalized_node.get("raw_sender_name")
                or normalized_node.get("sender_name")
                or normalized_node.get("raw_sender_id")
                or normalized_node.get("sender_id")
            )
            text_parts.append(f"{prefix}: {node_text}" if prefix else node_text)

    flattened_text = "\n".join(part for part in text_parts if part).strip()
    return (
        normalized_nodes,
        flattened_text,
        len(normalized_nodes),
        _forward_detail_depth(normalized_nodes),
    )


def _extract_exporter_elements(message: dict[str, Any]) -> list[dict[str, Any]]:
    raw_message = _message_raw(message)
    elements = raw_message.get("elements")
    if isinstance(elements, list):
        return elements
    return []


def _extract_onebot_segments(message: dict[str, Any]) -> list[dict[str, Any]]:
    payload = message.get("message")
    if isinstance(payload, list):
        return payload
    return []


def _onebot_segments_have_expanded_forward_content(message: dict[str, Any]) -> bool:
    for segment in _extract_onebot_segments(message):
        if not isinstance(segment, dict):
            continue
        segment_type = str(segment.get("type") or "").strip().lower()
        data = _safe_mapping(segment.get("data"))
        if segment_type == "node":
            if isinstance(data.get("message") or data.get("content"), list):
                return True
        if segment_type == "forward":
            if isinstance(data.get("content") or data.get("message"), list):
                return True
    return False


def _extract_onebot_parent_context(message: dict[str, Any]) -> dict[str, Any]:
    raw_message = _message_raw(message)
    raw_msg_id = _clean_text(
        raw_message.get("msgId")
        or message.get("message_id")
        or message.get("messageId")
    )
    raw_peer_uid = _clean_text(raw_message.get("peerUid"))
    raw_chat_type = raw_message.get("chatType")
    forward_element_id: str | None = None
    for element in _extract_exporter_elements(message):
        element_type = element.get("elementType")
        if element_type == FORWARD_ELEMENT:
            forward_element_id = _clean_text(
                element.get("elementId") or element.get("element_id")
            ) or None
            break
    return {
        "message_id_raw": raw_msg_id or None,
        "peer_uid": raw_peer_uid or None,
        "chat_type_raw": raw_chat_type,
        "forward_element_id": forward_element_id,
    }


def _build_reply_ref(
    message: dict[str, Any], reply_payload: dict[str, Any] | None
) -> ReplyRef | None:
    if not reply_payload:
        return None
    return ReplyRef(
        referenced_message_id=str(
            reply_payload.get("referencedMessageId")
            or reply_payload.get("messageId")
            or reply_payload.get("id")
            or ""
        )
        or None,
        referenced_sender_id=str(
            reply_payload.get("senderUid")
            or reply_payload.get("sender_id")
            or reply_payload.get("senderId")
            or ""
        )
        or None,
        referenced_timestamp=str(
            reply_payload.get("replyMsgTime") or reply_payload.get("time") or ""
        )
        or None,
        preview_text=_clean_text(
            reply_payload.get("content")
            or reply_payload.get("summary")
            or reply_payload.get("preview")
        )
        or None,
    )


def _normalize_exporter_elements(
    message: dict[str, Any],
) -> tuple[
    list[NormalizedSegment],
    list[str],
    list[str],
    list[str],
    list[str],
    str,
    ReplyRef | None,
]:
    segments: list[NormalizedSegment] = []
    content_parts: list[str] = []
    text_parts: list[str] = []
    image_file_names: list[str] = []
    uploaded_file_names: list[str] = []
    emoji_tokens: list[str] = []
    reply_to = _build_reply_ref(message, message.get("content", {}).get("reply"))
    raw_message = _message_raw(message)
    raw_msg_id = _clean_text(
        raw_message.get("msgId")
        or message.get("messageId")
        or message.get("message_id")
    )
    raw_peer_uid = _clean_text(raw_message.get("peerUid"))
    raw_chat_type = raw_message.get("chatType")

    for element in _extract_exporter_elements(message):
        element_type = element.get("elementType")
        element_id = _clean_text(element.get("elementId"))
        if element_type == TEXT_ELEMENT:
            text = _clean_text(element.get("textElement", {}).get("content"))
            if text:
                segments.append(NormalizedSegment(type="text", text=text))
                content_parts.append(text)
                text_parts.append(text)
            continue

        if element_type == PIC_ELEMENT:
            pic = element.get("picElement", {})
            file_name = (
                pic.get("fileName")
                or _basename_from_path(pic.get("sourcePath"))
                or "image.jpg"
            )
            token = f"[image:{file_name}]"
            image_file_names.append(file_name)
            segments.append(
                NormalizedSegment(
                    type="image",
                    token=token,
                    file_name=file_name,
                    path=pic.get("sourcePath"),
                    md5=_infer_md5(
                        pic.get("md5HexStr"),
                        file_name,
                        pic.get("sourcePath"),
                    ),
                    summary=_safe_summary(pic.get("summary")) or None,
                    extra={
                        "file_id": pic.get("fileUuid"),
                        "width": pic.get("picWidth"),
                        "height": pic.get("picHeight"),
                        "url": pic.get("originImageUrl"),
                        "message_id_raw": raw_msg_id or None,
                        "element_id": element_id or None,
                        "peer_uid": raw_peer_uid or None,
                        "chat_type_raw": raw_chat_type,
                    },
                )
            )
            content_parts.append(token)
            continue

        if element_type == FILE_ELEMENT:
            file_payload = element.get("fileElement", {})
            file_name = (
                file_payload.get("fileName")
                or _basename_from_path(file_payload.get("filePath"))
                or "uploaded_file"
            )
            token = f"[uploaded_file_name:{file_name}]"
            uploaded_file_names.append(file_name)
            segments.append(
                NormalizedSegment(
                    type="file",
                    token=token,
                    file_name=file_name,
                    path=file_payload.get("filePath"),
                    md5=file_payload.get("fileMd5"),
                    extra={
                        "file_id": file_payload.get("fileUuid"),
                        "file_biz_id": file_payload.get("fileBizId"),
                        "message_id_raw": raw_msg_id or None,
                        "element_id": element_id or None,
                        "peer_uid": raw_peer_uid or None,
                        "chat_type_raw": raw_chat_type,
                    },
                )
            )
            content_parts.append(token)
            continue

        if element_type == PTT_ELEMENT:
            ptt = element.get("pttElement", {})
            token = "[speech audio]"
            segments.append(
                NormalizedSegment(
                    type="speech",
                    token=token,
                    file_name=ptt.get("fileName"),
                    path=ptt.get("filePath"),
                    md5=ptt.get("md5HexStr"),
                    extra={
                        "file_id": ptt.get("fileUuid"),
                        "message_id_raw": raw_msg_id or None,
                        "element_id": element_id or None,
                        "peer_uid": raw_peer_uid or None,
                        "chat_type_raw": raw_chat_type,
                    },
                )
            )
            content_parts.append(token)
            continue

        if element_type == FACE_ELEMENT:
            face = element.get("faceElement", {})
            face_id = str(face.get("faceIndex"))
            token = f"[emoji:id={face_id}]"
            emoji_tokens.append(token)
            segments.append(
                NormalizedSegment(
                    type="emoji",
                    token=token,
                    emoji_id=face_id,
                    extra={
                        "resultId": face.get("resultId"),
                        "chainCount": face.get("chainCount"),
                    },
                )
            )
            content_parts.append(token)
            continue

        if element_type == MARKET_FACE_ELEMENT:
            market = element.get("marketFaceElement", {})
            summary = _safe_summary(market.get("faceName")) or "sticker"
            emoji_id = str(market.get("emojiId") or "")
            package_id = market.get("emojiPackageId")
            remote_url, remote_file_name = _build_marketface_remote(emoji_id)
            token = f"[sticker:summary={summary},emoji_id={emoji_id},package_id={package_id}]"
            emoji_tokens.append(token)
            segments.append(
                NormalizedSegment(
                    type="sticker",
                    token=token,
                    emoji_id=emoji_id,
                    emoji_package_id=package_id,
                    summary=summary,
                    extra={
                        "key": market.get("key"),
                        "static_path": market.get("staticFacePath"),
                        "dynamic_path": market.get("dynamicFacePath"),
                        "message_id_raw": raw_msg_id or None,
                        "element_id": element_id or None,
                        "peer_uid": raw_peer_uid or None,
                        "chat_type_raw": raw_chat_type,
                        "remote_url": remote_url,
                        "remote_file_name": remote_file_name,
                    },
                )
            )
            content_parts.append(token)
            continue

        if element_type == FORWARD_ELEMENT:
            forward = element.get("multiForwardMsgElement", {})
            parsed_forward = _parse_forward_preview_xml(forward.get("xmlContent"))
            summary = parsed_forward["title"]
            preview_text = parsed_forward["preview_text"]
            (
                forward_messages,
                detailed_text,
                forwarded_count,
                forward_depth,
            ) = _normalize_forward_nodes(
                forward.get("messages") or forward.get("content") or []
            )
            forward_text = detailed_text or preview_text
            segments.append(
                NormalizedSegment(
                    type="forward",
                    token=FORWARD_TOKEN,
                    summary=summary,
                    extra={
                        "message_id_raw": raw_msg_id or None,
                        "element_id": element_id or None,
                        "peer_uid": raw_peer_uid or None,
                        "chat_type_raw": raw_chat_type,
                        "xml_content": forward.get("xmlContent"),
                        "res_id": forward.get("resId"),
                        "file_name": forward.get("fileName"),
                        "source_name": parsed_forward.get("source_name"),
                        "summary_text": parsed_forward.get("summary_text"),
                        "preview_lines": parsed_forward.get("preview_lines") or [],
                        "preview_text": preview_text,
                        "forwarded_count": parsed_forward.get("forwarded_count")
                        or forwarded_count
                        or None,
                        "forward_messages": forward_messages,
                        "detailed_text": detailed_text or None,
                        "forward_depth": forward_depth,
                    },
                )
            )
            content_parts.append(FORWARD_TOKEN)
            if forward_text:
                content_parts.append(forward_text)
                text_parts.append(forward_text)
            continue

        if element_type == GRAY_TIP_ELEMENT:
            gray_tip = element.get("grayTipElement", {})
            parsed_gray_tip = _parse_gray_tip_payload(gray_tip)
            text = parsed_gray_tip["text"]
            segments.append(
                NormalizedSegment(
                    type="system",
                    text=text,
                    summary=text,
                    extra={
                        "gray_tip": gray_tip,
                        "busi_id": parsed_gray_tip.get("busi_id"),
                        "sub_element_type": parsed_gray_tip.get("sub_element_type"),
                        "recent_abstract": parsed_gray_tip.get("recent_abstract"),
                    },
                )
            )
            content_parts.append(text)
            text_parts.append(text)
            continue

        if element_type == ARK_ELEMENT:
            ark = element.get("arkElement", {})
            parsed_share = _parse_share_payload(ark.get("bytesData"))
            title = parsed_share.get("title") or "分享卡片"
            token = f"[share:{title}]"
            summary_text = parsed_share.get("summary_text") or title
            segments.append(
                NormalizedSegment(
                    type="share",
                    token=token,
                    summary=title,
                    extra={
                        "desc": parsed_share.get("desc"),
                        "tag": parsed_share.get("tag"),
                        "url": parsed_share.get("url"),
                        "prompt": parsed_share.get("prompt"),
                        "raw_share": parsed_share.get("raw"),
                    },
                )
            )
            content_parts.append(token)
            if summary_text:
                content_parts.append(summary_text)
                text_parts.append(summary_text)
            continue

        video = element.get("videoElement")
        if video:
            file_name = (
                video.get("fileName")
                or _basename_from_path(video.get("filePath"))
                or "video.mp4"
            )
            token = f"[video:{file_name}]"
            segments.append(
                NormalizedSegment(
                    type="video",
                    token=token,
                    file_name=file_name,
                    path=video.get("filePath"),
                    md5=video.get("md5HexStr"),
                    extra={
                        "fileUuid": video.get("fileUuid"),
                        "fileSize": video.get("fileSize"),
                        "file_id": video.get("fileUuid"),
                        "message_id_raw": raw_msg_id or None,
                        "element_id": element_id or None,
                        "peer_uid": raw_peer_uid or None,
                        "chat_type_raw": raw_chat_type,
                    },
                )
            )
            content_parts.append(token)
            continue

        if element_type == REPLY_ELEMENT:
            reply = element.get("replyElement", {})
            reply_text = _clean_text(
                reply.get("content") or reply.get("summary") or reply.get("text")
            )
            segments.append(
                NormalizedSegment(
                    type="reply",
                    text=reply_text or None,
                    extra={
                        "sender_uid": reply.get("senderUid"),
                        "reply_msg_time": reply.get("replyMsgTime"),
                        "reply_msg_id": reply.get("replayMsgId"),
                        "reply_text": reply_text or None,
                    },
                )
            )
            if reply_text:
                content_parts.append(reply_text)
                text_parts.append(reply_text)
            if reply_to is None:
                reply_to = _build_reply_ref(message, reply)
            continue

        segments.append(
            NormalizedSegment(
                type="unsupported",
                token=f"[unsupported:{element_type}]",
                extra={"element": element},
            )
        )
        content_parts.append(f"[unsupported:{element_type}]")

    content = " ".join(part for part in content_parts if part).strip()
    text_content = " ".join(part for part in text_parts if part).strip()
    return (
        segments,
        image_file_names,
        uploaded_file_names,
        emoji_tokens,
        content_parts,
        text_content,
        reply_to,
    )


def _normalize_onebot_segments(
    message: dict[str, Any],
    *,
    inherited_parent_context: dict[str, Any] | None = None,
) -> tuple[
    list[NormalizedSegment],
    list[str],
    list[str],
    list[str],
    list[str],
    str,
    ReplyRef | None,
]:
    segments: list[NormalizedSegment] = []
    content_parts: list[str] = []
    text_parts: list[str] = []
    image_file_names: list[str] = []
    uploaded_file_names: list[str] = []
    emoji_tokens: list[str] = []
    reply_to: ReplyRef | None = None
    parent_context = _merge_parent_context(
        message=message,
        inherited_parent_context=inherited_parent_context,
    )
    parent_message_id_raw = parent_context.get("message_id_raw")
    parent_peer_uid = parent_context.get("peer_uid")
    parent_chat_type_raw = parent_context.get("chat_type_raw")
    parent_forward_element_id = parent_context.get("forward_element_id")

    for segment in _extract_onebot_segments(message):
        segment_type = segment.get("type")
        data = segment.get("data", {})
        if segment_type == "node":
            (
                forward_messages,
                detailed_text,
                forwarded_count,
                forward_depth,
            ) = _normalize_forward_nodes(
                data.get("message") or data.get("content") or [],
                inherited_parent_context=parent_context,
            )
            segments.append(
                NormalizedSegment(
                    type="forward",
                    token=FORWARD_TOKEN,
                    summary="聊天记录",
                    extra={
                        "message_id_raw": parent_message_id_raw,
                        "element_id": parent_forward_element_id,
                        "peer_uid": parent_peer_uid,
                        "chat_type_raw": parent_chat_type_raw,
                        "forward_messages": forward_messages,
                        "detailed_text": detailed_text or None,
                        "forwarded_count": forwarded_count or None,
                        "forward_depth": forward_depth,
                    },
                )
            )
            content_parts.append(FORWARD_TOKEN)
            if detailed_text:
                content_parts.append(detailed_text)
                text_parts.append(detailed_text)
            continue

        if segment_type == "text":
            text = _clean_text(data.get("text"))
            if text:
                segments.append(NormalizedSegment(type="text", text=text))
                content_parts.append(text)
                text_parts.append(text)
            continue

        if segment_type == "at":
            text = f"@{data.get('name') or data.get('qq')}"
            segments.append(NormalizedSegment(type="text", text=text))
            content_parts.append(text)
            text_parts.append(text)
            continue

        if segment_type == "image":
            file_name = (
                data.get("name")
                or _basename_from_path(data.get("path"))
                or _basename_from_path(data.get("file"))
                or "image.jpg"
            )
            token = f"[image:{file_name}]"
            image_file_names.append(file_name)
            segments.append(
                NormalizedSegment(
                    type="image",
                    token=token,
                    file_name=file_name,
                    path=data.get("path"),
                    md5=_infer_md5(data.get("md5"), file_name, data.get("file")),
                    summary=_safe_summary(data.get("summary")) or None,
                    extra={
                        "url": data.get("url"),
                        "file_id": data.get("file_id"),
                        "message_id_raw": parent_message_id_raw,
                        "element_id": parent_forward_element_id,
                        "peer_uid": parent_peer_uid,
                        "chat_type_raw": parent_chat_type_raw,
                    },
                )
            )
            content_parts.append(token)
            continue

        if segment_type in {"file", "onlinefile"}:
            file_name = (
                data.get("name")
                or data.get("fileName")
                or _basename_from_path(data.get("path"))
                or "uploaded_file"
            )
            token = f"[uploaded_file_name:{file_name}]"
            uploaded_file_names.append(file_name)
            segments.append(
                NormalizedSegment(
                    type="file",
                    token=token,
                    file_name=file_name,
                    path=data.get("path"),
                    md5=_infer_md5(data.get("md5"), data.get("file"), file_name),
                    extra={
                        "url": data.get("url"),
                        "file_id": data.get("file_id"),
                        "file_biz_id": data.get("file_biz_id") or data.get("fileBizId"),
                        "message_id_raw": parent_message_id_raw,
                        "element_id": parent_forward_element_id,
                        "peer_uid": parent_peer_uid,
                        "chat_type_raw": parent_chat_type_raw,
                    },
                )
            )
            content_parts.append(token)
            continue

        if segment_type == "record":
            token = "[speech audio]"
            segments.append(
                NormalizedSegment(
                    type="speech",
                    token=token,
                    file_name=data.get("name") or _basename_from_path(data.get("path")),
                    path=data.get("path"),
                    extra={
                        "url": data.get("url"),
                        "file_id": data.get("file_id"),
                        "message_id_raw": parent_message_id_raw,
                        "element_id": parent_forward_element_id,
                        "peer_uid": parent_peer_uid,
                        "chat_type_raw": parent_chat_type_raw,
                    },
                )
            )
            content_parts.append(token)
            continue

        if segment_type == "video":
            file_name = (
                data.get("name")
                or _basename_from_path(data.get("path"))
                or _basename_from_path(data.get("file"))
                or "video.mp4"
            )
            token = f"[video:{file_name}]"
            segments.append(
                NormalizedSegment(
                    type="video",
                    token=token,
                    file_name=file_name,
                    path=data.get("path"),
                    md5=data.get("md5"),
                    extra={
                        "url": data.get("url"),
                        "file_id": data.get("file_id"),
                        "message_id_raw": parent_message_id_raw,
                        "element_id": parent_forward_element_id,
                        "peer_uid": parent_peer_uid,
                        "chat_type_raw": parent_chat_type_raw,
                    },
                )
            )
            content_parts.append(token)
            continue

        if segment_type == "forward":
            summary = _collapse_inline_text(data.get("title") or data.get("name"))
            content_hint = data.get("content")
            if isinstance(content_hint, list):
                content_hint = None
            preview_text = _truncate_preview(
                _collapse_inline_text(content_hint or data.get("prompt") or summary),
                max_length=240,
            )
            (
                forward_messages,
                detailed_text,
                forwarded_count,
                forward_depth,
            ) = _normalize_forward_nodes(
                data.get("content") or [],
                inherited_parent_context=parent_context,
            )
            forward_text = detailed_text or preview_text
            segments.append(
                NormalizedSegment(
                    type="forward",
                    token=FORWARD_TOKEN,
                    summary=summary or "聊天记录",
                    extra={
                        "message_id_raw": parent_message_id_raw,
                        "element_id": parent_forward_element_id,
                        "peer_uid": parent_peer_uid,
                        "chat_type_raw": parent_chat_type_raw,
                        "forward_id": data.get("id") or data.get("resid"),
                        "preview_text": preview_text or None,
                        "forward_messages": forward_messages,
                        "detailed_text": detailed_text or None,
                        "forwarded_count": forwarded_count or None,
                        "forward_depth": forward_depth,
                    },
                )
            )
            content_parts.append(FORWARD_TOKEN)
            if forward_text:
                content_parts.append(forward_text)
                text_parts.append(forward_text)
            continue

        if segment_type == "json":
            parsed_share = _parse_share_payload(data.get("data") or data.get("content"))
            title = parsed_share.get("title")
            if title or parsed_share.get("desc") or parsed_share.get("prompt"):
                token = f"[share:{title or '分享卡片'}]"
                summary_text = parsed_share.get("summary_text") or title or "分享卡片"
                segments.append(
                    NormalizedSegment(
                        type="share",
                        token=token,
                        summary=title or "分享卡片",
                        extra={
                            "desc": parsed_share.get("desc"),
                            "tag": parsed_share.get("tag"),
                            "url": parsed_share.get("url"),
                            "prompt": parsed_share.get("prompt"),
                            "raw_share": parsed_share.get("raw"),
                        },
                    )
                )
                content_parts.append(token)
                if summary_text:
                    content_parts.append(summary_text)
                    text_parts.append(summary_text)
                continue

        if segment_type == "xml":
            xml_payload = data.get("data") or data.get("xml") or data.get("content")
            xml_text = _clean_text(xml_payload).strip()
            if "viewMultiMsg" in xml_text or "[聊天记录]" in xml_text:
                parsed_forward = _parse_forward_preview_xml(xml_text)
                preview_text = parsed_forward.get("preview_text") or ""
                segments.append(
                    NormalizedSegment(
                        type="forward",
                        token=FORWARD_TOKEN,
                        summary=parsed_forward.get("title") or "聊天记录",
                        extra={
                            "message_id_raw": parent_message_id_raw,
                            "element_id": parent_forward_element_id,
                            "peer_uid": parent_peer_uid,
                            "chat_type_raw": parent_chat_type_raw,
                            "xml_content": xml_text,
                            "source_name": parsed_forward.get("source_name"),
                            "summary_text": parsed_forward.get("summary_text"),
                            "preview_lines": parsed_forward.get("preview_lines") or [],
                            "preview_text": preview_text or None,
                            "forwarded_count": parsed_forward.get("forwarded_count"),
                            "forward_depth": 1,
                        },
                    )
                )
                content_parts.append(FORWARD_TOKEN)
                if preview_text:
                    content_parts.append(preview_text)
                    text_parts.append(preview_text)
                continue
            if "<gtip" in xml_text:
                text = (
                    _truncate_preview(_strip_xml_tags(xml_text), max_length=240)
                    or "[system message]"
                )
                segments.append(
                    NormalizedSegment(
                        type="system",
                        text=text,
                        summary=text,
                        extra={"xml_content": xml_text},
                    )
                )
                content_parts.append(text)
                text_parts.append(text)
                continue

        if segment_type == "face":
            emoji_id = str(data.get("id"))
            token = f"[emoji:id={emoji_id}]"
            emoji_tokens.append(token)
            segments.append(
                NormalizedSegment(
                    type="emoji",
                    token=token,
                    emoji_id=emoji_id,
                    extra={
                        "resultId": data.get("resultId"),
                        "chainCount": data.get("chainCount"),
                    },
                )
            )
            content_parts.append(token)
            continue

        if segment_type == "mface":
            summary = _safe_summary(data.get("summary")) or "sticker"
            emoji_id = str(data.get("emoji_id") or "")
            package_id = data.get("emoji_package_id")
            remote_url, remote_file_name = _build_marketface_remote(emoji_id)
            token = f"[sticker:summary={summary},emoji_id={emoji_id},package_id={package_id}]"
            emoji_tokens.append(token)
            segments.append(
                NormalizedSegment(
                    type="sticker",
                    token=token,
                    path=data.get("path") or data.get("file"),
                    emoji_id=emoji_id,
                    emoji_package_id=package_id,
                    summary=summary,
                    extra={
                        "key": data.get("key"),
                        "static_path": data.get("static_path"),
                        "dynamic_path": data.get("dynamic_path"),
                        "remote_url": data.get("remote_url") or remote_url,
                        "remote_file_name": data.get("remote_file_name") or remote_file_name,
                    },
                )
            )
            content_parts.append(token)
            continue

        if segment_type == "reply":
            reply_to = ReplyRef(
                referenced_message_id=str(data.get("id") or data.get("seq") or "")
            )
            segments.append(NormalizedSegment(type="reply"))
            continue

        segments.append(
            NormalizedSegment(
                type="unsupported",
                token=f"[unsupported:{segment_type}]",
                extra={"segment": segment},
            )
        )
        content_parts.append(f"[unsupported:{segment_type}]")

    content = " ".join(part for part in content_parts if part).strip()
    text_content = " ".join(part for part in text_parts if part).strip()
    return (
        segments,
        image_file_names,
        uploaded_file_names,
        emoji_tokens,
        content_parts,
        text_content,
        reply_to,
    )


def normalize_message(
    message: dict[str, Any],
    *,
    chat_type: str,
    chat_id: str,
    chat_name: str | None = None,
    include_raw: bool = False,
    fallback_timestamp: datetime | None = None,
) -> NormalizedMessage:
    timestamp = _parse_timestamp(message, fallback_timestamp=fallback_timestamp)
    if _onebot_segments_have_expanded_forward_content(message):
        (
            segments,
            image_file_names,
            uploaded_file_names,
            emoji_tokens,
            content_parts,
            text_content,
            reply_to,
        ) = _normalize_onebot_segments(message)
    elif _extract_exporter_elements(message):
        (
            segments,
            image_file_names,
            uploaded_file_names,
            emoji_tokens,
            content_parts,
            text_content,
            reply_to,
        ) = _normalize_exporter_elements(message)
    elif _extract_onebot_segments(message):
        (
            segments,
            image_file_names,
            uploaded_file_names,
            emoji_tokens,
            content_parts,
            text_content,
            reply_to,
        ) = _normalize_onebot_segments(message)
    else:
        (
            segments,
            image_file_names,
            uploaded_file_names,
            emoji_tokens,
            content_parts,
            text_content,
            reply_to,
        ) = _normalize_exporter_elements(message)

    sender = _message_sender(message)
    raw_message = _message_raw(message)
    sender_id = str(
        sender.get("uin")
        or message.get("user_id")
        or message.get("sender_id")
        or raw_message.get("senderUin")
        or ""
    )
    content = " ".join(part for part in content_parts if part).strip()
    extra: dict[str, Any] = {}
    if message.get("isSystemMessage"):
        extra["is_system_message"] = True
    if message.get("isRecalled"):
        extra["is_recalled"] = True

    peer_id = chat_id if chat_type == "private" else None
    group_id = chat_id if chat_type == "group" else None

    return NormalizedMessage(
        chat_type=chat_type,  # type: ignore[arg-type]
        chat_id=chat_id,
        group_id=group_id,
        peer_id=peer_id,
        chat_name=chat_name,
        sender_id=sender_id,
        sender_name=sender.get("name") or sender.get("nickname"),
        sender_card=sender.get("card"),
        message_id=str(
            message.get("messageId")
            or message.get("message_id")
            or raw_message.get("msgId")
            or ""
        )
        or None,
        message_seq=str(
            message.get("messageSeq")
            or message.get("message_seq")
            or raw_message.get("msgSeq")
            or ""
        )
        or None,
        timestamp_ms=int(timestamp.timestamp() * 1000),
        timestamp_iso=timestamp.isoformat(),
        content=content,
        text_content=text_content,
        image_file_names=image_file_names,
        uploaded_file_names=uploaded_file_names,
        emoji_tokens=emoji_tokens,
        segments=segments,
        reply_to=reply_to,
        extra=extra,
        # Keep a detached debug snapshot so later in-process mutation of the
        # source payload cannot rewrite previously captured forensic/raw views.
        raw_message=deepcopy(message) if include_raw else None,
    )


def normalize_snapshot(
    snapshot: SourceChatSnapshot,
    *,
    include_raw: bool = False,
) -> NormalizedSnapshot:
    fallback_timestamp = snapshot.exported_at.astimezone(EXPORT_TIMEZONE)
    messages = [
        normalize_message(
            message,
            chat_type=snapshot.chat_type,
            chat_id=snapshot.chat_id,
            chat_name=snapshot.chat_name,
            include_raw=include_raw,
            fallback_timestamp=fallback_timestamp,
        )
        for message in snapshot.messages
    ]
    messages.sort(key=lambda item: (item.timestamp_ms, item.message_seq or "", item.message_id or ""))
    return NormalizedSnapshot(
        chat_type=snapshot.chat_type,
        chat_id=snapshot.chat_id,
        chat_name=snapshot.chat_name,
        exported_at=snapshot.exported_at,
        metadata=deepcopy(snapshot.metadata),
        messages=messages,
    )
