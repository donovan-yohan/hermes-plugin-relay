"""End-to-end acceptance: streamed participant rows persist and rehydrate.

Opt-in. Set ``HERMES_AGENT_ROOT`` to a Hermes checkout containing
``tui_gateway/participants.py`` and the whole file runs against the REAL core
seam, a REAL ``SessionDB`` and a temp Hermes home. Without it every test skips.

Nothing here touches the user's Hermes state: ``HERMES_HOME`` is redirected by
the autouse fixture in ``conftest``, and the fake live-session record carries
``profile_home`` so ``tui_gateway.server._session_db`` opens the temp
``state.db`` instead of the shared handle.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from hermes_plugin_relay.config import ChainConfig, ParticipantConfig, RelayConfig
from hermes_plugin_relay.runtime.manager import RelayRuntimeManager
from hermes_plugin_relay.runtime.persistence import ProviderSessionStore

HERMES_AGENT_ROOT = os.environ.get("HERMES_AGENT_ROOT")

pytestmark = pytest.mark.skipif(
    not HERMES_AGENT_ROOT,
    reason="set HERMES_AGENT_ROOT to a Hermes checkout to run the core-seam integration test",
)

PLUGIN_ID = "hermes-plugin-relay"
GATEWAY_SESSION_ID = "gw-integration-1"


def _import_hermes(name: str):
    root = str(Path(HERMES_AGENT_ROOT).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def seam():
    try:
        module = _import_hermes("tui_gateway.participants")
    except ImportError as exc:
        pytest.skip(f"core seam not present in {HERMES_AGENT_ROOT}: {exc}")
    required = (
        "register_participants",
        "append_participant_user_message",
        "begin_participant_message",
        "append_participant_delta",
        "complete_participant_message",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        pytest.skip(f"core seam not present: tui_gateway.participants lacks {missing}")
    return module


@pytest.fixture
def live_session(seam, temp_hermes_home):
    """Register a real DB session plus a minimal live gateway session record."""

    server = _import_hermes("tui_gateway.server")
    hermes_state = _import_hermes("hermes_state")

    db_path = Path(temp_hermes_home) / "state.db"
    db = hermes_state.SessionDB(db_path=db_path)
    session_key = f"sess-{uuid.uuid4().hex[:12]}"
    try:
        db.create_session(session_key, "desktop")
    finally:
        db.close()

    session = {
        "session_key": session_key,
        "profile_home": str(temp_hermes_home),
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    emitted = []
    original_emit = server._emit
    server._emit = lambda event, sid, payload=None: emitted.append((event, sid, payload))

    with server._sessions_lock:
        server._sessions[GATEWAY_SESSION_ID] = session
    try:
        yield {
            "server": server,
            "session": session,
            "session_key": session_key,
            "db_path": db_path,
            "emitted": emitted,
            "hermes_state": hermes_state,
        }
    finally:
        server._emit = original_emit
        with server._sessions_lock:
            server._sessions.pop(GATEWAY_SESSION_ID, None)


@pytest.fixture
def manager(seam, live_session, tmp_path):
    config = RelayConfig(
        participants=(
            ParticipantConfig(
                id="mock:default",
                handle="mock",
                display_name="Mock Participant",
                adapter="mock",
                options={"chunk_size": 3},
            ),
        ),
        chain=ChainConfig(),
        tool_timeout_seconds=30.0,
        default_cwd=str(tmp_path),
    )
    runtime = RelayRuntimeManager(
        config, seam=seam, store=ProviderSessionStore(tmp_path / "store.json")
    )
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.shutdown()


def read_rows(live_session):
    db = live_session["hermes_state"].SessionDB(db_path=live_session["db_path"])
    try:
        return db.get_messages(live_session["session_key"])
    finally:
        db.close()


def poll_until(predicate, what: str, timeout: float = 10.0):
    """Poll until ``predicate`` returns truthy, failing loudly on the deadline.

    A silent poll loop turns a genuine regression into a confusing assertion
    further down; this names what never happened.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    pytest.fail(f"timed out after {timeout}s waiting for {what}")


def metadata_of(row):
    raw = row.get("display_metadata")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw or {}


# ---------------------------------------------------------------------------


