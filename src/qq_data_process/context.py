from __future__ import annotations

from typing import Any

from .rag_models import ContextBlock, ContextMessage, RetrievalConfig, RetrievedMessageHit
from .sqlite_store import SqlitePreprocessStore
from .utils import stable_digest


class ContextBuilder:
    def __init__(self, *, sqlite_store: SqlitePreprocessStore) -> None:
        self.sqlite_store = sqlite_store

    def build(
        self, *, hits: list[RetrievedMessageHit], config: RetrievalConfig
    ) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        used_message_uids: set[str] = set()

        for hit in hits:
            if len(blocks) >= config.max_context_blocks:
                break
            if hit.message_uid in used_message_uids:
                continue

            rows: list[dict[str, Any]] = []
            source_kind = "window"
            if config.prefer_chunk_context:
                rows = self.sqlite_store.load_chunk_context_for_message(
                    message_uid=hit.message_uid,
                    run_id=hit.run_id,
                    max_messages=config.max_messages_per_block,
                )
                if rows:
                    source_kind = "chunk"

            if not rows:
                rows = self.sqlite_store.load_message_window(
                    message_uid=hit.message_uid,
                    before=config.context_window_before,
                    after=config.context_window_after,
                )

            if not rows:
                continue

            messages = [self._context_message(row, config) for row in rows]
            blocks.append(
                ContextBlock(
                    block_id=f"ctx_{stable_digest(hit.message_uid, source_kind)}",
                    source_kind=source_kind,
                    anchor_message_uid=hit.message_uid,
                    messages=messages,
                    rendered_text=self._render_messages(messages),
                )
            )
            used_message_uids.update(message.message_uid for message in messages)

        return blocks

    def _context_message(
        self, row: dict[str, Any], config: RetrievalConfig
    ) -> ContextMessage:
        if config.projection_mode == "raw":
            chat_id = row["chat_id_raw"]
            chat_name = row["chat_name_raw"]
            sender_id = row["sender_id_raw"]
            sender_name = row["sender_name_raw"]
        else:
            chat_id = row["chat_alias_id"]
            chat_name = row["chat_alias_label"]
            sender_id = row["sender_alias_id"]
            sender_name = row["sender_alias_label"]
        return ContextMessage(
            message_uid=row["message_uid"],
            timestamp_iso=row["timestamp_iso"],
            chat_id=chat_id,
            chat_name=chat_name,
            sender_id=sender_id,
            sender_name=sender_name,
            content=row["content"],
        )

    def _render_messages(self, messages: list[ContextMessage]) -> str:
        lines = []
        for message in messages:
            lines.append(
                f"[{message.timestamp_iso}] {message.sender_name} ({message.sender_id}): {message.content}"
            )
        return "\n".join(lines)
