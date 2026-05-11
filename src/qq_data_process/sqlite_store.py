from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .identities import IdentityProjector
from .models import (
    CanonicalAssetRecord,
    CanonicalMessageRecord,
    ChunkBuildResult,
    EmbeddingPolicy,
    ImportedChatBundle,
    PreprocessJobConfig,
    PreprocessRunResult,
)
from .runtime_control import maybe_cooperative_yield


class SqlitePreprocessStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS import_runs (
                    run_id TEXT PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    fidelity TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_name TEXT,
                    chunk_specs_json TEXT NOT NULL,
                    embedding_policy_json TEXT NOT NULL,
                    identity_policy_json TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    asset_count INTEGER NOT NULL,
                    chunk_set_count INTEGER NOT NULL,
                    warnings_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id_raw TEXT PRIMARY KEY,
                    chat_type TEXT NOT NULL,
                    chat_name_raw TEXT,
                    chat_alias_id TEXT NOT NULL,
                    chat_alias_label TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS participants_raw (
                    sender_id_raw TEXT PRIMARY KEY,
                    sender_name_raw TEXT,
                    sender_alias_id TEXT NOT NULL,
                    sender_alias_label TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS participants_alias (
                    alias_id TEXT PRIMARY KEY,
                    alias_label TEXT NOT NULL,
                    sender_id_raw TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_uid TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    import_source TEXT NOT NULL,
                    fidelity TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    chat_id_raw TEXT NOT NULL,
                    chat_name_raw TEXT,
                    chat_alias_id TEXT NOT NULL,
                    chat_alias_label TEXT NOT NULL,
                    sender_id_raw TEXT NOT NULL,
                    sender_name_raw TEXT,
                    sender_alias_id TEXT NOT NULL,
                    sender_alias_label TEXT NOT NULL,
                    message_id TEXT,
                    message_seq TEXT,
                    timestamp_ms INTEGER NOT NULL,
                    timestamp_iso TEXT NOT NULL,
                    content TEXT NOT NULL,
                    text_content TEXT NOT NULL,
                    extra_json TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    message_uid UNINDEXED,
                    run_id UNINDEXED,
                    chat_id_raw UNINDEXED,
                    chat_alias_id UNINDEXED,
                    sender_alias_id UNINDEXED,
                    content,
                    text_content,
                    tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE IF NOT EXISTS message_assets (
                    asset_id TEXT PRIMARY KEY,
                    message_uid TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    file_name TEXT,
                    path TEXT,
                    md5 TEXT,
                    extra_json TEXT NOT NULL,
                    future_multimodal_parse INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    message_uid TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    path TEXT,
                    extra_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunk_sets (
                    chunk_set_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    policy_name TEXT NOT NULL,
                    policy_version TEXT NOT NULL,
                    policy_params_json TEXT NOT NULL,
                    chunk_kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id TEXT PRIMARY KEY,
                    chunk_set_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chunk_kind TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    start_message_uid TEXT NOT NULL,
                    end_message_uid TEXT NOT NULL,
                    start_timestamp_ms INTEGER NOT NULL,
                    end_timestamp_ms INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    content_preview TEXT NOT NULL,
                    extra_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunk_memberships (
                    chunk_id TEXT NOT NULL,
                    message_uid TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY (chunk_id, message_uid)
                );
                CREATE TABLE IF NOT EXISTS run_diagnostics (
                    run_id TEXT NOT NULL,
                    diagnostic_kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, diagnostic_kind)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_run_chat_time
                    ON messages(run_id, chat_id_raw, timestamp_ms, message_uid);
                CREATE INDEX IF NOT EXISTS idx_messages_alias_time
                    ON messages(chat_alias_id, timestamp_ms, message_uid);
                CREATE INDEX IF NOT EXISTS idx_message_assets_message
                    ON message_assets(message_uid);
                CREATE INDEX IF NOT EXISTS idx_chunk_memberships_message
                    ON chunk_memberships(message_uid, chunk_id, ordinal);
                """
            )

    def persist_run(
        self,
        *,
        result: PreprocessRunResult,
        bundle: ImportedChatBundle,
        config: PreprocessJobConfig,
        messages: list[CanonicalMessageRecord],
        chunks: list[ChunkBuildResult],
        identity_projector: IdentityProjector,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        with self._connect() as conn:
            chat_projection = identity_projector.alias_for("chat", bundle.chat_id)
            conn.execute(
                """
                INSERT OR REPLACE INTO chats (
                    chat_id_raw, chat_type, chat_name_raw, chat_alias_id, chat_alias_label
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    bundle.chat_id,
                    bundle.chat_type,
                    bundle.chat_name,
                    chat_projection.alias_id,
                    chat_projection.alias_label,
                ),
            )

            participant_raw_rows: list[tuple[str, str | None, str, str]] = []
            participant_alias_rows: list[tuple[str, str, str]] = []
            seen_senders: set[str] = set()
            message_rows: list[tuple[Any, ...]] = []
            fts_delete_rows: list[tuple[str]] = []
            fts_insert_rows: list[tuple[Any, ...]] = []
            asset_rows: list[tuple[Any, ...]] = []
            artifact_rows: list[tuple[Any, ...]] = []
            total_messages = len(messages)
            for message_index, message in enumerate(messages, start=1):
                view = identity_projector.project(message)
                if message.sender_id_raw not in seen_senders:
                    seen_senders.add(message.sender_id_raw)
                    participant_raw_rows.append(
                        (
                            message.sender_id_raw,
                            message.sender_name_raw,
                            view.sender.alias_id,
                            view.sender.alias_label,
                        )
                    )
                    participant_alias_rows.append(
                        (
                            view.sender.alias_id,
                            view.sender.alias_label,
                            message.sender_id_raw,
                        )
                    )
                message_rows.append(
                    (
                        message.message_uid,
                        result.run_id,
                        message.import_source,
                        message.fidelity,
                        message.chat_type,
                        message.chat_id,
                        message.chat_name,
                        view.chat.alias_id,
                        view.chat.alias_label,
                        message.sender_id_raw,
                        message.sender_name_raw,
                        view.sender.alias_id,
                        view.sender.alias_label,
                        message.message_id,
                        message.message_seq,
                        message.timestamp_ms,
                        message.timestamp_iso,
                        message.content,
                        message.text_content,
                        json.dumps(message.extra, ensure_ascii=False),
                    )
                )
                if not config.skip_keyword_index:
                    fts_delete_rows.append((message.message_uid,))
                    fts_insert_rows.append(
                        (
                            message.message_uid,
                            result.run_id,
                            message.chat_id,
                            view.chat.alias_id,
                            view.sender.alias_id,
                            message.content,
                            message.text_content,
                        )
                    )
                for asset in message.assets:
                    asset_row, artifact_row = self._asset_rows(asset)
                    asset_rows.append(asset_row)
                    artifact_rows.append(artifact_row)
                if message_index == total_messages or message_index % 1000 == 0:
                    self._flush_message_batch(
                        conn,
                        participant_raw_rows=participant_raw_rows,
                        participant_alias_rows=participant_alias_rows,
                        message_rows=message_rows,
                        fts_delete_rows=fts_delete_rows,
                        fts_insert_rows=fts_insert_rows,
                        asset_rows=asset_rows,
                        artifact_rows=artifact_rows,
                    )
                    if progress_callback is not None:
                        progress_callback(
                            {
                                "phase": "persist_sqlite",
                                "current": message_index,
                                "total": total_messages,
                                "message": (
                                    f"Persisted message rows/assets {message_index}/{total_messages}"
                                ),
                            }
                        )
                maybe_cooperative_yield(message_index)

            total_chunks = sum(
                len(item.chunks) for item in chunks if item.chunk_set is not None
            )
            processed_chunks = 0
            for chunk_result in chunks:
                if chunk_result.chunk_set is None:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO chunk_sets (
                        chunk_set_id, run_id, chat_id, policy_name, policy_version,
                        policy_params_json, chunk_kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_result.chunk_set.chunk_set_id,
                        chunk_result.chunk_set.run_id,
                        chunk_result.chunk_set.chat_id,
                        chunk_result.chunk_set.policy_name,
                        chunk_result.chunk_set.policy_version,
                        json.dumps(
                            chunk_result.chunk_set.policy_params, ensure_ascii=False
                        ),
                        chunk_result.chunk_set.chunk_kind,
                    ),
                )
                for chunk in chunk_result.chunks:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO chunks (
                            chunk_id, chunk_set_id, chat_id, chunk_kind, ordinal,
                            start_message_uid, end_message_uid, start_timestamp_ms,
                            end_timestamp_ms, message_count, content_preview, extra_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chunk.chunk_id,
                            chunk.chunk_set_id,
                            chunk.chat_id,
                            chunk.chunk_kind,
                            chunk.ordinal,
                            chunk.start_message_uid,
                            chunk.end_message_uid,
                            chunk.start_timestamp_ms,
                            chunk.end_timestamp_ms,
                            chunk.message_count,
                            chunk.content_preview,
                            json.dumps(chunk.extra, ensure_ascii=False),
                        ),
                    )
                    processed_chunks += 1
                    if (
                        progress_callback is not None
                        and total_chunks > 0
                        and (
                            processed_chunks == total_chunks
                            or processed_chunks % 500 == 0
                        )
                    ):
                        progress_callback(
                            {
                                "phase": "persist_chunks",
                                "current": processed_chunks,
                                "total": total_chunks,
                                "message": (
                                    f"Persisted chunk rows {processed_chunks}/{total_chunks}"
                                ),
                            }
                        )
                for membership in chunk_result.memberships:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO chunk_memberships (
                            chunk_id, message_uid, ordinal
                        ) VALUES (?, ?, ?)
                        """,
                        (
                            membership.chunk_id,
                            membership.message_uid,
                            membership.ordinal,
                        ),
                    )

            conn.execute(
                """
                INSERT OR REPLACE INTO import_runs (
                    run_id, source_type, fidelity, source_path, chat_type, chat_id,
                    chat_name, chunk_specs_json, embedding_policy_json,
                    identity_policy_json, message_count, asset_count, chunk_set_count,
                    warnings_json, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.source_type,
                    result.fidelity,
                    str(bundle.source_path),
                    bundle.chat_type,
                    bundle.chat_id,
                    bundle.chat_name,
                    json.dumps(
                        [
                            spec.model_dump(mode="json")
                            for spec in config.chunk_policy_specs
                        ],
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        config.embedding_policy.model_dump(mode="json"),
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        config.identity_policy.model_dump(mode="json"),
                        ensure_ascii=False,
                    ),
                    result.message_count,
                    result.asset_count,
                    result.chunk_set_count,
                    json.dumps(result.warnings, ensure_ascii=False),
                    result.started_at.isoformat(),
                    result.completed_at.isoformat(),
                ),
            )

    def assert_embedding_policy_compatible(self, policy: EmbeddingPolicy) -> None:
        existing = self.load_embedding_policy()
        if existing is None:
            return
        if existing.compatibility_signature() != policy.compatibility_signature():
            raise ValueError(
                "This SQLite/Qdrant state directory already contains vectors built with "
                "a different embedding policy. Use a new state_dir or reindex consistently."
            )

    def load_run_record(self, run_id: str | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            if run_id:
                row = conn.execute(
                    "SELECT * FROM import_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM import_runs ORDER BY completed_at DESC LIMIT 1"
                ).fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in (
            "chunk_specs_json",
            "embedding_policy_json",
            "identity_policy_json",
            "warnings_json",
        ):
            record[key] = json.loads(record[key])
        return record

    def persist_run_diagnostic(
        self, *, run_id: str, diagnostic_kind: str, payload: dict[str, Any]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_diagnostics (
                    run_id, diagnostic_kind, payload_json
                ) VALUES (?, ?, ?)
                """,
                (run_id, diagnostic_kind, json.dumps(payload, ensure_ascii=False)),
            )

    def load_run_diagnostic(
        self, *, run_id: str, diagnostic_kind: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json FROM run_diagnostics
                WHERE run_id = ? AND diagnostic_kind = ?
                """,
                (run_id, diagnostic_kind),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def load_embedding_policy(
        self, run_id: str | None = None
    ) -> EmbeddingPolicy | None:
        record = self.load_run_record(run_id)
        if record is None:
            return None
        return EmbeddingPolicy.model_validate(record["embedding_policy_json"])

    def search_messages_fts(
        self,
        *,
        query: str,
        limit: int,
        run_id: str | None = None,
        chat_id_raw: str | None = None,
        chat_alias_id: str | None = None,
        start_timestamp_ms: int | None = None,
        end_timestamp_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        match_query = self._to_fts_query(query)
        if not match_query:
            return []

        where_clauses = ["messages_fts MATCH ?"]
        params: list[Any] = [match_query]
        if run_id is not None:
            where_clauses.append("m.run_id = ?")
            params.append(run_id)
        if chat_id_raw is not None:
            where_clauses.append("m.chat_id_raw = ?")
            params.append(chat_id_raw)
        if chat_alias_id is not None:
            where_clauses.append("m.chat_alias_id = ?")
            params.append(chat_alias_id)
        if start_timestamp_ms is not None:
            where_clauses.append("m.timestamp_ms >= ?")
            params.append(start_timestamp_ms)
        if end_timestamp_ms is not None:
            where_clauses.append("m.timestamp_ms <= ?")
            params.append(end_timestamp_ms)
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, bm25(messages_fts) AS keyword_score
                FROM messages_fts
                JOIN messages AS m ON m.message_uid = messages_fts.message_uid
                WHERE {" AND ".join(where_clauses)}
                ORDER BY keyword_score ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def load_messages_by_uids(self, message_uids: list[str]) -> list[dict[str, Any]]:
        if not message_uids:
            return []
        placeholders = ", ".join("?" for _ in message_uids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*,
                       (SELECT COUNT(*) FROM message_assets AS a WHERE a.message_uid = m.message_uid) AS asset_count
                FROM messages AS m
                WHERE m.message_uid IN ({placeholders})
                """,
                message_uids,
            ).fetchall()
        by_uid = {row["message_uid"]: dict(row) for row in rows}
        return [by_uid[item] for item in message_uids if item in by_uid]

    def load_message_window(
        self,
        *,
        message_uid: str,
        before: int,
        after: int,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            anchor = conn.execute(
                "SELECT run_id, chat_id_raw, timestamp_ms, message_uid FROM messages WHERE message_uid = ?",
                (message_uid,),
            ).fetchone()
            if anchor is None:
                return []
            previous = conn.execute(
                """
                SELECT * FROM messages
                WHERE run_id = ?
                  AND chat_id_raw = ?
                  AND (
                    timestamp_ms < ?
                    OR (timestamp_ms = ? AND message_uid < ?)
                  )
                ORDER BY timestamp_ms DESC, message_uid DESC
                LIMIT ?
                """,
                (
                    anchor["run_id"],
                    anchor["chat_id_raw"],
                    anchor["timestamp_ms"],
                    anchor["timestamp_ms"],
                    anchor["message_uid"],
                    before,
                ),
            ).fetchall()
            current = conn.execute(
                "SELECT * FROM messages WHERE message_uid = ?",
                (message_uid,),
            ).fetchall()
            following = conn.execute(
                """
                SELECT * FROM messages
                WHERE run_id = ?
                  AND chat_id_raw = ?
                  AND (
                    timestamp_ms > ?
                    OR (timestamp_ms = ? AND message_uid > ?)
                  )
                ORDER BY timestamp_ms ASC, message_uid ASC
                LIMIT ?
                """,
                (
                    anchor["run_id"],
                    anchor["chat_id_raw"],
                    anchor["timestamp_ms"],
                    anchor["timestamp_ms"],
                    anchor["message_uid"],
                    after,
                ),
            ).fetchall()

        rows = list(reversed(previous)) + list(current) + list(following)
        return [dict(row) for row in rows]

    def load_chunk_context_for_message(
        self,
        *,
        message_uid: str,
        run_id: str | None = None,
        max_messages: int | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            params: list[Any] = [message_uid]
            where = ["cm.message_uid = ?"]
            if run_id is not None:
                where.append("cs.run_id = ?")
                params.append(run_id)
            chunk = conn.execute(
                f"""
                SELECT c.chunk_id, c.message_count
                FROM chunk_memberships AS cm
                JOIN chunks AS c ON c.chunk_id = cm.chunk_id
                JOIN chunk_sets AS cs ON cs.chunk_set_id = c.chunk_set_id
                WHERE {" AND ".join(where)}
                ORDER BY c.message_count ASC, c.ordinal ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if chunk is None:
                return []
            rows = conn.execute(
                """
                SELECT m.*, cm.ordinal AS chunk_ordinal
                FROM chunk_memberships AS cm
                JOIN messages AS m ON m.message_uid = cm.message_uid
                WHERE cm.chunk_id = ?
                ORDER BY cm.ordinal ASC
                """,
                (chunk["chunk_id"],),
            ).fetchall()

        dict_rows = [dict(row) for row in rows]
        if max_messages is None or len(dict_rows) <= max_messages:
            return dict_rows

        anchor_index = next(
            index
            for index, row in enumerate(dict_rows)
            if row["message_uid"] == message_uid
        )
        half = max_messages // 2
        start = max(0, anchor_index - half)
        end = start + max_messages
        if end > len(dict_rows):
            end = len(dict_rows)
            start = max(0, end - max_messages)
        return dict_rows[start:end]

    def _persist_asset(
        self, conn: sqlite3.Connection, asset: CanonicalAssetRecord
    ) -> None:
        asset_row, artifact_row = self._asset_rows(asset)
        conn.execute(
            """
            INSERT OR REPLACE INTO message_assets (
                asset_id, message_uid, asset_type, file_name, path, md5,
                extra_json, future_multimodal_parse
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            asset_row,
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO artifacts (
                artifact_id, message_uid, artifact_type, path, extra_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            artifact_row,
        )

    def _asset_rows(
        self, asset: CanonicalAssetRecord
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        asset_row = (
            asset.asset_id,
            asset.message_uid,
            asset.asset_type,
            asset.file_name,
            asset.path,
            asset.md5,
            json.dumps(asset.extra, ensure_ascii=False),
            1 if asset.future_multimodal_parse else 0,
        )
        artifact_row = (
            asset.asset_id,
            asset.message_uid,
            asset.asset_type,
            asset.path,
            json.dumps(
                {
                    "file_name": asset.file_name,
                    "md5": asset.md5,
                    "future_multimodal_parse": asset.future_multimodal_parse,
                    **asset.extra,
                },
                ensure_ascii=False,
            ),
        )
        return asset_row, artifact_row

    def _flush_message_batch(
        self,
        conn: sqlite3.Connection,
        *,
        participant_raw_rows: list[tuple[str, str | None, str, str]],
        participant_alias_rows: list[tuple[str, str, str]],
        message_rows: list[tuple[Any, ...]],
        fts_delete_rows: list[tuple[str]],
        fts_insert_rows: list[tuple[Any, ...]],
        asset_rows: list[tuple[Any, ...]],
        artifact_rows: list[tuple[Any, ...]],
    ) -> None:
        if participant_raw_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO participants_raw (
                    sender_id_raw, sender_name_raw, sender_alias_id, sender_alias_label
                ) VALUES (?, ?, ?, ?)
                """,
                participant_raw_rows,
            )
            participant_raw_rows.clear()
        if participant_alias_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO participants_alias (
                    alias_id, alias_label, sender_id_raw
                ) VALUES (?, ?, ?)
                """,
                participant_alias_rows,
            )
            participant_alias_rows.clear()
        if message_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO messages (
                    message_uid, run_id, import_source, fidelity, chat_type,
                    chat_id_raw, chat_name_raw, chat_alias_id, chat_alias_label,
                    sender_id_raw, sender_name_raw, sender_alias_id, sender_alias_label,
                    message_id, message_seq, timestamp_ms, timestamp_iso,
                    content, text_content, extra_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                message_rows,
            )
            message_rows.clear()
        if fts_delete_rows:
            conn.executemany(
                "DELETE FROM messages_fts WHERE message_uid = ?",
                fts_delete_rows,
            )
            fts_delete_rows.clear()
        if fts_insert_rows:
            conn.executemany(
                """
                INSERT INTO messages_fts (
                    message_uid, run_id, chat_id_raw, chat_alias_id,
                    sender_alias_id, content, text_content
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                fts_insert_rows,
            )
            fts_insert_rows.clear()
        if asset_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO message_assets (
                    asset_id, message_uid, asset_type, file_name, path, md5,
                    extra_json, future_multimodal_parse
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                asset_rows,
            )
            asset_rows.clear()
        if artifact_rows:
            conn.executemany(
                """
                INSERT OR REPLACE INTO artifacts (
                    artifact_id, message_uid, artifact_type, path, extra_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                artifact_rows,
            )
            artifact_rows.clear()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-64000")
        conn.row_factory = sqlite3.Row
        return conn

    def _to_fts_query(self, query: str) -> str:
        terms = [item for item in re.split(r"\s+", query.strip()) if item]
        if not terms:
            return ""
        if len(terms) == 1:
            return f'"{terms[0].replace('"', '""')}"'
        return " OR ".join(f'"{item.replace('"', '""')}"' for item in terms)
