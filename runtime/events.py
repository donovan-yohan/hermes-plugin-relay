"""Normalized adapter event vocabulary (participant seam contract v1 section 8).

Adapters translate provider-native protocol frames into exactly these four
events. Reasoning, tool activity and approval traffic may exist inside an
adapter, but slice 1 does not surface them through the Hermes seam.

This module has no dependencies beyond the standard library so that it can be
imported from both adapter and runtime code without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

#: Terminal status values allowed on ``TurnCompleted``.
TURN_STATUSES = ("completed", "failed", "interrupted")


@dataclass(frozen=True)
class SessionUpdated:
    """The provider handed us a durable session/thread id we can resume with."""

    provider_session_id: str
    type: str = "session_updated"


@dataclass(frozen=True)
class TurnStarted:
    """The provider accepted the turn.

    Provider-native turn ids stay inside the adapter that needs them (Codex
    uses one for ``turn/interrupt``); correlation across the seam is by
    ``participant_turn_id``, which the manager owns.
    """

    type: str = "turn_started"


@dataclass(frozen=True)
class MessageDelta:
    """An incremental chunk of participant-visible assistant text."""

    text: str
    type: str = "message_delta"


@dataclass(frozen=True)
class TurnCompleted:
    """Terminal event for a turn. ``status`` is one of :data:`TURN_STATUSES`."""

    status: str
    text: str = ""
    error: Union[str, None] = None
    type: str = "turn_completed"

    def __post_init__(self) -> None:
        if self.status not in TURN_STATUSES:
            raise ValueError(
                f"invalid turn status {self.status!r}; expected one of {TURN_STATUSES}"
            )


AdapterEvent = Union[SessionUpdated, TurnStarted, MessageDelta, TurnCompleted]

__all__ = [
    "TURN_STATUSES",
    "AdapterEvent",
    "MessageDelta",
    "SessionUpdated",
    "TurnCompleted",
    "TurnStarted",
]
