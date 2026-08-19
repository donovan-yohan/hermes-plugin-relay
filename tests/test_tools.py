"""Hermes tool handlers, with the session-resolution rules of contract v1.2.

The publish target is the live gateway/UI session id, resolved per call from
``gateway.session_context.get_session_env("HERMES_UI_SESSION_ID")``. Never from
handler kwargs, never ``HERMES_SESSION_ID``, never a process-global.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from conftest import install_seam_module, install_session_context

from hermes_plugin_relay.config import ChainConfig, ParticipantConfig, RelayConfig
from hermes_plugin_relay.runtime.manager import RelayRuntimeManager
from hermes_plugin_relay.runtime.persistence import ProviderSessionStore
from hermes_plugin_relay.tools import (
    NO_SESSION_ERROR,
    PARTICIPANT_CONTENT_LIMIT,
    SCHEMAS,
    TOOL_NAMES,
    TOOLSET,
    cap_participant_text,
    make_handlers,
    register_tools,
    resolve_ui_session_id,
)


def participant(handle: str, **options) -> ParticipantConfig:
    return ParticipantConfig(
        id=f"{handle}:default",
        handle=handle,
        display_name=handle.title(),
        adapter="mock",
        options=options,
    )


class RecordingManager:
    """Captures exactly which session id each tool call routed to."""

    def __init__(self, *, barrier: threading.Barrier = None) -> None:
        self.config = RelayConfig(participants=(participant("claude"),), chain=ChainConfig())
        self.dispatches = []
        self.interrupts = []
        self.rosters = []
        self._barrier = barrier
        self._lock = threading.Lock()

    def dispatch(self, session_id, participant_id, text, **kwargs):
        if self._barrier is not None:
            # Hold both callers inside dispatch simultaneously.
            self._barrier.wait(timeout=5)
        with self._lock:
            self.dispatches.append(
                {"session_id": session_id, "participant": participant_id, "text": text, **kwargs}
            )
        return {
            "ok": True,
            "participant_id": "claude:default",
            "participant_turn_id": f"pturn-{session_id}",
            "status": "completed",
            "text": f"reply for {session_id}",
        }

    def interrupt(self, session_id, participant_id):
        self.interrupts.append((session_id, participant_id))
        return {"ok": True, "participant_id": participant_id, "status": "interrupt_requested"}

    def roster(self, session_id=None):
        self.rosters.append(session_id)
        return [{"id": "claude:default", "handle": "claude", "status": "ready"}]


@pytest.fixture
def ui_session(monkeypatch):
    """Fake ``gateway.session_context`` whose value is per-thread."""

    local = threading.local()

    def get_session_env(name, default=""):
        if name == "HERMES_UI_SESSION_ID":
            return getattr(local, "ui_session_id", default)
        if name == "HERMES_SESSION_ID":
            return getattr(local, "db_session_id", default)
        return default

    install_session_context(get_session_env, monkeypatch)
    return local


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------


def test_agent_message_routes_to_the_live_ui_session(ui_session):
    ui_session.ui_session_id = "S1"
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)

    raw = handlers["agent_message"]({"participant": "claude", "message": "hello"})
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["text"] == "reply for S1"
    assert len(manager.dispatches) == 1
    call = manager.dispatches[0]
    assert call["session_id"] == "S1"
    assert call["append_user_message"] is False
    assert call["wait"] is True
    # The runtime owns the timeout policy; the tool does not pass one.
    assert "timeout" not in call


def test_handler_kwargs_session_id_is_ignored(ui_session):
    ui_session.ui_session_id = "S1"
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)

    handlers["agent_message"](
        {"participant": "claude", "message": "hi"},
        session_id="db-session-999",
        task_id="task-1",
    )
    assert manager.dispatches[0]["session_id"] == "S1"


def test_hermes_session_id_is_never_used_as_a_fallback(ui_session):
    """HERMES_SESSION_ID is the durable DB id: unusable as a publish key."""

    ui_session.ui_session_id = ""
    ui_session.db_session_id = "db-session-42"
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)

    payload = json.loads(handlers["agent_message"]({"participant": "claude", "message": "hi"}))
    assert payload == {"ok": False, "error": NO_SESSION_ERROR}
    assert manager.dispatches == []


def test_core_resolver_is_used_when_ui_env_is_unset(ui_session, fake_seam, monkeypatch):
    ui_session.ui_session_id = ""
    fake_seam.resolve_publish_session_id = lambda explicit=None: "S-from-core"
    install_seam_module(fake_seam, monkeypatch)

    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)
    payload = json.loads(handlers["agent_message"]({"participant": "claude", "message": "hi"}))

    assert payload["ok"] is True
    assert manager.dispatches[0]["session_id"] == "S-from-core"


def test_core_resolver_failure_is_treated_as_unresolvable(ui_session, fake_seam, monkeypatch):
    ui_session.ui_session_id = ""

    def boom(explicit=None):
        raise fake_seam.UnknownSessionError("no live session")

    fake_seam.resolve_publish_session_id = boom
    install_seam_module(fake_seam, monkeypatch)

    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)
    payload = json.loads(handlers["agent_message"]({"participant": "claude", "message": "hi"}))
    assert payload["ok"] is False
    assert payload["error"] == NO_SESSION_ERROR
    assert manager.dispatches == []


def test_concurrent_sessions_do_not_cross_route(ui_session):
    """Two tool calls in flight at once must each publish into their own session."""

    barrier = threading.Barrier(2, timeout=5)
    manager = RecordingManager(barrier=barrier)
    handlers = make_handlers(lambda: manager)
    results = {}

    def run(session_id: str) -> None:
        ui_session.ui_session_id = session_id
        results[session_id] = json.loads(
            handlers["agent_message"]({"participant": "claude", "message": f"from {session_id}"})
        )

    threads = [threading.Thread(target=run, args=(sid,)) for sid in ("S1", "S2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results["S1"]["text"] == "reply for S1"
    assert results["S2"]["text"] == "reply for S2"

    routed = {call["session_id"]: call["text"] for call in manager.dispatches}
    assert routed == {"S1": "from S1", "S2": "from S2"}

    # Nothing was cached process-wide: a context with no UI session still refuses.
    ui_session.ui_session_id = ""
    payload = json.loads(handlers["agent_message"]({"participant": "claude", "message": "x"}))
    assert payload["error"] == NO_SESSION_ERROR


def test_resolve_returns_none_without_any_hermes_context(hermes_absent):
    assert resolve_ui_session_id() is None


# ---------------------------------------------------------------------------
# Handler behavior
# ---------------------------------------------------------------------------


def test_all_handlers_return_valid_json(ui_session):
    ui_session.ui_session_id = "S1"
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)

    assert set(handlers) == set(TOOL_NAMES)
    payloads = [
        json.loads(handlers["agent_participants_list"]({})),
        json.loads(handlers["agent_message"]({"participant": "claude", "message": "hi"})),
        json.loads(handlers["agent_interrupt"]({"participant": "claude"})),
    ]
    for payload in payloads:
        assert isinstance(payload, dict)
        assert "ok" in payload


def test_participants_list_returns_roster(ui_session):
    ui_session.ui_session_id = "S1"
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)
    payload = json.loads(handlers["agent_participants_list"]({}))
    assert payload["ok"] is True
    assert payload["participants"][0]["handle"] == "claude"
    assert manager.rosters == ["S1"]


def test_participants_list_works_without_a_session(ui_session):
    ui_session.ui_session_id = ""
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)
    payload = json.loads(handlers["agent_participants_list"]({}))
    assert payload["ok"] is True
    assert manager.rosters == [None]


def test_interrupt_requires_a_session(ui_session):
    ui_session.ui_session_id = ""
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)
    payload = json.loads(handlers["agent_interrupt"]({"participant": "claude"}))
    assert payload["error"] == NO_SESSION_ERROR
    assert manager.interrupts == []


def test_interrupt_routes_to_the_live_session(ui_session):
    ui_session.ui_session_id = "S1"
    manager = RecordingManager()
    handlers = make_handlers(lambda: manager)
    payload = json.loads(handlers["agent_interrupt"]({"participant": "claude"}))
    assert payload["ok"] is True
    assert manager.interrupts == [("S1", "claude")]


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("agent_message", {"message": "hi"}, "'participant' is required"),
        ("agent_message", {"participant": "claude"}, "'message' is required"),
        ("agent_interrupt", {}, "'participant' is required"),
    ],
)
def test_missing_arguments_return_visible_errors(ui_session, name, args, expected):
    ui_session.ui_session_id = "S1"
    handlers = make_handlers(lambda: RecordingManager())
    payload = json.loads(handlers[name](args))
    assert payload["ok"] is False
    assert expected in payload["error"]


def test_manager_exceptions_become_visible_json(ui_session):
    ui_session.ui_session_id = "S1"

    class BrokenManager(RecordingManager):
        def dispatch(self, *args, **kwargs):
            raise RuntimeError("hermes core seam missing")

    handlers = make_handlers(lambda: BrokenManager())
    payload = json.loads(handlers["agent_message"]({"participant": "claude", "message": "hi"}))
    assert payload["ok"] is False
    assert payload["error"] == "hermes core seam missing"
    assert payload["participant"] == "claude"


# ---------------------------------------------------------------------------
# End-to-end through the real manager
# ---------------------------------------------------------------------------


def test_tool_result_text_is_capped_with_the_core_marker(ui_session, fake_seam, tmp_path):
    """Contract v1.5: untrusted peer text must not reach Hermes unbounded."""

    ui_session.ui_session_id = "S-live"
    oversize = "z" * (PARTICIPANT_CONTENT_LIMIT + 500)
    config = RelayConfig(
        participants=(participant("claude", reply=lambda _text: oversize),),
        chain=ChainConfig(),
        tool_timeout_seconds=10.0,
        default_cwd=str(tmp_path),
    )
    manager = RelayRuntimeManager(
        config, seam=fake_seam, store=ProviderSessionStore(tmp_path / "s.json")
    )
    manager.start()
    try:
        handlers = make_handlers(lambda: manager)
        payload = json.loads(
            handlers["agent_message"]({"participant": "claude", "message": "go"})
        )
    finally:
        manager.shutdown()

    text = payload["text"]
    assert len(text) < len(oversize)
    assert text.startswith("z" * 100)
    assert text.endswith("\n[truncated: 500 more characters]")
    assert text[:PARTICIPANT_CONTENT_LIMIT] == oversize[:PARTICIPANT_CONTENT_LIMIT]

    # The full text still reached the transcript row: only the model-facing
    # tool result is bounded.
    complete = fake_seam.calls_named("complete_participant_message")[0]
    assert len(complete[5]) == len(oversize)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("short", "short"),
        ("x" * PARTICIPANT_CONTENT_LIMIT, "x" * PARTICIPANT_CONTENT_LIMIT),
        (None, None),
        (123, 123),
    ],
)
def test_cap_participant_text_leaves_bounded_values_alone(value, expected):
    assert cap_participant_text(value) == expected


def test_cap_marker_matches_the_hermes_core_formula():
    """Byte-identical to agent/conversation_loop.py::_sanitize_participant_content."""

    text = "a" * (PARTICIPANT_CONTENT_LIMIT + 7)
    omitted = len(text) - PARTICIPANT_CONTENT_LIMIT
    expected = f"{text[:PARTICIPANT_CONTENT_LIMIT]}\n[truncated: {omitted} more characters]"
    assert cap_participant_text(text) == expected


def test_tool_result_error_is_capped_too(ui_session):
    ui_session.ui_session_id = "S1"

    class HugeErrorManager(RecordingManager):
        def dispatch(self, *args, **kwargs):
            return {
                "ok": False,
                "participant_id": "claude:default",
                "participant_turn_id": "pturn-x",
                "status": "failed",
                "text": "",
                "error": "e" * (PARTICIPANT_CONTENT_LIMIT + 10),
            }

    handlers = make_handlers(lambda: HugeErrorManager())
    payload = json.loads(handlers["agent_message"]({"participant": "claude", "message": "hi"}))
    assert payload["error"].endswith("\n[truncated: 10 more characters]")


def test_agent_message_returns_mock_adapter_text(ui_session, fake_seam, tmp_path):
    ui_session.ui_session_id = "S-live"
    config = RelayConfig(
        participants=(participant("claude"),),
        chain=ChainConfig(),
        tool_timeout_seconds=10.0,
        default_cwd=str(tmp_path),
    )
    manager = RelayRuntimeManager(
        config, seam=fake_seam, store=ProviderSessionStore(tmp_path / "s.json")
    )
    manager.start()
    try:
        handlers = make_handlers(lambda: manager)
        payload = json.loads(
            handlers["agent_message"]({"participant": "claude", "message": "ping"})
        )
    finally:
        manager.shutdown()

    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["text"] == "mock reply: ping"
    assert payload["participant_turn_id"].startswith("pturn-")
    # The reply is also published through the seam into the live UI session.
    begin = fake_seam.calls_named("begin_participant_message")[0]
    assert begin[1] == "S-live"
    assert fake_seam.calls_named("append_participant_user_message") == []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_tools_uses_the_plugin_toolset():
    calls = []

    class Ctx:
        def register_tool(self, **kwargs):
            calls.append(kwargs)

    register_tools(Ctx())
    assert [call["name"] for call in calls] == list(TOOL_NAMES)
    for call in calls:
        assert call["toolset"] == TOOLSET
        assert callable(call["handler"])
        assert call["description"]
        assert call["schema"]["function"]["name"] == call["name"]


def test_schemas_are_well_formed():
    for name in TOOL_NAMES:
        schema = SCHEMAS[name]
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] == name
        assert isinstance(function["description"], str) and function["description"]
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert isinstance(parameters["properties"], dict)
        for required in parameters.get("required", []):
            assert required in parameters["properties"]
        json.dumps(schema)
