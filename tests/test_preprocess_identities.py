from __future__ import annotations

import pytest

from qq_data_process.identities import DangerousIdentityAccessError, IdentityProjector
from qq_data_process.models import (
    CanonicalMessageRecord,
    IdentityMode,
    IdentityProjectionPolicy,
)


def _message(sender_id: str) -> CanonicalMessageRecord:
    return CanonicalMessageRecord(
        message_uid=f"msg_{sender_id}",
        import_source="exporter_jsonl",
        fidelity="high",
        chat_type="private",
        chat_id="chat_1",
        chat_name="示例",
        sender_id_raw=sender_id,
        sender_name_raw="name",
        timestamp_ms=1_000,
        timestamp_iso="2026-03-07T00:00:01+08:00",
        content="hello",
        text_content="hello",
    )


def test_alias_projection_is_globally_stable() -> None:
    projector = IdentityProjector()
    first = projector.project(_message("user_1"))
    second = projector.project(_message("user_1"))
    assert first.sender.alias_id == second.sender.alias_id
    assert first.chat.alias_id == second.chat.alias_id


def test_raw_projection_requires_danger_flag() -> None:
    projector = IdentityProjector()
    message = _message("user_1")

    with pytest.raises(DangerousIdentityAccessError):
        projector.project_message_fields(
            message=message,
            policy=IdentityProjectionPolicy(),
            mode="raw",
        )

    projected = projector.project_message_fields(
        message=message,
        policy=IdentityProjectionPolicy(danger_allow_raw_identity_output=True),
        mode="raw",
    )
    assert projected["sender_id"] == "user_1"


def test_alias_is_default_projection_mode() -> None:
    """Verify alias is the safe default for Benshi-facing outputs."""
    projector = IdentityProjector()
    message = _message("user_123456789")

    # Default policy should use alias mode
    projected = projector.project_message_fields(
        message=message,
        policy=IdentityProjectionPolicy(),  # No mode override, uses default
    )

    # Should be alias-safe by default
    assert projected["sender_id"].startswith("user_")
    assert not projected["sender_id"].isdigit()
    assert projected["chat_id"].startswith("chat_")
    assert not projected["chat_id"].isdigit()


def test_none_mode_falls_back_to_policy_default() -> None:
    """Verify None mode uses policy default instead of hardcoded fallback."""
    projector = IdentityProjector()
    message = _message("user_123")

    # Explicit None should use policy default (alias)
    projected = projector.project_message_fields(
        message=message,
        policy=IdentityProjectionPolicy(),
        mode=None,
    )

    assert projected["sender_id"].startswith("user_")
    assert not projected["sender_id"].isdigit()


def test_benshi_output_never_contains_raw_ids_by_default() -> None:
    """Ensure raw QQ identities don't leak into default Benshi paths."""
    projector = IdentityProjector()
    sender_ids = ["987654321", "111222333", "555666777"]

    for sender_id in sender_ids:
        message = _message(sender_id)
        projected = projector.project_message_fields(
            message=message,
            policy=IdentityProjectionPolicy(),
        )

        # Raw numeric IDs should never appear in default alias mode
        assert sender_id not in projected["sender_id"]
        assert sender_id not in projected["chat_id"]
        assert not projected["sender_id"].isdigit()
        assert not projected["chat_id"].isdigit()
