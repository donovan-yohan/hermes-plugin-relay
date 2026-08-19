"""REST surface tests (contract section 6), driven the way the host loads it.

``dashboard/plugin_api.py`` is imported as a flat module with
``spec_from_file_location`` — exactly what ``web_server._mount_plugin_api_routes``
does — so the package-resolution bootstrap inside it is under test too.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import ROOT

from hermes_plugin_relay.config import ChainConfig, ParticipantConfig, RelayConfig
from hermes_plugin_relay.runtime.manager import RelayRuntimeManager, set_manager
from hermes_plugin_relay.runtime.persistence import ProviderSessionStore

PREFIX = "/api/plugins/hermes-plugin-relay"
SESSION = "gw-session-1"

# Mirrors the host's module naming for a mounted plugin API.
HOST_MODULE_NAME = "hermes_dashboard_plugin_hermes-plugin-relay"


def participant(handle: str, **options) -> ParticipantConfig:
    return ParticipantConfig(
        id=f"{handle}:default",
        handle=handle,
        display_name=handle.title(),
        adapter="mock",
        options=options,
    )


def build_config(tmp_path, *participants) -> RelayConfig:
    return RelayConfig(
        participants=participants or (participant("claude"), participant("codex")),
        chain=ChainConfig(),
        tool_timeout_seconds=10.0,
        default_cwd=str(tmp_path),
    )


class SlowStartManager(RelayRuntimeManager):
    async def _start_turn(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await super()._start_turn(*args, **kwargs)


@pytest.fixture
def api_module():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(HOST_MODULE_NAME, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[HOST_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(HOST_MODULE_NAME, None)


@pytest.fixture
def client(api_module):
    app = FastAPI()
    app.include_router(api_module.router, prefix=PREFIX)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_manager(fake_seam, tmp_path):
    created = []

    def _make(config=None, *, cls=RelayRuntimeManager, **kwargs):
        kwargs.setdefault("seam", fake_seam)
        kwargs.setdefault("store", ProviderSessionStore(tmp_path / "store.json"))
        manager = cls(config or build_config(tmp_path), **kwargs)
        manager.start()
        set_manager(manager)
        created.append(manager)
        return manager

    yield _make
    for manager in created:
        manager.shutdown()


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def body(**overrides) -> dict:
    payload = {
        "session_id": SESSION,
        "dispatch_id": "d-1",
        "text": "@claude take a look",
        "mentions": ["claude"],
        "append_user_message": True,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Package resolution
# ---------------------------------------------------------------------------


def test_flat_module_load_reuses_the_already_imported_package(api_module):
    """A second package copy would mean a second runtime manager."""

    assert api_module._PKG == "hermes_plugin_relay"
    assert api_module._manager_mod is sys.modules["hermes_plugin_relay.runtime.manager"]


# ---------------------------------------------------------------------------
# GET /participants
# ---------------------------------------------------------------------------


def test_participants_roster_shape(client, make_manager):
    make_manager()
    response = client.get(f"{PREFIX}/participants")
    assert response.status_code == 200
    participants = response.json()["participants"]
    assert [entry["handle"] for entry in participants] == ["claude", "codex"]
    for entry in participants:
        assert set(entry) >= {"id", "handle", "display_name", "adapter_id", "status", "capabilities"}
        assert entry["status"] in {"ready", "busy", "offline", "error"}
        assert entry["capabilities"]["text"] is True


def test_participants_reports_busy_for_a_session(client, make_manager, tmp_path):
    manager = make_manager(build_config(tmp_path, participant("claude", hang=True)))
    manager.dispatch(SESSION, "claude", "long")
    assert wait_for(lambda: manager.roster(SESSION)[0]["status"] == "busy")

    scoped = client.get(f"{PREFIX}/participants", params={"session_id": SESSION}).json()
    assert scoped["participants"][0]["status"] == "busy"
    unscoped = client.get(f"{PREFIX}/participants").json()
    assert unscoped["participants"][0]["status"] == "ready"


def test_roster_failure_does_not_leak_exception_detail(client, make_manager, caplog):
    manager = make_manager()

    def boom(session_id=None):
        raise RuntimeError("/home/secret/path/state.db is locked by pid 4242")

    manager.roster = boom
    response = client.get(f"{PREFIX}/participants")

    assert response.status_code == 500
    body = response.json()
    assert body == {"ok": False, "error": "failed to build the participant roster"}
    assert "secret" not in response.text
    assert "4242" not in response.text


def test_participants_reports_error_without_the_seam(client, tmp_path, seam_absent):
    manager = RelayRuntimeManager(
        build_config(tmp_path, participant("claude")),
        store=ProviderSessionStore(tmp_path / "s.json"),
    )
    set_manager(manager)
    try:
        entry = client.get(f"{PREFIX}/participants").json()["participants"][0]
        assert entry["status"] == "error"
        assert entry["reason"] == "hermes core seam missing"
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# POST /dispatch
# ---------------------------------------------------------------------------


def test_dispatch_returns_turn_refs(client, make_manager, fake_seam):
    make_manager()
    response = client.post(f"{PREFIX}/dispatch", json=body())
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["turns"] == [
        {"participant_id": "claude:default", "participant_turn_id": payload["turns"][0]["participant_turn_id"]}
    ]
    assert payload["turns"][0]["participant_turn_id"].startswith("pturn-")
    assert payload["user_row_appended"] is True
    assert wait_for(lambda: fake_seam.calls_named("complete_participant_message"))
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1


def test_dispatch_without_user_row(client, make_manager, fake_seam):
    make_manager()
    response = client.post(f"{PREFIX}/dispatch", json=body(append_user_message=False))
    assert response.status_code == 200
    assert response.json()["user_row_appended"] is False
    assert wait_for(lambda: fake_seam.calls_named("complete_participant_message"))
    assert fake_seam.calls_named("append_participant_user_message") == []


def test_dispatch_fans_out_to_every_mention(client, make_manager, fake_seam):
    make_manager()
    response = client.post(
        f"{PREFIX}/dispatch", json=body(mentions=["claude", "codex"], text="@claude @codex hi")
    )
    payload = response.json()
    assert [turn["participant_id"] for turn in payload["turns"]] == [
        "claude:default",
        "codex:default",
    ]
    assert wait_for(lambda: len(fake_seam.calls_named("complete_participant_message")) == 2)
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"dispatch_id": ""}, "'dispatch_id' is required"),
        ({"session_id": ""}, "'session_id' is required"),
        ({"text": ""}, "'text' is required"),
        ({"mentions": []}, "'mentions' is required"),
        ({"mentions": "claude"}, "'mentions' is required"),
        ({"append_user_message": "yes"}, "'append_user_message' must be a boolean"),
    ],
)
def test_dispatch_validation_errors_are_4xx(client, make_manager, fake_seam, payload, fragment):
    make_manager()
    response = client.post(f"{PREFIX}/dispatch", json=body(**payload))
    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert fragment in response.json()["error"]
    assert fake_seam.calls == []


def test_dispatch_missing_dispatch_id_key_is_rejected(client, make_manager, fake_seam):
    make_manager()
    request = body()
    request.pop("dispatch_id")
    response = client.post(f"{PREFIX}/dispatch", json=request)
    assert response.status_code == 400
    assert "'dispatch_id' is required" in response.json()["error"]
    assert fake_seam.calls == []


def test_dispatch_non_object_body_is_rejected(client, make_manager):
    make_manager()
    response = client.post(f"{PREFIX}/dispatch", json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_dispatch_unknown_participant_is_4xx(client, make_manager):
    make_manager()
    response = client.post(f"{PREFIX}/dispatch", json=body(mentions=["nobody"]))
    assert response.status_code == 400
    assert "unknown participants" in response.json()["error"] or "no known participants" in response.json()["error"]


class AllQueueFailManager(RelayRuntimeManager):
    """Every participant fails to queue, so nothing lands at all."""

    async def _start_turn(self, session_id, participant, *args, **kwargs):
        raise RuntimeError(
            "failed to spawn /opt/private/tools/bin/claude: No such file or directory"
        )


def test_committed_failure_detail_never_reaches_the_body(client, make_manager, caplog):
    """The manager raises a bare RelayRuntimeError carrying joined spawn errors.

    Those strings can hold filesystem paths and provider diagnostics, so the
    body must be generic even though the exception is the runtime's own type.
    """

    make_manager(cls=AllQueueFailManager)
    response = client.post(f"{PREFIX}/dispatch", json=body(append_user_message=False))

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "dispatch failed unexpectedly"}
    for leaked in ("/opt/private", "claude", "No such file", "spawn"):
        assert leaked not in response.text, leaked
    # The detail is still recoverable — from the log, not the wire.
    assert "/opt/private/tools/bin/claude" in caplog.text


def test_pre_acceptance_errors_keep_their_message(client, make_manager):
    """A request-shaped error is safe to explain, and stays 4xx."""

    make_manager()
    response = client.post(f"{PREFIX}/dispatch", json=body(mentions=["nobody"]))
    assert 400 <= response.status_code < 500
    assert "nobody" in response.json()["error"]


def test_unclassified_dispatch_failure_is_500_never_4xx(client, make_manager):
    """A 4xx tells Desktop "nothing happened" and licenses a Hermes fallback.

    An unclassified failure may have appended the user row already, so
    advertising it as pre-acceptance would duplicate the human's message.
    """

    manager = make_manager()

    def boom(*args, **kwargs):
        raise ValueError("something the runtime never classified")

    manager.dispatch_group = boom
    response = client.post(f"{PREFIX}/dispatch", json=body())

    assert response.status_code == 500
    assert response.json()["ok"] is False
    # The detail stays in the log, not the body.
    assert "never classified" not in response.text


def test_unclassified_interrupt_failure_is_500(client, make_manager):
    manager = make_manager()

    def boom(*args, **kwargs):
        raise ValueError("unclassified")

    manager.interrupt = boom
    response = client.post(
        f"{PREFIX}/interrupt", json={"session_id": SESSION, "participant_id": "claude:default"}
    )
    assert response.status_code == 500


def test_classified_errors_use_their_declared_status(api_module):
    """The route ladder reads the status off the exception, not a local table."""

    assert api_module.SeamUnavailableError.http_status == 424
    assert api_module.DispatchCapacityError.http_status == 429
    assert api_module.DispatchValidationError.http_status == 400
    assert api_module.ParticipantNotFoundError.http_status == 400
    assert api_module.RelayRuntimeError.http_status == 500
    # Only classified failures may claim "nothing happened".
    assert api_module.RelayRuntimeError.pre_acceptance is False
    assert api_module.DispatchValidationError.pre_acceptance is True


def test_dispatch_preserves_text_verbatim(client, make_manager, fake_seam):
    """The human row is persisted as typed, so validation must not strip it."""

    make_manager()
    padded = "  @claude look at this  "
    client.post(f"{PREFIX}/dispatch", json=body(text=padded))
    assert wait_for(lambda: fake_seam.calls_named("append_participant_user_message"))
    assert fake_seam.calls_named("append_participant_user_message")[0][3] == padded


def test_dispatch_without_seam_is_4xx(client, tmp_path, api_module, seam_absent):
    manager = RelayRuntimeManager(
        build_config(tmp_path, participant("claude")),
        store=ProviderSessionStore(tmp_path / "s.json"),
    )
    set_manager(manager)
    try:
        response = client.post(f"{PREFIX}/dispatch", json=body())
        assert response.status_code == api_module.SEAM_UNAVAILABLE_STATUS
        assert 400 <= response.status_code < 500
        assert response.json()["error"] == "hermes core seam missing"
    finally:
        manager.shutdown()


# ---------------------------------------------------------------------------
# Exactly-once
# ---------------------------------------------------------------------------


def test_retry_after_response_loss_returns_identical_turn_refs(client, make_manager, fake_seam):
    make_manager()
    first = client.post(f"{PREFIX}/dispatch", json=body()).json()
    second = client.post(f"{PREFIX}/dispatch", json=body()).json()

    assert first == second
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1
    assert wait_for(lambda: len(fake_seam.calls_named("begin_participant_message")) == 1)


def test_concurrent_duplicate_posts_fan_out_once(client, make_manager, fake_seam):
    make_manager(cls=SlowStartManager)
    request = body(mentions=["claude", "codex"], text="@claude @codex go")
    results = {}
    barrier = threading.Barrier(2, timeout=10)

    def post(slot: str) -> None:
        barrier.wait()
        results[slot] = client.post(f"{PREFIX}/dispatch", json=request).json()

    threads = [threading.Thread(target=post, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert results["a"] == results["b"]
    assert len(results["a"]["turns"]) == 2
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1
    assert wait_for(lambda: len(fake_seam.calls_named("begin_participant_message")) == 2)
    begins = sorted(call[3] for call in fake_seam.calls_named("begin_participant_message"))
    assert begins == ["claude:default", "codex:default"]


def test_capacity_pressure_returns_429_and_keeps_live_entries(
    client, make_manager, fake_seam, api_module
):
    manager = make_manager(cls=SlowStartManager)
    manager.idempotency.max_entries = 2

    statuses = []
    barrier = threading.Barrier(3, timeout=10)

    def post(dispatch_id: str) -> None:
        barrier.wait()
        response = client.post(f"{PREFIX}/dispatch", json=body(dispatch_id=dispatch_id))
        statuses.append((dispatch_id, response.status_code, response.json()))

    threads = [threading.Thread(target=post, args=(f"d-{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    codes = sorted(code for _, code, _ in statuses)
    assert codes == [200, 200, api_module.CAPACITY_STATUS]
    rejected = [payload for _, code, payload in statuses if code == api_module.CAPACITY_STATUS][0]
    assert rejected["ok"] is False
    assert "capacity" in rejected["error"]
    assert len(manager.idempotency) == 2
    assert len(fake_seam.calls_named("append_participant_user_message")) == 2


# ---------------------------------------------------------------------------
# POST /interrupt
# ---------------------------------------------------------------------------


def test_interrupt_reports_idle_without_an_active_turn(client, make_manager):
    make_manager()
    response = client.post(
        f"{PREFIX}/interrupt", json={"session_id": SESSION, "participant_id": "claude:default"}
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "participant_id": "claude:default",
        "status": "idle",
        "error": "no active turn",
    }


def test_interrupt_stops_an_active_turn(client, make_manager, fake_seam, tmp_path):
    make_manager(build_config(tmp_path, participant("claude", hang=True)))
    client.post(f"{PREFIX}/dispatch", json=body())
    assert wait_for(lambda: fake_seam.calls_named("begin_participant_message"))

    response = client.post(
        f"{PREFIX}/interrupt", json={"session_id": SESSION, "participant_id": "claude:default"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["status"] == "interrupt_requested"
    assert wait_for(
        lambda: fake_seam.calls_named("complete_participant_message")
        and fake_seam.calls_named("complete_participant_message")[0][4] == "interrupted"
    )


@pytest.mark.parametrize(
    "payload", [{"session_id": SESSION}, {"participant_id": "claude"}, {}]
)
def test_interrupt_validation_errors(client, make_manager, payload):
    make_manager()
    response = client.post(f"{PREFIX}/interrupt", json=payload)
    assert response.status_code == 400
    assert response.json()["ok"] is False


def test_interrupt_unknown_participant_is_4xx(client, make_manager):
    make_manager()
    response = client.post(
        f"{PREFIX}/interrupt", json={"session_id": SESSION, "participant_id": "nobody"}
    )
    assert response.status_code == 400
    assert "unknown participant" in response.json()["error"]
