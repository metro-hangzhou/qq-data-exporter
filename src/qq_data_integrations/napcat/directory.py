from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import orjson
from pypinyin import lazy_pinyin

from qq_data_core.paths import atomic_write_bytes
from .http_client import NapCatHttpClient
from .models import ChatTarget, MetadataCache
from qq_data_core.models import EXPORT_TIMEZONE


ChatType = Literal["group", "private"]


class NapCatTargetLookupError(LookupError):
    def __init__(self, message: str, *, matches: list[ChatTarget] | None = None) -> None:
        super().__init__(message)
        self.matches = matches or []


class NapCatMetadataDirectory:
    CACHE_TTL = timedelta(minutes=5)

    def __init__(
        self,
        client: NapCatHttpClient,
        *,
        state_dir: Path,
    ) -> None:
        self._client = client
        self._state_dir = state_dir
        self._cache: dict[ChatType, MetadataCache | None] = {
            "group": None,
            "private": None,
        }

    def count(self, chat_type: ChatType) -> int:
        return len(self.get_targets(chat_type, refresh=False))

    def count_cached(self, chat_type: ChatType) -> int:
        return len(self.get_cached_targets(chat_type))

    def get_targets(self, chat_type: ChatType, *, refresh: bool = False) -> list[ChatTarget]:
        if refresh:
            self._cache[chat_type] = self._refresh(chat_type)
        elif self._cache[chat_type] is None:
            self._cache[chat_type] = self._load_cache(chat_type)
        cache = self._cache[chat_type]
        if cache is not None and self._is_stale(cache):
            with_refresh_fallback = cache
            try:
                self._cache[chat_type] = self._refresh(chat_type)
            except Exception:
                self._cache[chat_type] = with_refresh_fallback
        return list(self._cache[chat_type].targets if self._cache[chat_type] is not None else [])

    def get_cached_targets(self, chat_type: ChatType) -> list[ChatTarget]:
        if self._cache[chat_type] is None:
            self._cache[chat_type] = self._load_cache(chat_type)
        return list(self._cache[chat_type].targets if self._cache[chat_type] is not None else [])

    def search(
        self,
        chat_type: ChatType,
        keyword: str | None = None,
        *,
        limit: int = 8,
        refresh: bool = False,
    ) -> list[ChatTarget]:
        targets = self.get_targets(chat_type, refresh=refresh)
        return self._rank_targets(targets, keyword, limit=limit)

    def search_cached(
        self,
        chat_type: ChatType,
        keyword: str | None = None,
        *,
        limit: int = 8,
    ) -> list[ChatTarget]:
        targets = self.get_cached_targets(chat_type)
        return self._rank_targets(targets, keyword, limit=limit)

    def resolve(
        self,
        chat_type: ChatType,
        query: str,
        *,
        refresh_if_missing: bool = True,
    ) -> ChatTarget:
        normalized_query = query.strip()
        if not normalized_query:
            raise NapCatTargetLookupError("Missing target query")

        matches = self._find_exact_or_ranked(chat_type, normalized_query, refresh=False)
        if not matches and refresh_if_missing:
            matches = self._find_exact_or_ranked(chat_type, normalized_query, refresh=True)

        exact_id_matches = [item for item in matches if item.chat_id == normalized_query]
        if len(exact_id_matches) == 1:
            return exact_id_matches[0]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NapCatTargetLookupError(
                f"No {self._chat_label(chat_type)} matched {normalized_query!r}"
            )

        raise NapCatTargetLookupError(
            f"Ambiguous {self._chat_label(chat_type)} target {normalized_query!r}",
            matches=matches[:5],
        )

    def _find_exact_or_ranked(
        self,
        chat_type: ChatType,
        query: str,
        *,
        refresh: bool,
    ) -> list[ChatTarget]:
        targets = self.get_targets(chat_type, refresh=refresh)
        normalized_query = _normalize_search_value(query)
        exact_matches = [
            target
            for target in targets
            if any(key == normalized_query for _, key, _ in _iter_search_keys(target))
        ]
        if exact_matches:
            return self._sort_targets(exact_matches)
        return self._rank_targets(targets, query, limit=5)

    def _refresh(self, chat_type: ChatType) -> MetadataCache:
        raw_targets = (
            self._client.get_group_list(no_cache=True)
            if chat_type == "group"
            else self._client.get_friend_list(no_cache=True)
        )
        cache = MetadataCache(
            chat_type=chat_type,
            targets=self._normalize_targets(chat_type, raw_targets),
        )
        self._cache[chat_type] = cache
        self._persist_cache(chat_type, cache)
        return cache

    def _load_cache(self, chat_type: ChatType) -> MetadataCache | None:
        path = self._cache_path(chat_type)
        if not path.exists():
            return None
        try:
            cache = MetadataCache.model_validate(orjson.loads(path.read_bytes()))
        except Exception:
            return None
        self._cache[chat_type] = cache
        return cache

    def _persist_cache(self, chat_type: ChatType, cache: MetadataCache) -> None:
        path = self._cache_path(chat_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, orjson.dumps(cache.model_dump(mode="json"), option=orjson.OPT_INDENT_2))

    def _is_stale(self, cache: MetadataCache) -> bool:
        refreshed_at = cache.refreshed_at
        if refreshed_at is None:
            return True
        try:
            refreshed = refreshed_at.astimezone(EXPORT_TIMEZONE)
        except Exception:
            return True
        return datetime.now(EXPORT_TIMEZONE) - refreshed > self.CACHE_TTL

    def _cache_path(self, chat_type: ChatType) -> Path:
        file_name = "groups.json" if chat_type == "group" else "friends.json"
        return self._state_dir / "metadata" / file_name

    def _normalize_targets(self, chat_type: ChatType, payload: Any) -> list[ChatTarget]:
        if not isinstance(payload, list):
            return []
        targets: list[ChatTarget] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if chat_type == "group":
                target = self._normalize_group(item)
            else:
                target = self._normalize_friend(item)
            if target is not None:
                targets.append(target)
        return self._sort_targets(targets)

    def _normalize_group(self, item: dict[str, Any]) -> ChatTarget | None:
        group_id = str(item.get("group_id") or "").strip()
        if not group_id:
            return None
        group_name = str(item.get("group_name") or group_id).strip() or group_id
        aliases = [group_name]
        return ChatTarget(
            chat_type="group",
            chat_id=group_id,
            name=group_name,
            aliases=aliases,
            member_count=item.get("member_count"),
            extra={"max_member_count": item.get("max_member_count")},
        )

    def _normalize_friend(self, item: dict[str, Any]) -> ChatTarget | None:
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            return None
        nickname = str(
            item.get("nickname")
            or item.get("nick")
            or item.get("showName")
            or item.get("remark")
            or user_id
        ).strip() or user_id
        remark = str(item.get("remark") or "").strip() or None
        aliases = [
            value
            for value in [
                item.get("nickname"),
                item.get("nick"),
                item.get("showName"),
                item.get("remark"),
            ]
            if isinstance(value, str) and value.strip()
        ]
        return ChatTarget(
            chat_type="private",
            chat_id=user_id,
            name=nickname,
            remark=remark,
            aliases=aliases,
            extra={
                "uid": item.get("uid"),
                "qq_level": item.get("qqLevel"),
            },
        )

    def _rank_targets(
        self,
        targets: list[ChatTarget],
        keyword: str | None,
        *,
        limit: int,
    ) -> list[ChatTarget]:
        if not keyword:
            return self._sort_targets(targets)[:limit]

        normalized_keyword = _normalize_search_value(keyword)
        if not normalized_keyword:
            return self._sort_targets(targets)[:limit]

        ranked: list[tuple[tuple[int, int, str, str], ChatTarget]] = []
        for target in targets:
            score = self._score_target(target, normalized_keyword)
            if score is None:
                continue
            ranked.append((score, target))
        ranked.sort(key=lambda item: item[0])
        return [target for _, target in ranked[:limit]]

    def _score_target(
        self,
        target: ChatTarget,
        keyword: str,
    ) -> tuple[int, int, str, str] | None:
        best: tuple[int, int] | None = None
        for source_rank, key, source_length in _iter_search_keys(target):
            if key == keyword:
                candidate = (source_rank * 3, source_length)
            elif key.startswith(keyword):
                candidate = (source_rank * 3 + 1, source_length)
            elif keyword in key:
                candidate = (source_rank * 3 + 2, source_length)
            else:
                continue
            if best is None or candidate < best:
                best = candidate
        if best is None:
            return None
        return (
            best[0],
            best[1],
            target.display_name.casefold(),
            target.chat_id,
        )

    def _sort_targets(self, targets: list[ChatTarget]) -> list[ChatTarget]:
        return sorted(targets, key=lambda item: (item.display_name.casefold(), item.chat_id))

    def _chat_label(self, chat_type: ChatType) -> str:
        return "group" if chat_type == "group" else "friend"


