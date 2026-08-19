"""Adapter registry for hermes-plugin-relay.

Only stdlib imports here so the package stays importable in a plain CLI process
with no Hermes core, no FastAPI and no provider CLI installed.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Type

from .base import (
    AdapterCapabilities,
    AdapterError,
    AdapterNotAvailableError,
    AgentAdapter,
    Availability,
    LineProcessAdapter,
    TurnInput,
)
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .mock import MockAdapter

#: Adapter kind (as written in config) -> implementation class.
ADAPTERS: Mapping[str, Type[LineProcessAdapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "mock": MockAdapter,
}


class UnknownAdapterError(AdapterError):
    """Configuration named an adapter kind that does not exist."""


def get_adapter_class(kind: str) -> Type[LineProcessAdapter]:
    try:
        return ADAPTERS[kind]
    except KeyError as exc:
        known = ", ".join(sorted(ADAPTERS))
        raise UnknownAdapterError(f"unknown adapter {kind!r}; known adapters: {known}") from exc


def build_adapter(kind: str, **kwargs: Any) -> LineProcessAdapter:
    return get_adapter_class(kind)(**kwargs)


def adapter_availability(kind: str) -> Availability:
    """Cheap readiness probe for an adapter kind. Never raises."""

    try:
        return get_adapter_class(kind).availability()
    except UnknownAdapterError as exc:
        return Availability("error", str(exc))
    except Exception as exc:  # noqa: BLE001 - a probe must never break the roster
        return Availability("error", f"availability probe failed: {exc}")


def adapter_capabilities(kind: str) -> Dict[str, bool]:
    try:
        return get_adapter_class(kind).capabilities.to_dict()
    except UnknownAdapterError:
        return AdapterCapabilities(text=False).to_dict()


__all__ = [
    "ADAPTERS",
    "AdapterCapabilities",
    "AdapterError",
    "AdapterNotAvailableError",
    "AgentAdapter",
    "Availability",
    "ClaudeAdapter",
    "CodexAdapter",
    "LineProcessAdapter",
    "MockAdapter",
    "TurnInput",
    "UnknownAdapterError",
    "adapter_availability",
    "adapter_capabilities",
    "build_adapter",
    "get_adapter_class",
]
