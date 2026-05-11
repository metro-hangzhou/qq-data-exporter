from __future__ import annotations

from typing import Literal

PreprocessOperationType = Literal[
    "annotate",
    "compact",
    "classify",
    "group",
    "infer",
    "suppress",
]

DeliveryProfile = Literal[
    "raw_only",
    "processed_only",
    "raw_plus_processed",
]

PreprocessScopeLevel = Literal[
    "message",
    "thread",
    "asset",
    "topic",
    "corpus",
]

ProcessedViewKind = Literal[
    "raw_view",
    "processed_view",
    "compact_view",
    "topic_view",
    "meme_view",
    "repost_view",
    "expired_asset_context_view",
]

