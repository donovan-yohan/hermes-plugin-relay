"""Codex adapter contract tests against a scripted app-server JSON-RPC fake."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

import pytest

from conftest import FakeProcess, collect, drain, process_factory, wait_until

from hermes_plugin_relay.adapters.codex import CodexAdapter
from hermes_plugin_relay.adapters.base import TurnInput
from hermes_plugin_relay.runtime.events import (
    MessageDelta,
    SessionUpdated,
    TurnCompleted,
    TurnStarted,
)

class ScriptedAppServer:
    """Minimal ``codex app-server`` stand-in driven by JSON-RPC over the fake pipe."""

    def __init__(self, echo_thread: bool = True) -> None:
        self.thread_id = "thread-1"
        self.echo_thread = echo_thread
        self.requests: List[dict] = []
        self.notifications: List[dict] = []
        self.client_responses: List[dict] = []
        self.overrides: Dict[str, Any] = {}
        self.proc = FakeProcess(on_stdin_line=self._on_line)

    # -- wiring ----------------------------------------------------------------

    def _on_line(self, proc: FakeProcess, line: str) -> None:
        message = json.loads(line)
        if "method" in message and message.get("id") is not None:
            self.requests.append(message)
            result = self._result_for(message)
            if isinstance(result, Exception):
                proc.feed(
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32000, "message": str(result)},
                    }
                )
                return
            proc.feed({"jsonrpc": "2.0", "id": message["id"], "result": result})
        elif "method" in message:
            self.notifications.append(message)
        else:
            self.client_responses.append(message)

    def _result_for(self, message: dict) -> Any:
        method = message["method"]
        override = self.overrides.get(method)
        if override is not None:
            return override(message) if callable(override) else override
        if method == "initialize":
            return {"serverInfo": {"name": "codex", "version": "0.0.0-test"}}
        if method == "thread/start":
            return {"thread": {"id": self.thread_id}}
        if method == "thread/resume":
            return {"thread": {"id": self.thread_id}} if self.echo_thread else {}
        if method == "turn/start":
            return {"turnId": "native-turn-1"}
        if method == "turn/interrupt":
            return {}
        return {}

    # -- helpers ---------------------------------------------------------------

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.feed(payload)

    def request_methods(self) -> List[str]:
        return [request["method"] for request in self.requests]

    def request(self, method: str) -> dict:
        for entry in self.requests:
            if entry["method"] == method:
                return entry
        raise AssertionError(f"{method} was never requested; saw {self.request_methods()}")


def make_adapter(server: ScriptedAppServer, **kwargs) -> CodexAdapter:
    return CodexAdapter(
        participant_id="codex:default",
        process_factory=process_factory(server.proc),
        **kwargs,
    )


async def start(adapter: CodexAdapter, tmp_path, turn_id: str = "pturn-1", **kwargs) -> None:
    await adapter.start_turn(
        TurnInput(text="hi", cwd=str(tmp_path), participant_turn_id=turn_id, **kwargs)
    )


def types_of(events) -> list:
    return [event.type for event in events]


# ---------------------------------------------------------------------------


def test_argv_forces_stdio_transport():
    adapter = CodexAdapter(participant_id="codex:default")
    assert adapter.build_argv() == ["codex", "app-server", "--listen", "stdio://"]


def test_handshake_thread_and_streaming_turn(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("turn/started", {"turn": {"id": "native-turn-1"}})
        server.notify(
            "item/agentMessage/delta", {"itemId": "item-1", "delta": "hello "}
        )
        server.notify("item/agentMessage/delta", {"itemId": "item-1", "delta": "world"})
        server.notify(
            "item/completed",
            {"item": {"id": "item-1", "type": "agentMessage", "text": "hello world"}},
        )
        server.notify("turn/completed", {"turn": {"id": "native-turn-1", "status": "completed"}})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events, server

    events, server = asyncio.run(scenario())

    assert server.request_methods()[:3] == ["initialize", "thread/start", "turn/start"]
    assert server.request("initialize")["params"]["clientInfo"]["name"] == "hermes-plugin-relay"
    # `initialized` is a notification with no id and no params.
    assert server.notifications[0]["method"] == "initialized"
    assert "id" not in server.notifications[0]
    assert "params" not in server.notifications[0]
    assert server.request("thread/start")["params"] == {"cwd": str(tmp_path)}
    assert server.request("turn/start")["params"] == {
        "threadId": "thread-1",
        "input": [{"type": "text", "text": "hi"}],
    }

    assert types_of(events) == [
        "session_updated",
        "turn_started",
        "message_delta",
        "message_delta",
        "turn_completed",
    ]
    assert events[0] == SessionUpdated("thread-1")
    assert isinstance(events[1], TurnStarted)
    assert [e.text for e in events if isinstance(e, MessageDelta)] == ["hello ", "world"]
    assert events[-1].status == "completed"
    assert events[-1].text == "hello world"


def test_turn_completed_before_item_completed_uses_authoritative_text(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("item/agentMessage/delta", {"itemId": "item-1", "delta": "partial"})
        await drain()
        # Terminal notification races ahead of the final item.
        server.notify("turn/completed", {"turn": {"id": "t", "status": "completed"}})
        await drain()
        assert not [e for e in events if isinstance(e, TurnCompleted)]
        server.notify(
            "item/completed",
            {"item": {"id": "item-1", "type": "agentMessage", "text": "partial but final"}},
        )
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "completed"
    assert events[-1].text == "partial but final"


def test_grace_applies_when_final_text_never_seen_before_completion(tmp_path):
    """Contract v1.4: item/completed may be the FIRST sight of the text."""

    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("turn/started", {"turn": {"id": "native-turn-1"}})
        # No delta and no item/started: nothing marks an open agentMessage item.
        server.notify("turn/completed", {"turn": {"id": "native-turn-1", "status": "completed"}})
        await drain()
        assert not [e for e in events if isinstance(e, TurnCompleted)]
        server.notify(
            "item/completed",
            {"item": {"id": "item-9", "type": "agentMessage", "text": "hello"}},
        )
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    completed = events[-1]
    assert completed.status == "completed"
    assert completed.text == "hello"


def test_multiple_agent_messages_in_one_turn_are_all_kept(tmp_path):
    """A preamble message plus a final message must both survive completion."""

    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("turn/started", {"turn": {"id": "native-turn-1"}})

        # Preamble message: streams, then closes.
        server.notify("item/agentMessage/delta", {"itemId": "item-1", "delta": "let me check"})
        server.notify(
            "item/completed",
            {"item": {"id": "item-1", "type": "agentMessage", "text": "let me check"}},
        )
        # A tool runs in between (not surfaced in slice 1).
        server.notify("item/completed", {"item": {"id": "cmd-1", "type": "commandExecution"}})
        # Final message.
        server.notify("item/agentMessage/delta", {"itemId": "item-2", "delta": "the answer is 42"})
        server.notify(
            "item/completed",
            {"item": {"id": "item-2", "type": "agentMessage", "text": "the answer is 42"}},
        )
        server.notify("turn/completed", {"turn": {"id": "native-turn-1", "status": "completed"}})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    completed = events[-1]
    assert completed.status == "completed"
    assert "let me check" in completed.text
    assert "the answer is 42" in completed.text
    assert completed.text == "let me check\n\nthe answer is 42"
    # Nothing that streamed to the user is missing from the final text.
    for delta in (e.text for e in events if isinstance(e, MessageDelta)):
        assert delta in completed.text


def test_late_agent_message_is_appended_not_substituted(tmp_path):
    """turn/completed racing ahead must not drop the already-closed preamble."""

    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("item/agentMessage/delta", {"itemId": "item-1", "delta": "first"})
        server.notify(
            "item/completed",
            {"item": {"id": "item-1", "type": "agentMessage", "text": "first"}},
        )
        server.notify("turn/completed", {"turn": {"id": "t", "status": "completed"}})
        await drain()
        assert not [e for e in events if isinstance(e, TurnCompleted)]
        server.notify(
            "item/completed",
            {"item": {"id": "item-2", "type": "agentMessage", "text": "second"}},
        )
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].text == "first\n\nsecond"


def test_grace_expires_and_falls_back_to_delta_buffer(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_plugin_relay.adapters.codex.TERMINAL_ITEM_GRACE_SECONDS", 0.02
    )

    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("item/agentMessage/delta", {"itemId": "item-1", "delta": "only deltas"})
        server.notify("turn/completed", {"turn": {"id": "t", "status": "completed"}})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "completed"
    assert events[-1].text == "only deltas"


@pytest.mark.parametrize(
    "turn_payload,expected_status,expected_error",
    [
        ({"id": "t", "status": "failed", "error": "sandbox denied"}, "failed", "sandbox denied"),
        ({"id": "t", "status": "failed"}, "failed", "codex turn failed"),
        ({"id": "t", "status": "interrupted"}, "interrupted", None),
    ],
)
def test_turn_status_mapping(tmp_path, turn_payload, expected_status, expected_error):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify(
            "item/completed",
            {"item": {"id": "item-1", "type": "agentMessage", "text": ""}},
        )
        server.notify("turn/completed", {"turn": turn_payload})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == expected_status
    assert events[-1].error == expected_error


def test_interrupt_sends_turn_interrupt_and_waits_for_completion(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("turn/started", {"turn": {"id": "native-turn-1"}})
        server.notify("item/agentMessage/delta", {"itemId": "item-1", "delta": "half"})
        await drain()

        def on_interrupt(message: dict) -> dict:
            server.notify("turn/completed", {"turn": {"id": "native-turn-1", "status": "interrupted"}})
            return {}

        server.overrides["turn/interrupt"] = on_interrupt
        await adapter.interrupt()
        await adapter.close()
        return events, server

    events, server = asyncio.run(scenario())
    assert server.request("turn/interrupt")["params"] == {
        "threadId": "thread-1",
        "turnId": "native-turn-1",
    }
    assert events[-1].status == "interrupted"
    assert events[-1].text == "half"


def test_interrupt_swallows_rpc_error(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("turn/started", {"turn": {"id": "native-turn-1"}})
        await drain()

        def on_interrupt(message: dict):
            server.notify("turn/completed", {"turn": {"id": "native-turn-1", "status": "interrupted"}})
            return RuntimeError("no such turn")

        server.overrides["turn/interrupt"] = on_interrupt
        await adapter.interrupt()
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "interrupted"


def test_interrupt_without_completion_forces_finalization(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.adapters.codex.INTERRUPT_WAIT_SECONDS", 0.05)

    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.notify("turn/started", {"turn": {"id": "native-turn-1"}})
        await drain()
        await adapter.interrupt()
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "interrupted"


def test_resume_uses_thread_resume_and_retains_requested_id(tmp_path):
    async def scenario():
        server = ScriptedAppServer(echo_thread=False)
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path, resume_session_id="thread-from-store")
        server.notify(
            "item/completed",
            {"item": {"id": "i", "type": "agentMessage", "text": "resumed"}},
        )
        server.notify("turn/completed", {"turn": {"id": "t", "status": "completed"}})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events, server

    events, server = asyncio.run(scenario())
    assert "thread/start" not in server.request_methods()
    assert server.request("thread/resume")["params"] == {"threadId": "thread-from-store"}
    assert events[0] == SessionUpdated("thread-from-store")
    assert server.request("turn/start")["params"]["threadId"] == "thread-from-store"


def test_thread_started_notification_supplies_id(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        server.overrides["thread/start"] = {}  # no id in the response
        adapter = make_adapter(server)
        events = collect(adapter)
        with pytest.raises(Exception):
            await start(adapter, tmp_path)
        # A late thread/started notification still updates the id.
        server.notify("thread/started", {"thread": {"id": "thread-late"}})
        await drain()
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert SessionUpdated("thread-late") in events


def test_server_initiated_request_is_declined(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        await start(adapter, tmp_path)
        server.proc.feed(
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "rm -rf /"},
            }
        )
        await drain()
        await adapter.close()
        return server

    server = asyncio.run(scenario())
    declines = [m for m in server.client_responses if m.get("id") == 9001]
    assert len(declines) == 1
    assert declines[0]["error"]["code"] == -32601
    assert declines[0]["error"]["message"] == "Method not found"


def test_malformed_and_unknown_messages_are_ignored(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.proc.feed("{ this is not json")
        server.proc.feed('"a bare string"')
        server.notify("thread/tokenUsageUpdated", {"threadId": "thread-1", "total": 10})
        server.notify("item/reasoning/textDelta", {"itemId": "r1", "delta": "thinking"})
        server.proc.feed({"jsonrpc": "2.0", "id": 999, "result": {"unmatched": True}})
        server.notify(
            "item/completed",
            {"item": {"id": "i", "type": "agentMessage", "text": "survived"}},
        )
        server.notify("turn/completed", {"turn": {"id": "t", "status": "completed"}})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert not [e for e in events if isinstance(e, MessageDelta)]
    assert events[-1].status == "completed"
    assert events[-1].text == "survived"


def test_turn_start_rpc_error_fails_the_turn(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        server.overrides["turn/start"] = RuntimeError("thread is busy")
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "failed"
    assert "thread is busy" in events[-1].error


def test_server_exit_mid_turn_fails_with_stderr_tail(tmp_path):
    async def scenario():
        server = ScriptedAppServer()
        adapter = make_adapter(server)
        events = collect(adapter)
        await start(adapter, tmp_path)
        server.proc.feed_stderr("panic: app-server crashed")
        await drain()
        server.proc.feed_eof()
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "failed"
    assert "app-server crashed" in events[-1].error


def test_availability_offline_without_binary(monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.adapters.base.probe_binary", lambda b: None)
    availability = CodexAdapter.availability()
    assert availability.status == "offline"
    assert "codex CLI not found" in availability.reason


def test_availability_error_without_auth(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "hermes_plugin_relay.adapters.base.probe_binary", lambda b: "/usr/bin/codex"
    )
    availability = CodexAdapter.availability(home=tmp_path)
    assert availability.status == "error"
    assert "auth.json" in availability.reason

    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    assert CodexAdapter.availability(home=tmp_path).status == "ready"
