"""Claude Code adapter contract tests against scripted stream-json fixtures."""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import FakeProcess, collect, drain, process_factory, wait_until

from hermes_plugin_relay.adapters.base import AdapterNotAvailableError, TurnInput
from hermes_plugin_relay.adapters.claude import ClaudeAdapter
from hermes_plugin_relay.runtime.events import (
    MessageDelta,
    SessionUpdated,
    TurnCompleted,
    TurnStarted,
)


def make_adapter(proc: FakeProcess, **kwargs) -> ClaudeAdapter:
    return ClaudeAdapter(
        participant_id="claude:default",
        process_factory=process_factory(proc),
        **kwargs,
    )


def text_delta(text: str, index: int = 0) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def types_of(events) -> list:
    return [event.type for event in events]


# ---------------------------------------------------------------------------


def test_argv_shape_and_model(tmp_path):
    adapter = ClaudeAdapter(participant_id="p", model="opus-4")
    argv = adapter.build_argv(None)
    assert argv[:2] == ["claude", "-p"]
    for flag in (
        "--input-format",
        "--output-format",
        "--verbose",
        "--include-partial-messages",
        "--permission-mode",
    ):
        assert flag in argv
    assert argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert argv[argv.index("--model") + 1] == "opus-4"
    assert "--resume" not in argv


def test_argv_includes_resume_when_resuming(tmp_path):
    adapter = ClaudeAdapter(participant_id="p")
    argv = adapter.build_argv("sess-abc")
    assert argv[argv.index("--resume") + 1] == "sess-abc"