def test_streamed_participant_rows_persist_and_rehydrate(manager, live_session):
    result = manager.dispatch_group(
        GATEWAY_SESSION_ID, "d-int-1", "@mock hello there", ["mock"]
    )
    assert result["ok"] is True
    turn_id = result["turns"][0]["participant_turn_id"]

    poll_until(
        lambda: [
            row
            for row in read_rows(live_session)
            if metadata_of(row).get("participant_turn_id") == turn_id
            and metadata_of(row).get("status") == "completed"
        ],
        "the participant row to be finalized as completed",
    )
    rows = read_rows(live_session)

    kinds = [row.get("display_kind") for row in rows]
    assert "participant_directed" in kinds, kinds
    assert "participant_message" in kinds, kinds

    user_row = next(row for row in rows if row.get("display_kind") == "participant_directed")
    assert user_row["role"] == "user"
    assert user_row["content"] == "@mock hello there"
    user_meta = metadata_of(user_row)
    assert user_meta["mentions"] == ["mock"]
    assert user_meta["plugin_id"] == PLUGIN_ID

    reply_row = next(row for row in rows if row.get("display_kind") == "participant_message")
    assert reply_row["role"] == "assistant"
    assert reply_row["content"] == "mock reply: @mock hello there"
    reply_meta = metadata_of(reply_row)
    assert reply_meta["status"] == "completed"
    assert reply_meta["participant_turn_id"] == turn_id
    assert reply_meta["participant"]["handle"] == "mock"
    assert reply_meta["participant"]["display_name"] == "Mock Participant"
    assert reply_meta["participant"]["plugin_id"] == PLUGIN_ID
    assert reply_meta["participant"]["adapter_id"] == "mock"
    assert "error" not in reply_meta

    # Rows land in insertion order: the human ask precedes the reply.
    assert kinds.index("participant_directed") < kinds.index("participant_message")


def test_gateway_events_are_session_scoped(manager, live_session):
    manager.dispatch_group(GATEWAY_SESSION_ID, "d-int-2", "@mock ping", ["mock"])

    poll_until(
        lambda: "participant.message.complete"
        in [event for event, _sid, _payload in live_session["emitted"]],
        "the participant.message.complete gateway event",
    )

    types = [event for event, _sid, _payload in live_session["emitted"]]
    assert types[0] == "participant.user_message"
    assert "participant.message.start" in types
    assert "participant.message.delta" in types
    assert types[-1] == "participant.message.complete"
    assert {sid for _event, sid, _payload in live_session["emitted"]} == {GATEWAY_SESSION_ID}

    complete = [p for e, _s, p in live_session["emitted"] if e == "participant.message.complete"][-1]
    assert complete["status"] == "completed"
    assert complete["text"] == "mock reply: @mock ping"


def test_live_history_carries_both_rows(manager, live_session):
    manager.dispatch_group(GATEWAY_SESSION_ID, "d-int-3", "@mock history", ["mock"])

    poll_until(
        lambda: any(
            entry.get("display_kind") == "participant_message" and entry.get("content")
            for entry in (live_session["session"].get("history") or [])
        ),
        "the participant reply to land in live history",
    )
    history = list(live_session["session"].get("history") or [])
    kinds = [entry.get("display_kind") for entry in history]
    assert kinds == ["participant_directed", "participant_message"]
    assert history[1]["content"] == "mock reply: @mock history"
    assert history[1]["display_metadata"]["status"] == "completed"


def test_model_projection_wraps_the_reply_as_untrusted_peer_content(manager, live_session):
    """Contract section 2: the reply reaches the model only inside a user envelope."""

    conversation_loop = _import_hermes("agent.conversation_loop")
    project = getattr(conversation_loop, "_project_participant_messages", None)
    if project is None:
        pytest.skip("core seam not present: agent.conversation_loop lacks the projection")

    manager.dispatch_group(GATEWAY_SESSION_ID, "d-int-4", "@mock projected", ["mock"])
    poll_until(
        lambda: any(
            entry.get("display_kind") == "participant_message" and entry.get("content")
            for entry in (live_session["session"].get("history") or [])
        ),
        "the participant reply to land in live history",
    )
    history = list(live_session["session"].get("history") or [])
    projected, dropped = project(history)

    roles = [message.get("role") for message in projected]
    assert "assistant" not in roles
    assert "system" not in roles
    reply_text = "mock reply: @mock projected"
    carriers = [
        message
        for message in projected
        if isinstance(message.get("content"), str) and reply_text in message["content"]
    ]
    assert carriers, projected
    assert all(message["role"] == "user" for message in carriers)


def test_interrupted_turn_persists_its_status(manager, live_session, tmp_path):
    manager.config = RelayConfig(
        participants=(
            ParticipantConfig(
                id="mock:default",
                handle="mock",
                display_name="Mock Participant",
                adapter="mock",
                options={"hang": True},
            ),
        ),
        chain=ChainConfig(),
        tool_timeout_seconds=30.0,
        default_cwd=str(tmp_path),
    )
    result = manager.dispatch_group(GATEWAY_SESSION_ID, "d-int-5", "@mock stall", ["mock"])
    turn_id = result["turns"][0]["participant_turn_id"]

    poll_until(
        lambda: manager.roster(GATEWAY_SESSION_ID)[0]["status"] == "busy",
        "the participant to report busy",
    )

    manager.interrupt(GATEWAY_SESSION_ID, "mock:default")

    def _settled():
        for row in read_rows(live_session):
            meta = metadata_of(row)
            if meta.get("participant_turn_id") == turn_id and meta.get("status") != "streaming":
                return meta
        return None

    reply_meta = poll_until(_settled, "the interrupted turn to leave streaming state")
    assert reply_meta.get("status") == "interrupted"
