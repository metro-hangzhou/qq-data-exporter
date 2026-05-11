from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


_SCOPE_ORDER = {
    "message": 0,
    "thread": 1,
    "asset": 2,
    "topic": 3,
    "corpus": 4,
}


class PreprocessorPlugin(Protocol):
    plugin_id: str
    plugin_version: str
    scope_level: str
    requires: Sequence[str]
    produces: Sequence[str]
    supported_modalities: Sequence[str]

    def run(self, context: Any) -> list[Any]:
        """Execute preprocessor logic within the provided context."""


@dataclass(frozen=True)
class PreprocessorSpec:
    preprocessor_id: str
    plugin: PreprocessorPlugin
    plugin_version: str
    scope_level: str
    requires: tuple[str, ...]
    produces: tuple[str, ...]
    supported_modalities: tuple[str, ...]

    @property
    def scope_rank(self) -> int:
        return _SCOPE_ORDER.get(self.scope_level, len(_SCOPE_ORDER) + 1)


class PreprocessorRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, PreprocessorSpec] = {}

    def register(
        self,
        plugin: PreprocessorPlugin,
        *,
        preprocessor_id: str | None = None,
    ) -> PreprocessorSpec:
        resolved_id = preprocessor_id or getattr(plugin, "plugin_id", None)
        if not resolved_id:
            raise ValueError("Preprocessor plugin must define plugin_id or preprocessor_id")
        if resolved_id in self._specs:
            raise ValueError(f"Preprocessor already registered: {resolved_id}")
        scope_level = str(getattr(plugin, "scope_level", "") or "").strip() or "message"
        spec = PreprocessorSpec(
            preprocessor_id=resolved_id,
            plugin=plugin,
            plugin_version=str(getattr(plugin, "plugin_version", "0.0.0")),
            scope_level=scope_level,
            requires=tuple(getattr(plugin, "requires", ()) or ()),
            produces=tuple(getattr(plugin, "produces", ()) or ()),
            supported_modalities=tuple(getattr(plugin, "supported_modalities", ()) or ()),
        )
        self._specs[resolved_id] = spec
        return spec

    def get(self, preprocessor_id: str) -> PreprocessorSpec:
        try:
            return self._specs[preprocessor_id]
        except KeyError as exc:
            raise KeyError(f"Unknown preprocessor: {preprocessor_id}") from exc

    def list(self) -> list[PreprocessorSpec]:
        return [self._specs[key] for key in sorted(self._specs)]

    def resolve_execution_order(
        self,
        preprocessor_ids: Sequence[str] | None = None,
    ) -> list[PreprocessorSpec]:
        target_ids = tuple(preprocessor_ids) if preprocessor_ids else tuple(self._specs)
        expanded_ids = self._expand_dependencies(target_ids)
        ordered_ids = self._toposort(expanded_ids)
        return [self._specs[item] for item in ordered_ids]

    def to_payload(self) -> dict[str, Any]:
        return {
            "preprocessors": [
                {
                    "preprocessor_id": spec.preprocessor_id,
                    "plugin_version": spec.plugin_version,
                    "scope_level": spec.scope_level,
                    "requires": list(spec.requires),
                    "produces": list(spec.produces),
                    "supported_modalities": list(spec.supported_modalities),
                }
                for spec in self.list()
            ]
        }

    def _expand_dependencies(self, preprocessor_ids: Sequence[str]) -> set[str]:
        resolved: set[str] = set()

        def visit(preprocessor_id: str) -> None:
            spec = self.get(preprocessor_id)
            if preprocessor_id in resolved:
                return
            for dependency in spec.requires:
                visit(dependency)
            resolved.add(preprocessor_id)

        for preprocessor_id in preprocessor_ids:
            visit(preprocessor_id)
        return resolved

    def _toposort(self, preprocessor_ids: set[str]) -> list[str]:
        ordered: list[str] = []
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(preprocessor_id: str) -> None:
            if preprocessor_id in permanent:
                return
            if preprocessor_id in temporary:
                raise ValueError(f"Dependency cycle detected at preprocessor: {preprocessor_id}")
            temporary.add(preprocessor_id)
            spec = self.get(preprocessor_id)
            dependencies = sorted(
                spec.requires,
                key=lambda item: (self.get(item).scope_rank, item),
            )
            for dependency in dependencies:
                visit(dependency)
            temporary.remove(preprocessor_id)
            permanent.add(preprocessor_id)
            ordered.append(preprocessor_id)

        for preprocessor_id in sorted(
            preprocessor_ids,
            key=lambda item: (self.get(item).scope_rank, item),
        ):
            visit(preprocessor_id)
        return ordered


def register_preprocessor(
    registry: PreprocessorRegistry,
    plugin: PreprocessorPlugin,
    *,
    preprocessor_id: str | None = None,
) -> PreprocessorSpec:
    return registry.register(plugin, preprocessor_id=preprocessor_id)
