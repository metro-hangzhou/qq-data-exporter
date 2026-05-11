from __future__ import annotations

from datetime import datetime, timedelta

from qq_data_cli.export_commands import (
    interval_is_full_history,
    interval_needs_history_bounds,
    parse_root_export_command,
    parse_watch_export_command,
    resolve_interval,
)
from qq_data_core.models import EXPORT_TIMEZONE
from qq_data_integrations.napcat import ChatHistoryBounds


def test_parse_root_export_command_with_interval() -> None:
    parsed = parse_root_export_command(
        "/export",
        ["friend", "菜鸡", "@final_content", "2026-03-01_00-00-00"],
        {"format": "txt", "limit": "50"},
        default_limit=20,
    )

    assert parsed.chat_type == "friend"
    assert parsed.target_query == "菜鸡"
    assert parsed.interval is not None
    assert parsed.fmt == "txt"
    assert parsed.limit == 50
    assert parsed.profile == "all"
    assert parsed.strict_missing is None


def test_parse_watch_export_command_accepts_interval() -> None:
    parsed = parse_watch_export_command(
        "/export",
        ["@earliest_content", "@final_content-1d"],
        {},
        default_limit=80,
    )

    assert parsed.interval is not None
    assert parsed.limit == 80
    assert parsed.profile == "all"


def test_parse_root_export_command_uses_jsonl_default_and_alias_override() -> None:
    default_fmt = parse_root_export_command(
        "/export",
        ["friend", "菜鸡"],
        {},
        default_limit=20,
    )
    alias_fmt = parse_root_export_command(
        "/export",
        ["friend", "菜鸡", "asJSONL"],
        {},
        default_limit=20,
    )

    assert default_fmt.fmt == "jsonl"
    assert alias_fmt.fmt == "jsonl"


def test_parse_watch_export_command_uses_jsonl_default() -> None:
    parsed = parse_watch_export_command(
        "/export",
        [],
        {},
        default_limit=80,
    )

    assert parsed.fmt == "jsonl"


def test_parse_watch_export_command_accepts_format_alias() -> None:
    parsed = parse_watch_export_command(
        "/export",
        ["@final_content", "@earliest_content", "asTXT"],
        {},
        default_limit=80,
    )

    assert parsed.fmt == "txt"
    assert parsed.interval is not None


def test_resolve_interval_accepts_unordered_bounds() -> None:
    final = datetime(2026, 3, 7, 12, 0, 0, tzinfo=EXPORT_TIMEZONE)
    earliest = final - timedelta(days=30)
    start, end = resolve_interval(
        parse_root_export_command(
            "/export",
            ["friend", "菜鸡", "@final_content", "@final_content-1d"],
            {},
            default_limit=20,
        ).interval,
        bounds=ChatHistoryBounds(
            earliest_content_at=earliest,
            final_content_at=final,
        ),
    )

    assert start == final
    assert end == final - timedelta(days=1)


def test_interval_needs_history_bounds_for_special_tokens_only() -> None:
    explicit_interval = parse_watch_export_command(
        "/export",
        ["2026-03-07_14-00-21", "2026-03-05_23-59-59"],
        {},
        default_limit=80,
    ).interval
    special_interval = parse_watch_export_command(
        "/export",
        ["@final_content", "2026-03-05_23-59-59"],
        {},
        default_limit=80,
    ).interval

    assert explicit_interval is not None
    assert special_interval is not None
    assert interval_needs_history_bounds(explicit_interval) is False
    assert interval_needs_history_bounds(special_interval) is True


def test_interval_is_full_history_only_for_zero_offset_special_pair() -> None:
    full_interval = parse_watch_export_command(
        "/export",
        ["@final_content", "@earliest_content"],
        {},
        default_limit=80,
    ).interval
    offset_interval = parse_watch_export_command(
        "/export",
        ["@final_content-1d", "@earliest_content"],
        {},
        default_limit=80,
    ).interval

    assert full_interval is not None
    assert offset_interval is not None
    assert interval_is_full_history(full_interval) is True
    assert interval_is_full_history(offset_interval) is False


def test_parse_root_export_command_supports_data_count_and_profile_alias() -> None:
    parsed = parse_root_export_command(
        "/export_TextImageEmoji",
        ["group", "测试群", "data_count=123", "asJSONL"],
        {"strict-missing": "collect"},
        default_limit=20,
    )

    assert parsed.chat_type == "group"
    assert parsed.target_query == "测试群"
    assert parsed.data_count == 123
    assert parsed.fmt == "jsonl"
    assert parsed.profile == "text_image_emoji"
    assert parsed.strict_missing == "collect"


def test_parse_watch_export_command_supports_option_data_count() -> None:
    parsed = parse_watch_export_command(
        "/export_onlyText",
        ["@final_content", "@earliest_content"],
        {"data-count": "88"},
        default_limit=80,
    )

    assert parsed.data_count == 88
    assert parsed.profile == "only_text"


def test_parse_root_export_command_accepts_non_padded_explicit_dates() -> None:
    parsed = parse_root_export_command(
        "/export",
        ["friend", "菜鸡", "2026-3-09_00-00-00", "2026-3-10_00-00-00"],
        {},
        default_limit=20,
    )

    assert parsed.interval is not None
    assert parsed.interval.start_token == "2026-3-09_00-00-00"
    assert parsed.interval.end_token == "2026-3-10_00-00-00"


def test_parse_root_export_command_supports_batch_targets() -> None:
    parsed = parse_root_export_command(
        "/export",
        ["group_asBatch=蕾米二次元萌萌群,哈基米开发群,悦之声女子计算机学院", "@final_content", "@earliest_content"],
        {},
        default_limit=20,
    )

    assert parsed.chat_type == "group"
    assert parsed.target_query is None
    assert parsed.batch_target_queries == (
        "蕾米二次元萌萌群",
        "哈基米开发群",
        "悦之声女子计算机学院",
    )
    assert parsed.interval is not None


def test_parse_root_export_command_supports_quoted_batch_targets() -> None:
    parsed = parse_root_export_command(
        "/export",
        ["friend_asBatch='Alpha Friend','Beta Lab'"],
        {},
        default_limit=20,
    )

    assert parsed.chat_type == "friend"
    assert parsed.batch_target_queries == ("Alpha Friend", "Beta Lab")


def test_parse_root_export_command_merges_space_split_batch_target_before_interval() -> None:
    parsed = parse_root_export_command(
        "/export",
        [
            "group_asBatch=VOID-TECH",
            "O.OO实验室",
            "@final_content",
            "@earliest_content",
            "asJSONL",
        ],
        {},
        default_limit=20,
    )

    assert parsed.chat_type == "group"
    assert parsed.batch_target_queries == ("VOID-TECH O.OO实验室",)
    assert parsed.interval is not None
    assert parsed.interval.start_token == "@final_content"
    assert parsed.interval.end_token == "@earliest_content"
    assert parsed.fmt == "jsonl"