def test_streaming_turn_emits_normalized_sequence(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)

        await adapter.start_turn(
            TurnInput(text="hello", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed({"type": "system", "subtype": "init", "session_id": "sess-1"})
        proc.feed(text_delta("Hello"))
        proc.feed(text_delta(", "))
        proc.feed({"type": "assistant", "message": {"content": "ignored echo"}})
        proc.feed(text_delta("world"))
        proc.feed(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "sess-1",
                "result": "Hello, world",
            }
        )
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events, proc

    events, proc = asyncio.run(scenario())

    assert types_of(events) == [
        "turn_started",
        "session_updated",
        "message_delta",
        "message_delta",
        "message_delta",
        "turn_completed",
    ]
    assert isinstance(events[0], TurnStarted)
    assert events[1] == SessionUpdated("sess-1")
    assert [e.text for e in events if isinstance(e, MessageDelta)] == ["Hello", ", ", "world"]
    completed = events[-1]
    assert isinstance(completed, TurnCompleted)
    assert completed.status == "completed"
    assert completed.text == "Hello, world"
    assert completed.error is None

    # The user frame is the documented NDJSON shape.
    assert proc.stdin.json_lines[0] == {
        "type": "user",
        "message": {"role": "user", "content": "hello"},
    }
    assert proc.cwd == str(tmp_path)


def test_result_without_deltas_falls_back_to_result_field(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="hi", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed({"type": "result", "subtype": "success", "is_error": False, "result": "pong"})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert events[-1].status == "completed"
    assert events[-1].text == "pong"


@pytest.mark.parametrize(
    "frame,expected_error",
    [
        (
            {"type": "result", "subtype": "error_during_execution", "is_error": True,
             "errors": ["rate limited", "retry later"]},
            "rate limited; retry later",
        ),
        (
            {"type": "result", "subtype": "error", "is_error": True, "error": "boom"},
            "boom",
        ),
        (
            {"type": "result", "subtype": "error_max_turns", "is_error": False},
            "claude turn failed (error_max_turns)",
        ),
        (
            {"type": "result", "subtype": "success", "is_error": True},
            "claude turn failed (success)",
        ),
    ],
)
def test_failure_result_maps_to_failed(tmp_path, frame, expected_error):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="hi", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed(frame)
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    completed = events[-1]
    assert completed.status == "failed"
    assert completed.error == expected_error


def test_interrupt_writes_control_request_and_completes_interrupted(tmp_path):
    async def scenario():
        acked = {}

        def respond(process: FakeProcess, line: str) -> None:
            frame = json.loads(line)
            if frame.get("type") == "control_request":
                acked["request_id"] = frame["request_id"]
                # Ack shape: nested response.request_id.
                process.feed(
                    {
                        "type": "control_response",
                        "response": {"request_id": frame["request_id"], "subtype": "success"},
                    }
                )

        proc = FakeProcess(on_stdin_line=respond)
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="long task", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed(text_delta("partial"))
        await drain()
        await adapter.interrupt()
        await drain()
        # Captured before close(), which terminates the child unconditionally.
        warm = (proc.terminated, proc.killed)
        await adapter.close()
        return events, acked, warm

    events, acked, warm = asyncio.run(scenario())
    assert acked["request_id"].startswith("relay-int-")
    completed = events[-1]
    assert completed.status == "interrupted"
    assert completed.text == "partial"
    assert warm == (False, False)  # an acked interrupt keeps the child warm


def test_interrupt_ack_timeout_kills_child(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_plugin_relay.adapters.claude.INTERRUPT_ACK_SECONDS", 0.05
    )

    async def scenario():
        proc = FakeProcess()  # never acks
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="stuck", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        await adapter.interrupt()
        await drain()
        await adapter.close()
        return events, proc

    events, proc = asyncio.run(scenario())
    assert events[-1].status == "interrupted"
    assert proc.terminated is True


def test_late_result_after_interrupt_is_ignored(tmp_path):
    async def scenario():
        def respond(process: FakeProcess, line: str) -> None:
            frame = json.loads(line)
            if frame.get("type") == "control_request":
                process.feed(
                    {"type": "control_response", "response": {"request_id": frame["request_id"]}}
                )

        proc = FakeProcess(on_stdin_line=respond)
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="x", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        await adapter.interrupt()
        proc.feed({"type": "result", "subtype": "success", "is_error": False})
        proc.feed(text_delta("stale tail"))
        await drain()
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert [e.type for e in events].count("turn_completed") == 1
    assert events[-1].status == "interrupted"


def test_malformed_and_oversized_lines_are_skipped(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="x", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed("{not json at all")
        proc.feed("[1, 2, 3]")  # valid JSON, wrong shape
        proc.feed({"type": "stream_event", "event": {"type": "content_block_delta",
                                                     "delta": {"type": "thinking_delta",
                                                               "thinking": "hidden"}}})
        proc.feed({"type": "unknown_frame_type"})
        proc.feed(text_delta("ok"))
        proc.feed({"type": "result", "subtype": "success", "is_error": False})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert [e.text for e in events if isinstance(e, MessageDelta)] == ["ok"]
    assert events[-1].status == "completed"
    assert events[-1].text == "ok"


def test_oversize_line_is_dropped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.adapters.base.MAX_LINE_BYTES", 200)

    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="x", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed(text_delta("y" * 500))
        proc.feed(text_delta("small"))
        proc.feed({"type": "result", "subtype": "success", "is_error": False})
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    assert [e.text for e in events if isinstance(e, MessageDelta)] == ["small"]


def test_child_exit_mid_turn_fails_with_stderr_tail(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)
        await adapter.start_turn(
            TurnInput(text="x", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        proc.feed_stderr("Error: credentials expired")
        await drain()
        proc.feed_eof()
        await wait_until(lambda: any(isinstance(e, TurnCompleted) for e in events))
        await adapter.close()
        return events

    events = asyncio.run(scenario())
    completed = events[-1]
    assert completed.status == "failed"
    assert "exited before completing the turn" in completed.error
    assert "credentials expired" in completed.error


def test_stderr_ring_is_bounded(tmp_path):
    from hermes_plugin_relay.adapters.base import STDERR_RING_LINES

    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        await adapter.start_turn(
            TurnInput(text="x", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        for i in range(STDERR_RING_LINES * 3):
            proc.feed_stderr(f"line-{i}")
        await drain(20)
        tail = adapter.stderr_tail(limit=1000)
        await adapter.close()
        return tail

    tail = asyncio.run(scenario())
    assert len(tail.splitlines()) == STDERR_RING_LINES
    assert tail.splitlines()[-1] == f"line-{STDERR_RING_LINES * 3 - 1}"


def test_resume_id_flows_into_argv(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        await adapter.start_turn(
            TurnInput(
                text="x",
                cwd=str(tmp_path),
                participant_turn_id="pturn-1",
                resume_session_id="sess-from-store",
            )
        )
        argv = list(proc.argv)
        proc.feed({"type": "result", "subtype": "success", "is_error": False})
        await drain()
        await adapter.close()
        return argv

    argv = asyncio.run(scenario())
    assert argv[argv.index("--resume") + 1] == "sess-from-store"


def test_missing_binary_reports_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.adapters.base.probe_binary", lambda binary: None)

    async def scenario():
        adapter = ClaudeAdapter(participant_id="claude:default")
        with pytest.raises(AdapterNotAvailableError) as excinfo:
            await adapter.start_turn(
                TurnInput(text="x", cwd=str(tmp_path), participant_turn_id="pturn-1")
            )
        return str(excinfo.value)

    message = asyncio.run(scenario())
    assert "claude CLI not found on PATH" in message


def test_availability_reports_offline_without_binary(monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.adapters.base.probe_binary", lambda b: None)
    availability = ClaudeAdapter.availability()
    assert availability.status == "offline"
    assert "not found on PATH" in availability.reason


def test_second_turn_reuses_warm_child(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        events = collect(adapter)
        for index in (1, 2):
            await adapter.start_turn(
                TurnInput(text=f"turn {index}", cwd=str(tmp_path),
                          participant_turn_id=f"pturn-{index}")
            )
            proc.feed({"type": "system", "subtype": "init", "session_id": "sess-1"})
            proc.feed(text_delta(f"reply {index}"))
            proc.feed({"type": "result", "subtype": "success", "is_error": False})
            await wait_until(
                lambda i=index: len([e for e in events if isinstance(e, TurnCompleted)]) == i
            )
        stdin_lines = list(proc.stdin.json_lines)
        await adapter.close()
        return events, stdin_lines

    events, stdin_lines = asyncio.run(scenario())
    completions = [e for e in events if isinstance(e, TurnCompleted)]
    assert [c.text for c in completions] == ["reply 1", "reply 2"]
    # session_updated is emitted once even though system/init repeats.
    assert len([e for e in events if isinstance(e, SessionUpdated)]) == 1
    assert len(stdin_lines) == 2


def test_concurrent_turn_is_rejected(tmp_path):
    async def scenario():
        proc = FakeProcess()
        adapter = make_adapter(proc)
        await adapter.start_turn(
            TurnInput(text="a", cwd=str(tmp_path), participant_turn_id="pturn-1")
        )
        with pytest.raises(Exception) as excinfo:
            await adapter.start_turn(
                TurnInput(text="b", cwd=str(tmp_path), participant_turn_id="pturn-2")
            )
        await adapter.close()
        return str(excinfo.value)

    message = asyncio.run(scenario())
    assert "already active" in message
