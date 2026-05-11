from __future__ import annotations

from dataclasses import dataclass

from .models import (
    CanonicalMessageRecord,
    IdentityMode,
    IdentityProjection,
    IdentityProjectionPolicy,
)
from .utils import stable_digest


class DangerousIdentityAccessError(RuntimeError):
    pass


@dataclass(slots=True)
class IdentityView:
    chat: IdentityProjection
    sender: IdentityProjection


class IdentityProjector:
    def alias_for(self, entity_type: str, raw_id: str) -> IdentityProjection:
        prefix = "chat" if entity_type == "chat" else "user"
        digest = stable_digest(entity_type, raw_id, length=10)
        return IdentityProjection(
            entity_type="chat" if entity_type == "chat" else "sender",
            raw_id=raw_id,
            alias_id=f"{prefix}_{digest}",
            alias_label=f"{prefix}_{digest}",
        )

    def project(self, message: CanonicalMessageRecord) -> IdentityView:
        return IdentityView(
            chat=self.alias_for("chat", message.chat_id),
            sender=self.alias_for("sender", message.sender_id_raw),
        )

    def project_message_fields(
        self,
        *,
        message: CanonicalMessageRecord,
        policy: IdentityProjectionPolicy,
        mode: IdentityMode | None = None,
    ) -> dict[str, str | None]:
        selected = mode or policy.default_mode
        if selected == "raw":
            if not policy.danger_allow_raw_identity_output:
                raise DangerousIdentityAccessError(
                    "Raw identity output requires danger_allow_raw_identity_output=True."
                )
            return {
                "chat_id": message.chat_id,
                "chat_name": message.chat_name,
                "sender_id": message.sender_id_raw,
                "sender_name": message.sender_name_raw,
            }

        view = self.project(message)
        return {
            "chat_id": view.chat.alias_id,
            "chat_name": view.chat.alias_label,
            "sender_id": view.sender.alias_id,
            "sender_name": view.sender.alias_label,
        }
