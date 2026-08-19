"""Chain-safety foundation (participant seam contract v1 section 10).

External participant output is untrusted peer content. When a participant's
reply mentions another participant, routing it onward is a real capability and a
real footgun, so it is:

* off by default (``chain.enabled``);
* bounded by ``chain.turn_cap``;
* never self-recursive (a participant mentioning itself is ignored);
* never silently dropped — a refusal is logged and recorded.

``@hermes`` forwarding is deliberately NOT wired in slice 1. The seam is
:func:`forward_to_hermes` and it refuses until core lands a queued
untrusted-peer user-message path.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Deque, Dict, Iterable, List, Sequence, Tuple

from ..config import HERMES_HANDLE, ChainConfig

logger = logging.getLogger(__name__)

#: ``@handle`` tokens. The lookbehind keeps ``user@example.com`` and ``@@x``
#: from producing mentions.
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@.-])@([A-Za-z0-9][A-Za-z0-9_-]*)")

#: Do not scan unbounded participant output for mentions.
MAX_SCAN_CHARS = 32_000

#: Refusals are diagnostics, not a ledger. Keep a recent window so a long-lived
#: gateway with chaining on cannot accumulate them without bound.
MAX_REFUSALS = 100


def parse_mentions(text: str, known_handles: Iterable[str]) -> List[str]:
    """Ordered, de-duplicated roster handles mentioned in ``text``."""

    if not text:
        return []
    known = {handle.lower() for handle in known_handles if handle}
    found: List[str] = []
    for match in MENTION_RE.finditer(text[:MAX_SCAN_CHARS]):
        handle = match.group(1).lower()
        if handle in known and handle not in found:
            found.append(handle)
    return found


def mentions_hermes(text: str) -> bool:
    """True when ``@hermes`` appears as a standalone mention."""

    return HERMES_HANDLE in parse_mentions(text, (HERMES_HANDLE,))


@dataclass(frozen=True)
class ChainDecision:
    """What the router would do with one completed participant turn."""

    enabled: bool = False
    next_depth: int = 0
    targets: Tuple[str, ...] = ()
    refused: Tuple[Tuple[str, str], ...] = ()
    hermes_addressed: bool = False


def plan_chain(
    text: str,
    *,
    source_handle: str,
    roster_handles: Sequence[str],
    chain: ChainConfig,
    chain_depth: int,
) -> ChainDecision:
    """Pure planning step: decide onward dispatches for a completed turn."""

    # One scan for everything: roster handles and @hermes together, then split.
    found = parse_mentions(text, tuple(roster_handles) + (HERMES_HANDLE,))
    hermes_addressed = HERMES_HANDLE in found
    if not chain.enabled:
        return ChainDecision(
            enabled=False, next_depth=chain_depth + 1, hermes_addressed=hermes_addressed
        )

    source = (source_handle or "").lower()
    candidates = [h for h in found if h != HERMES_HANDLE and h != source]

    next_depth = chain_depth + 1
    if candidates and next_depth > chain.turn_cap:
        reason = (
            f"chain turn cap reached (cap={chain.turn_cap}, would be depth {next_depth}); "
            "a human must resume the chain"
        )
        return ChainDecision(
            enabled=True,
            next_depth=next_depth,
            refused=tuple((handle, reason) for handle in candidates),
            hermes_addressed=hermes_addressed,
        )

    return ChainDecision(
        enabled=True,
        next_depth=next_depth,
        targets=tuple(candidates),
        hermes_addressed=hermes_addressed,
    )


def forward_to_hermes(session_id: str, source_handle: str, text: str) -> bool:
    """SEAM (slice 2): queue an untrusted-peer user message for Hermes.

    Deliberately unimplemented. Contract section 10 requires ``@hermes`` routing
    to land as a queued untrusted-peer user message via explicit config, which
    is not part of slice-1 defaults. Wiring this without the core queue would
    let untrusted participant text wake Hermes with no trust envelope.
    """

    logger.info(
        "relay: @hermes mention from @%s in session %s is not forwarded in slice 1",
        source_handle,
        session_id,
    )
    return False


DispatchFn = Callable[..., Awaitable[Any]]


@dataclass
class ChainRouter:
    """Applies :func:`plan_chain` and drives onward dispatches."""

    chain: ChainConfig
    roster_handles: Callable[[], Sequence[str]]
    dispatch: DispatchFn
    refusals: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_REFUSALS)
    )

    async def route_completed_turn(
        self,
        *,
        session_id: str,
        source_handle: str,
        source_participant_id: str,
        text: str,
        status: str,
        chain_depth: int,
    ) -> ChainDecision:
        if status != "completed":
            return ChainDecision(enabled=self.chain.enabled, next_depth=chain_depth + 1)

        decision = plan_chain(
            text,
            source_handle=source_handle,
            roster_handles=self.roster_handles(),
            chain=self.chain,
            chain_depth=chain_depth,
        )

        if decision.hermes_addressed:
            forward_to_hermes(session_id, source_handle, text)

        for handle, reason in decision.refused:
            record = {
                "session_id": session_id,
                "source_participant_id": source_participant_id,
                "target_handle": handle,
                "reason": reason,
                "chain_depth": chain_depth,
            }
            self.refusals.append(record)
            logger.warning(
                "relay: refusing chained dispatch @%s -> @%s in session %s: %s",
                source_handle,
                handle,
                session_id,
                reason,
            )

        for handle in decision.targets:
            try:
                await self.dispatch(
                    session_id=session_id,
                    reference=handle,
                    text=text,
                    source="chain",
                    append_user_message=False,
                    chain_depth=decision.next_depth,
                )
            except Exception:  # noqa: BLE001 - a chain hop must not kill the parent turn
                logger.exception(
                    "relay: chained dispatch to @%s failed in session %s", handle, session_id
                )

        return decision


__all__ = [
    "ChainDecision",
    "ChainRouter",
    "MAX_REFUSALS",
    "MAX_SCAN_CHARS",
    "MENTION_RE",
    "forward_to_hermes",
    "mentions_hermes",
    "parse_mentions",
    "plan_chain",
]
