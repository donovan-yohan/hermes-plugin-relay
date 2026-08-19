"""Participant / adapter configuration for hermes-plugin-relay.

Everything is read through ``ctx.get_config`` (``plugins.entries.
hermes-plugin-relay.settings.*`` in ``config.yaml``). No ``HERMES_*``
environment variables configure behavior.

Settings keys:

``participants``
    List of participant objects; replaces the defaults entirely when present.
    Each: ``{id, handle, display_name, adapter, model?, cwd?, enabled?, options?}``.
``chain``
    ``{enabled: false, turn_cap: 2}`` — participant-to-participant routing.
``tool_timeout_seconds``
    Bound on the blocking ``agent_message`` tool (default 300).
``cwd``
    Default working directory for every participant that does not set its own.

Loading is defensive by design: participant-seam contract section 11 requires
plugin registration to survive a broken environment, so malformed entries are
skipped with a warning rather than raised.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-plugin-relay"
PLUGIN_VERSION = "0.1.0"

#: ``@hermes`` addresses Hermes itself and can never name a participant.
HERMES_HANDLE = "hermes"

#: Handles are the ``@mention`` tokens; keep them lowercase and word-safe.
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

DEFAULT_TOOL_TIMEOUT_SECONDS = 300.0
DEFAULT_CHAIN_TURN_CAP = 2

DEFAULT_PARTICIPANTS: Tuple[Dict[str, Any], ...] = (
    {
        "id": "claude:default",
        "handle": "claude",
        "display_name": "Claude Code",
        "adapter": "claude",
    },
    {
        "id": "codex:default",
        "handle": "codex",
        "display_name": "Codex",
        "adapter": "codex",
    },
    {
        "id": "mock:default",
        "handle": "mock",
        "display_name": "Mock Participant",
        "adapter": "mock",
    },
)


class ConfigError(ValueError):
    """A participant entry could not be understood."""


@dataclass(frozen=True)
class ParticipantConfig:
    """One configured external participant."""

    id: str
    handle: str
    display_name: str
    adapter: str
    model: Optional[str] = None
    cwd: Optional[str] = None
    enabled: bool = True
    options: Mapping[str, Any] = field(default_factory=dict)

    @property
    def adapter_id(self) -> str:
        """Stable adapter identifier surfaced in transcript attribution."""

        from .adapters import ADAPTERS

        cls = ADAPTERS.get(self.adapter)
        return cls.id if cls is not None else self.adapter


@dataclass(frozen=True)
class ChainConfig:
    """Participant-to-participant routing brakes (contract section 10)."""

    enabled: bool = False
    turn_cap: int = DEFAULT_CHAIN_TURN_CAP


@dataclass(frozen=True)
class RelayConfig:
    participants: Tuple[ParticipantConfig, ...] = ()
    chain: ChainConfig = field(default_factory=ChainConfig)
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    default_cwd: Optional[str] = None
    plugin_id: str = PLUGIN_ID

    # -- lookups ----------------------------------------------------------------

    @property
    def enabled_participants(self) -> Tuple[ParticipantConfig, ...]:
        return tuple(p for p in self.participants if p.enabled)

    @property
    def handles(self) -> Tuple[str, ...]:
        return tuple(p.handle for p in self.enabled_participants)

    def by_id(self, participant_id: str) -> Optional[ParticipantConfig]:
        for participant in self.participants:
            if participant.id == participant_id:
                return participant
        return None

    def by_handle(self, handle: str) -> Optional[ParticipantConfig]:
        wanted = (handle or "").strip().lstrip("@").lower()
        for participant in self.participants:
            if participant.handle == wanted:
                return participant
        return None

    def resolve(self, reference: str) -> Optional[ParticipantConfig]:
        """Resolve by participant id first, then by ``@handle``."""

        if not reference:
            return None
        return self.by_id(reference) or self.by_handle(reference)


def parse_participant(raw: Any) -> ParticipantConfig:
    """Build a :class:`ParticipantConfig` from a config mapping."""

    if not isinstance(raw, Mapping):
        raise ConfigError(f"participant entry must be a mapping, got {type(raw).__name__}")

    handle = str(raw.get("handle") or "").strip().lstrip("@").lower()
    if not handle:
        raise ConfigError("participant entry is missing 'handle'")
    if not HANDLE_RE.match(handle):
        raise ConfigError(
            f"invalid participant handle {handle!r}; expected [a-z0-9][a-z0-9_-]*"
        )
    if handle == HERMES_HANDLE:
        raise ConfigError("'hermes' is reserved and cannot name a participant")

    adapter = str(raw.get("adapter") or "").strip()
    if not adapter:
        raise ConfigError(f"participant {handle!r} is missing 'adapter'")

    participant_id = str(raw.get("id") or "").strip() or f"{adapter}:{handle}"
    display_name = str(raw.get("display_name") or raw.get("displayName") or "").strip()
    if not display_name:
        display_name = handle.replace("-", " ").replace("_", " ").title()

    options = raw.get("options")
    if not isinstance(options, Mapping):
        options = {}

    model = raw.get("model")
    cwd = raw.get("cwd")
    return ParticipantConfig(
        id=participant_id,
        handle=handle,
        display_name=display_name,
        adapter=adapter,
        model=str(model) if isinstance(model, str) and model.strip() else None,
        cwd=str(cwd) if isinstance(cwd, str) and cwd.strip() else None,
        enabled=bool(raw.get("enabled", True)),
        options=dict(options),
    )


def parse_participants(raw: Any) -> Tuple[ParticipantConfig, ...]:
    """Parse a participant list, skipping (and logging) invalid entries."""

    if raw is None:
        entries: Sequence[Any] = DEFAULT_PARTICIPANTS
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        entries = raw
    else:
        logger.warning(
            "%s: 'participants' setting must be a list; using defaults", PLUGIN_ID
        )
        entries = DEFAULT_PARTICIPANTS

    parsed: List[ParticipantConfig] = []
    seen_handles: Dict[str, str] = {}
    seen_ids: set = set()
    for entry in entries:
        try:
            participant = parse_participant(entry)
        except ConfigError as exc:
            logger.warning("%s: skipping participant entry: %s", PLUGIN_ID, exc)
            continue
        if participant.handle in seen_handles:
            logger.warning(
                "%s: duplicate participant handle %r; keeping the first (%s)",
                PLUGIN_ID,
                participant.handle,
                seen_handles[participant.handle],
            )
            continue
        if participant.id in seen_ids:
            logger.warning(
                "%s: duplicate participant id %r; skipping", PLUGIN_ID, participant.id
            )
            continue
        seen_handles[participant.handle] = participant.id
        seen_ids.add(participant.id)
        parsed.append(participant)
    return tuple(parsed)


def parse_chain(raw: Any) -> ChainConfig:
    if not isinstance(raw, Mapping):
        return ChainConfig()
    enabled = bool(raw.get("enabled", False))
    try:
        turn_cap = int(raw.get("turn_cap", DEFAULT_CHAIN_TURN_CAP))
    except (TypeError, ValueError):
        logger.warning("%s: chain.turn_cap must be an integer; using default", PLUGIN_ID)
        turn_cap = DEFAULT_CHAIN_TURN_CAP
    return ChainConfig(enabled=enabled, turn_cap=max(0, turn_cap))


ConfigGetter = Callable[[str, Any], Any]


def load_config(get_config: Optional[ConfigGetter] = None) -> RelayConfig:
    """Build a :class:`RelayConfig` from ``ctx.get_config`` (or defaults)."""

    def _get(key: str, default: Any) -> Any:
        if get_config is None:
            return default
        try:
            return get_config(key, default)
        except Exception:  # noqa: BLE001 - a bad settings tree must not break load
            logger.warning("%s: failed reading setting %r", PLUGIN_ID, key, exc_info=True)
            return default

    participants = parse_participants(_get("participants", None))
    chain = parse_chain(_get("chain", None))

    raw_timeout = _get("tool_timeout_seconds", DEFAULT_TOOL_TIMEOUT_SECONDS)
    try:
        tool_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        logger.warning(
            "%s: tool_timeout_seconds must be a number; using %s",
            PLUGIN_ID,
            DEFAULT_TOOL_TIMEOUT_SECONDS,
        )
        tool_timeout = DEFAULT_TOOL_TIMEOUT_SECONDS
    if tool_timeout <= 0:
        tool_timeout = DEFAULT_TOOL_TIMEOUT_SECONDS

    raw_cwd = _get("cwd", None)
    default_cwd = str(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd.strip() else None

    return RelayConfig(
        participants=participants,
        chain=chain,
        tool_timeout_seconds=tool_timeout,
        default_cwd=default_cwd,
    )


def resolve_cwd(
    participant: ParticipantConfig,
    relay_config: RelayConfig,
    session_cwd: Optional[str] = None,
) -> str:
    """Working directory for a participant turn.

    Precedence: participant ``cwd`` -> plugin ``cwd`` setting -> the Hermes
    session's cwd -> the user's home directory.
    """

    for candidate in (participant.cwd, relay_config.default_cwd, session_cwd):
        if candidate:
            path = Path(str(candidate)).expanduser()
            if path.is_dir():
                return str(path)
            logger.warning(
                "%s: configured cwd %s does not exist; falling back", PLUGIN_ID, path
            )
    return str(Path.home())


__all__ = [
    "ChainConfig",
    "ConfigError",
    "DEFAULT_CHAIN_TURN_CAP",
    "DEFAULT_PARTICIPANTS",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "HANDLE_RE",
    "HERMES_HANDLE",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "ParticipantConfig",
    "RelayConfig",
    "load_config",
    "parse_chain",
    "parse_participant",
    "parse_participants",
    "resolve_cwd",
]
