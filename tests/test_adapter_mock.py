"""In-process mock adapter behavior."""

from __future__ import annotations

import asyncio

import pytest

from conftest import collect, drain, wait_until

from hermes_plugin_relay.adapters.base import AdapterError, TurnInput
from hermes_plugin_relay.adapters.mock import MockAdapter
from hermes_plugin_relay.runtime.events import (
    MessageDelta,
    SessionUpdated,
    TurnCompleted,
)


def turn(text: str = "hello", turn_id: str = "pturn-1") -> TurnInput:
    return TurnInput(text=text, cwd=".", participant_turn_id=turn_id)


def run(adapter: MockAdapter, *turns: TurnInput):
    events = collect(adapter)

    async def scenario():
        for item in turns:
            await adapter.start_turn(item)
            await wait_until(
                lambda: len([e for e in events if isinstance(e, TurnCompleted)])
                == turns.index(item) + 1
            )
        await adapter.close()

    asyncio.run(scenario())
    return events


def test_capabilities_are_all_true():
    capabilities = MockAdapter.capabilities.to_dict()
    assert all(capabilities.values())
    assert MockAdapter.availability().status == "ready"


def test_streams_the_reply_in_chunks_then_completes():
    adapter = MockAdapter(participant_id="mock:default", chunk_size=4)
    events = run(adapter, turn("hi"))

    assert [e.type for e in events][:2] == ["session_updated", "turn_started"]
    assert isinstance(events[0], SessionUpdated)
    assert events[0].provider_session_id == "mock-mock:default"
    deltas = [e.text for e in events if isinstance(e, MessageDelta)]
    assert "".join(deltas) == "mock reply: hi"
    assert all(len(chunk) <= 4 for chunk in deltas)
    assert len(deltas) > 1
    assert events[-1] == TurnCompleted(status="completed", text="mock reply: hi", error=None)
    assert adapter.prompts == ["hi"]


def test_reply_template_and_handle_substitution():
    adapter = MockAdapter(
        participant_id="p", handle="codex", reply_template="{handle} heard: {text}"
    )
    events = run(adapter, turn("ping"))
    assert events[-1].text == "codex heard: ping"


def test_broken_template_does_not_crash():
    adapter = MockAdapter(participant_id="p", reply_template="{nope}")
    events = run(adapter, turn("ping"))
    assert events[-1].text == "{nope}"


def test_reply_callable_wins():
    adapter = MockAdapter(participant_id="p", reply=lambda text: text.upper())
    events = run(adapter, turn("shout"))
    assert events[-1].text == "SHOUT"


def test_fail_error_completes_as_failed():
    adapter = MockAdapter(participant_id="p", fail_error="mock exploded")
    events = run(adapter, turn("x"))
    assert events[-1].status == "failed"
    assert events[-1].error == "mock exploded"
    assert events[-1].text == "mock reply: x"


def test_hang_then_interrupt():
    adapter = MockAdapter(participant_id="p", hang=True)
    events = collect(adapter)

    async def scenario():
        await adapter.start_turn(turn("long"))
        await wait_until(lambda: any(isinstance(e, MessageDelta) for e in events))
        await adapter.interrupt()
        await adapter.close()

    asyncio.run(scenario())
    completed = [e for e in events if isinstance(e, TurnCompleted)]
    assert len(completed) == 1
    assert completed[0].status == "interrupted"
    assert completed[0].text == "mock reply: long"


def test_interrupt_without_an_active_turn_is_a_noop():
    adapter = MockAdapter(participant_id="p")
    events = collect(adapter)
    asyncio.run(adapter.interrupt())
    assert events == []


def test_session_id_is_emitted_once_across_turns():
    adapter = MockAdapter(participant_id="p")
    events = run(adapter, turn("a", "pturn-1"), turn("b", "pturn-2"))
    assert len([e for e in events if isinstance(e, SessionUpdated)]) == 1
    assert [e.text for e in events if isinstance(e, TurnCompleted)] == [
        "mock reply: a",
        "mock reply: b",
    ]
    assert adapter.prompts == ["a", "b"]


def test_resume_session_id_is_honored():
    adapter = MockAdapter(participant_id="p")
    events = collect(adapter)

    async def scenario():
        await adapter.start_turn(
            TurnInput(
                text="x", cwd=".", participant_turn_id="pturn-1", resume_session_id="mock-resumed"
            )
        )
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()

    asyncio.run(scenario())
    assert events[0] == SessionUpdated("mock-resumed")


def test_concurrent_turn_is_rejected():
    adapter = MockAdapter(participant_id="p", hang=True)

    async def scenario():
        await adapter.start_turn(turn("a", "pturn-1"))
        with pytest.raises(AdapterError):
            await adapter.start_turn(turn("b", "pturn-2"))
        await adapter.close()

    asyncio.run(scenario())


def test_close_finalizes_an_in_flight_turn():
    adapter = MockAdapter(participant_id="p", hang=True)
    events = collect(adapter)

    async def scenario():
        await adapter.start_turn(turn("x"))
        await drain()
        await adapter.close()

    asyncio.run(scenario())
    assert events[-1].status == "interrupted"