def _iter_search_keys(target: ChatTarget) -> list[tuple[int, str, int]]:
    seen: set[tuple[int, str]] = set()
    results: list[tuple[int, str, int]] = []
    for term in target.searchable_terms():
        normalized = _normalize_search_value(term)
        if normalized:
            key = (0, normalized)
            if key not in seen:
                seen.add(key)
                results.append((0, normalized, len(term)))
        for pinyin_key in _build_pinyin_keys(term):
            key = (pinyin_key[0], pinyin_key[1])
            if key in seen:
                continue
            seen.add(key)
            results.append((pinyin_key[0], pinyin_key[1], len(term)))
    return results


def _normalize_search_value(value: str) -> str:
    return "".join(char for char in value.casefold().strip() if not char.isspace())


def _build_pinyin_keys(value: str) -> list[tuple[int, str]]:
    if not any(_is_cjk(char) for char in value):
        return []

    syllables = [item for item in lazy_pinyin(value, errors="ignore") if item]
    if not syllables:
        return []

    keys: list[tuple[int, str]] = []
    full = _normalize_search_value("".join(syllables))
    if full:
        keys.append((1, full))
    initials = _normalize_search_value("".join(item[0] for item in syllables if item))
    if initials and initials != full:
        keys.append((2, initials))
    return keys


def _is_cjk(char: str) -> bool:
    code_point = ord(char)
    return (
        0x4E00 <= code_point <= 0x9FFF
        or 0x3400 <= code_point <= 0x4DBF
        or 0x20000 <= code_point <= 0x2A6DF
        or 0x2A700 <= code_point <= 0x2B73F
        or 0x2B740 <= code_point <= 0x2B81F
        or 0x2B820 <= code_point <= 0x2CEAF
        or 0xF900 <= code_point <= 0xFAFF
    )
