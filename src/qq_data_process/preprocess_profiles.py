from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from qq_data_process.preprocess_models import PreprocessDirective
from qq_data_process.preprocess_types import DeliveryProfile

__all__ = [
    "PreprocessProfile",
    "PREPROCESS_PROFILES",
    "build_preprocessors_for_profile",
    "context_filter_directive_for_profile",
    "get_preprocess_profile",
    "list_preprocess_profiles",
]


class PreprocessProfile(BaseModel):
    profile_id: str
    title: str
    description: str
    delivery_profile: DeliveryProfile
    plugin_ids: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    directive: PreprocessDirective | None = None


def _directive_with_defaults(
    *,
    directive_id: str,
    title: str,
    analysis_goal: str | None,
    relevance_policy: str,
    preserve_evidence_window: int,
    target_topics: list[str],
    suppress_topics: list[str],
    suppress_message_patterns: list[str],
    retain_modalities: list[str] | None = None,
) -> PreprocessDirective:
    return PreprocessDirective(
        directive_id=directive_id,
        title=title,
        analysis_goal=analysis_goal,
        relevance_policy=relevance_policy,
        noise_handling_mode="compact_cluster",
        preserve_evidence_window=preserve_evidence_window,
        suppress_non_target_chatter=True,
        prefer_compaction_over_deletion=True,
        preserve_reply_context=True,
        preserve_media_neighbors=True,
        target_topics=target_topics,
        suppress_topics=suppress_topics,
        suppress_message_patterns=suppress_message_patterns,
        retain_modalities=retain_modalities or ["image", "video", "file", "speech", "forward"],
    )


