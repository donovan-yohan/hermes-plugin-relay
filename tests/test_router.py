"""Chain-safety planning (contract section 10), tested as a pure function."""

from __future__ import annotations

import asyncio

import pytest

from hermes_plugin_relay.config import ChainConfig
from hermes_plugin_relay.runtime.router import (
    ChainRouter,
    forward_to_hermes,
    mentions_hermes,
    parse_mentions,
    plan_chain,
)

ROSTER = ["claude", "codex", "pi"]


# ---------------------------------------------------------------------------
# Mention parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("@claude take a look", ["claude"]),
        ("hey @codex and @claude", ["codex", "claude"]),
        ("@claude @claude @claude", ["claude"]),
        ("@CLAUDE shouting", ["claude"]),
        ("@claude, please", ["claude"]),
        ("(@claude)", ["claude"]),
        ("line\n@codex", ["codex"]),
        ("", []),
        ("no mentions here", []),
        ("@unknown-agent", []),
        ("user@example.com", []),
        ("email me at me@claude.com", []),
        ("foo@claude", []),
        ("@@claude", []),
        ("@claude-code", []),
        ("path/@claude", ["claude"]),
    ],
)
def test_parse_mentions(text, expected):
    assert parse_mentions(text, ROSTER) == expected


def test_parse_mentions_is_bounded():
    from hermes_plugin_relay.runtime.router import MAX_SCAN_CHARS

    text = ("x" * MAX_SCAN_CHARS) + " @claude"
    assert parse_mentions(text, ROSTER) == []


@pytest.mark.parametrize(
    "text,expected",
    [("@hermes look", True), ("hermes look", False), ("@hermesbot", False), ("", False)],
)
def test_mentions_hermes(text, expected):
    assert mentions_hermes(text) is expected


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_disabled_chain_never_targets_anyone():
    decision = plan_chain(
        "@codex your turn",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=False),
        chain_depth=0,
    )
    assert decision.enabled is False
    assert decision.targets == ()
    assert decision.refused == ()


def test_enabled_chain_targets_other_participants():
    decision = plan_chain(
        "@codex and @pi please continue",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=True, turn_cap=3),
        chain_depth=0,
    )
    assert decision.targets == ("codex", "pi")
    assert decision.next_depth == 1
    assert decision.refused == ()


def test_self_mentions_are_ignored():
    decision = plan_chain(
        "@claude keep going, @codex too",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=True, turn_cap=5),
        chain_depth=0,
    )
    assert decision.targets == ("codex",)


def test_only_self_mention_produces_no_dispatch_and_no_refusal():
    decision = plan_chain(
        "@claude again",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=True, turn_cap=0),
        chain_depth=0,
    )
    assert decision.targets == ()
    assert decision.refused == ()


@pytest.mark.parametrize(
    "depth,cap,expect_refused",
    [(0, 2, False), (1, 2, False), (2, 2, True), (5, 2, True), (0, 0, True)],
)
def test_turn_cap_boundary(depth, cap, expect_refused):
    decision = plan_chain(
        "@codex go",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=True, turn_cap=cap),
        chain_depth=depth,
    )
    if expect_refused:
        assert decision.targets == ()
        assert [handle for handle, _ in decision.refused] == ["codex"]
        assert "turn cap" in decision.refused[0][1]
    else:
        assert decision.targets == ("codex",)
        assert decision.refused == ()


def test_hermes_mention_is_flagged_but_never_a_target():
    decision = plan_chain(
        "@hermes and @codex",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=True, turn_cap=5),
        chain_depth=0,
    )
    assert decision.hermes_addressed is True
    assert decision.targets == ("codex",)


def test_hermes_flag_survives_a_disabled_chain():
    decision = plan_chain(
        "@hermes look at this",
        source_handle="claude",
        roster_handles=ROSTER,
        chain=ChainConfig(enabled=False),
        chain_depth=0,
    )
    assert decision.hermes_addressed is True


def test_forward_to_hermes_is_an_unwired_seam():
    """Slice 1 must not wake Hermes with untrusted peer text."""

    assert forward_to_hermes("session", "claude", "@hermes hi") is False


# ---------------------------------------------------------------------------
# Router driving
# ---------------------------------------------------------------------------


def run_router(text: str, *, status: str = "completed", chain: ChainConfig = None, depth: int = 0):
    dispatched = []

    async def dispatch(**kwargs):
        dispatched.append(kwargs)

    router = ChainRouter(
        chain=chain or ChainConfig(enabled=True, turn_cap=2),
        roster_handles=lambda: ROSTER,
        dispatch=dispatch,
    )
    decision = asyncio.run(
        router.route_completed_turn(
            session_id="s1",
            source_handle="claude",
            source_participant_id="claude:default",
            text=text,
            status=status,
            chain_depth=depth,
        )
    )
    return router, decision, dispatched


def test_router_dispatches_with_incremented_depth():
    router, decision, dispatched = run_router("@codex go")
    assert len(dispatched) == 1
    assert dispatched[0]["reference"] == "codex"
    assert dispatched[0]["chain_depth"] == 1
    assert dispatched[0]["source"] == "chain"
    assert dispatched[0]["append_user_message"] is False
    assert list(router.refusals) == []


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_router_ignores_non_completed_turns(status):
    _, _, dispatched = run_router("@codex go", status=status)
    assert dispatched == []


def test_router_records_refusals_above_the_cap():
    router, decision, dispatched = run_router("@codex go", depth=2)
    assert dispatched == []
    assert len(router.refusals) == 1
    assert router.refusals[0]["target_handle"] == "codex"
    assert router.refusals[0]["source_participant_id"] == "claude:default"


def test_refusals_are_bounded():
    """Diagnostics, not a ledger: a long-lived gateway must not accumulate them."""

    from hermes_plugin_relay.runtime.router import MAX_REFUSALS

    async def dispatch(**kwargs):  # pragma: no cover - never reached above cap
        raise AssertionError("should not dispatch above the cap")

    router = ChainRouter(
        chain=ChainConfig(enabled=True, turn_cap=0),
        roster_handles=lambda: ROSTER,
        dispatch=dispatch,
    )

    async def drive():
        for index in range(MAX_REFUSALS + 25):
            await router.route_completed_turn(
                session_id=f"s{index}",
                source_handle="claude",
                source_participant_id="claude:default",
                text="@codex go",
                status="completed",
                chain_depth=0,
            )

    asyncio.run(drive())
    assert len(router.refusals) == MAX_REFUSALS
    # The newest are the ones kept.
    assert router.refusals[-1]["session_id"] == f"s{MAX_REFUSALS + 24}"


def test_router_survives_a_failing_dispatch():
    async def dispatch(**kwargs):
        raise RuntimeError("downstream is down")

    router = ChainRouter(
        chain=ChainConfig(enabled=True, turn_cap=3),
        roster_handles=lambda: ROSTER,
        dispatch=dispatch,
    )
    decision = asyncio.run(
        router.route_completed_turn(
            session_id="s1",
            source_handle="claude",
            source_participant_id="claude:default",
            text="@codex go",
            status="completed",
            chain_depth=0,
        )
    )
    assert decision.targets == ("codex",)