PREPROCESS_PROFILES: dict[str, PreprocessProfile] = {
    "raw_only": PreprocessProfile(
        profile_id="raw_only",
        title="Raw Only",
        description="完全保留 bypass/raw 视图，不运行任何预处理插件。",
        delivery_profile="raw_only",
        plugin_ids=[],
        options={
            "preserve_raw_truth": True,
            "write_processed_view": False,
        },
        budgets={},
        tags=["baseline", "forensics", "truth-layer"],
    ),
    "compact_default": PreprocessProfile(
        profile_id="compact_default",
        title="Compact Default",
        description="面向通用群聊分析的保守预处理组合：先过滤明显的调试/开发噪音，再做主题窗口与低信息密度线程压缩。",
        delivery_profile="processed_only",
        plugin_ids=[
            "context_filter_preprocessor",
            "topic_window_builder",
            "thread_compaction_preprocessor",
        ],
        options={
            "write_processed_view": True,
            "keep_source_refs": True,
            "before_messages": 4,
            "after_messages": 4,
        },
        budgets={
            "max_thread_compactions_per_run": 500,
        },
        tags=["general-analysis", "compact", "deterministic"],
        directive=_directive_with_defaults(
            directive_id="compact_default_context_filter",
            title="General chatter compaction",
            analysis_goal="Compact development chatter while preserving evidence-bearing media, files, and forward bundles.",
            relevance_policy="balanced",
            preserve_evidence_window=2,
            target_topics=["话题", "讨论", "转发", "图片", "视频", "文件", "群上传", "素材", "来源"],
            suppress_topics=["调试", "日志", "报错", "更新", "脚本", "代理", "CLI", "NapCat", "OneBot"],
            suppress_message_patterns=[
                "debug",
                "log",
                "error",
                "traceback",
                "git",
                "pull",
                "push",
                "start cli",
                "watch",
                "export",
                "login",
                "onebot",
                "napcat",
                "二维码",
                "代理",
                "rag",
                "llm",
                "api",
            ],
        ),
    ),
    "meme_focus": PreprocessProfile(
        profile_id="meme_focus",
        title="Meme Focus",
        description="面向搬 shi / repost 分析的默认组合，先压缩调试/开发噪音，再兼顾主题窗口、资源复现、forward 展开和失活内容上下文准备。",
        delivery_profile="raw_plus_processed",
        plugin_ids=[
            "context_filter_preprocessor",
            "topic_window_builder",
            "thread_compaction_preprocessor",
            "asset_recurrence_preprocessor",
            "forward_bundle_expander",
            "expired_asset_inference_preprocessor",
        ],
        options={
            "write_processed_view": True,
            "keep_source_refs": True,
            "before_messages": 6,
            "after_messages": 6,
            "same_asset_occurrences": True,
            "forward_full_bundle": True,
        },
        budgets={
            "max_rounds": 3,
            "max_total_context_messages": 80,
            "max_total_asset_refs": 20,
            "max_runtime_s": 45,
        },
        tags=["meme", "repost", "expired-assets", "hybrid"],
        directive=_directive_with_defaults(
            directive_id="meme_focus_context_filter",
            title="Meme / repost signal preservation",
            analysis_goal="Preserve repost, forward, dump, media propagation, and expired-resource evidence while compacting debugging/dev chatter in mixed-purpose groups.",
            relevance_policy="meme_focus",
            preserve_evidence_window=3,
            retain_modalities=["image", "video", "file", "speech", "forward"],
            target_topics=[
                "搬",
                "转发",
                "repost",
                "forward",
                "dump",
                "素材",
                "出处",
                "来源",
                "视频",
                "动图",
                "图片",
                "文件",
                "群上传",
                "封面",
                "资源过期",
                "下载按钮",
                "shi",
            ],
            suppress_topics=["调试", "报错", "日志", "更新", "脚本", "代理", "配置", "CLI", "NapCat", "OneBot"],
            suppress_message_patterns=[
                "debug",
                "log",
                "error",
                "traceback",
                "git",
                "pull",
                "push",
                "onebot",
                "napcat",
                "start cli",
                "watch",
                "export",
                "login",
                "二维码",
                "代理",
                "更新成功",
                "rag",
                "llm",
                "embedding",
                "openai",
                "gpt",
                "数据库",
                "向量",
                "api",
            ],
        ),
    ),
    "shi_focus": PreprocessProfile(
        profile_id="shi_focus",
        title="Shi Focus",
        description="面向 `史数据统计群` 这类 mixed-purpose 搬shi/开发对接群的更严格预处理组合：尽量保留搬 shi forward、独立图片/视频/文件和相关反应，同时更激进地压缩开发调试 chatter。",
        delivery_profile="raw_plus_processed",
        plugin_ids=[
            "context_filter_preprocessor",
            "topic_window_builder",
            "thread_compaction_preprocessor",
            "asset_recurrence_preprocessor",
            "forward_bundle_expander",
            "expired_asset_inference_preprocessor",
        ],
        options={
            "write_processed_view": True,
            "keep_source_refs": True,
            "before_messages": 8,
            "after_messages": 8,
            "same_asset_occurrences": True,
            "forward_full_bundle": True,
        },
        budgets={
            "max_rounds": 3,
            "max_total_context_messages": 100,
            "max_total_asset_refs": 24,
            "max_runtime_s": 60,
        },
        tags=["shi", "strict-focus", "repost", "hybrid"],
        directive=_directive_with_defaults(
            directive_id="shi_focus_context_filter",
            title="Shi-focused context filter",
            analysis_goal="For mixed-purpose dump-and-debug groups, preserve concentrated 搬shi forward/media/text content while compacting later development, debugging, CLI, NapCat, OneBot, proxy, and update chatter.",
            relevance_policy="strict_focus",
            preserve_evidence_window=2,
            retain_modalities=["image", "video", "file", "speech", "forward"],
            target_topics=[
                "搬",
                "转发",
                "repost",
                "forward",
                "dump",
                "素材",
                "出处",
                "来源",
                "图片",
                "动图",
                "视频",
                "文件",
                "群上传",
                "下载按钮",
                "资源过期",
                "shi",
            ],
            suppress_topics=["调试", "日志", "报错", "更新", "脚本", "代理", "配置", "CLI", "NapCat", "OneBot"],
            suppress_message_patterns=[
                "debug",
                "log",
                "error",
                "traceback",
                "git",
                "pull",
                "push",
                "start cli",
                "start_cli",
                "watch",
                "export",
                "login",
                "二维码",
                "代理",
                "节点",
                "onebot",
                "napcat",
                "cli",
                "api",
                "llm",
                "rag",
                "prompt",
                "embedding",
                "openai",
                "gpt",
                "向量",
                "数据库",
            ],
        ),
    ),
    "raw_plus_processed": PreprocessProfile(
        profile_id="raw_plus_processed",
        title="Raw Plus Processed",
        description="同时保留 raw 证据和 processed 派生视图，先压缩调试/开发噪音，适合证据敏感但又需要摘要/压缩的下游分析。",
        delivery_profile="raw_plus_processed",
        plugin_ids=[
            "context_filter_preprocessor",
            "topic_window_builder",
            "thread_compaction_preprocessor",
        ],
        options={
            "write_processed_view": True,
            "keep_source_refs": True,
            "emit_raw_bindings": True,
            "before_messages": 4,
            "after_messages": 4,
        },
        budgets={
            "max_thread_compactions_per_run": 500,
        },
        tags=["hybrid", "evidence-aware", "default"],
        directive=_directive_with_defaults(
            directive_id="raw_plus_processed_context_filter",
            title="Hybrid evidence-preserving filter",
            analysis_goal="Emit compacted derived clusters for low-value debugging chatter while keeping raw evidence available downstream.",
            relevance_policy="balanced",
            preserve_evidence_window=2,
            retain_modalities=["image", "video", "file", "speech", "forward"],
            target_topics=["转发", "图片", "视频", "文件", "群上传", "素材", "出处", "来源"],
            suppress_topics=["调试", "日志", "报错", "更新", "脚本", "CLI", "NapCat", "OneBot"],
            suppress_message_patterns=[
                "debug",
                "log",
                "error",
                "traceback",
                "git",
                "pull",
                "watch",
                "export",
                "login",
                "代理",
                "onebot",
                "napcat",
                "cli",
                "二维码",
            ],
        ),
    ),
}


def get_preprocess_profile(profile_id: str) -> PreprocessProfile:
    try:
        return PREPROCESS_PROFILES[profile_id].model_copy(deep=True)
    except KeyError as exc:
        raise KeyError(f"Unknown preprocess profile: {profile_id}") from exc


def list_preprocess_profiles() -> list[PreprocessProfile]:
    return [profile.model_copy(deep=True) for _, profile in sorted(PREPROCESS_PROFILES.items())]


def context_filter_directive_for_profile(profile_id: str) -> PreprocessDirective | None:
    profile = get_preprocess_profile(profile_id)
    return profile.directive.model_copy(deep=True) if profile.directive is not None else None


def build_preprocessors_for_profile(profile_id: str, *, strict: bool = False) -> list[object]:
    profile = get_preprocess_profile(profile_id)
    from qq_data_analysis.preprocessors import (
        available_preprocessor_factories,
        build_preprocessor_plugins,
    )

    if strict:
        return build_preprocessor_plugins(profile.plugin_ids)
    available = available_preprocessor_factories()
    plugin_ids = [plugin_id for plugin_id in profile.plugin_ids if plugin_id in available]
    return build_preprocessor_plugins(plugin_ids)
