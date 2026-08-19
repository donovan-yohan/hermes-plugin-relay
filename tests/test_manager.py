"""Runtime manager: one dispatch path, ownership, queueing, chain brakes."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from conftest import install_seam_module

from hermes_plugin_relay.adapters.base import AdapterError, TurnInput
from hermes_plugin_relay.adapters.mock import MockAdapter
from hermes_plugin_relay.config import ChainConfig, ParticipantConfig, RelayConfig
from hermes_plugin_relay.runtime.manager import (
    DispatchCapacityError,
    ParticipantNotFoundError,
    RelayRuntimeManager,
    SEAM_MISSING_REASON,
    SeamUnavailableError,
)
from hermes_plugin_relay.runtime.persistence import ProviderSessionStore

SESSION = "gw-session-1"
PLUGIN_ID = "hermes-plugin-relay"


def participant(handle: str, adapter: str = "mock", **options) -> ParticipantConfig:
    return ParticipantConfig(
        id=f"{handle}:default",
        handle=handle,
        display_name=handle.title(),
        adapter=adapter,
        options=options,
    )


def build_config(*participants: ParticipantConfig, chain: ChainConfig = None, cwd=None) -> RelayConfig:
    return RelayConfig(
        participants=participants or (participant("claude"), participant("codex")),
        chain=chain or ChainConfig(),
        tool_timeout_seconds=10.0,
        default_cwd=str(cwd) if cwd else None,
    )


@pytest.fixture
def make_manager(fake_seam, tmp_path):
    created = []

    def _make(config=None, *, cls=RelayRuntimeManager, **kwargs):
        kwargs.setdefault("seam", fake_seam)
        kwargs.setdefault("store", ProviderSessionStore(tmp_path / "store.json"))
        manager = cls(config or build_config(cwd=tmp_path), **kwargs)
        manager.start()
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


# ---------------------------------------------------------------------------
# Human path
# ---------------------------------------------------------------------------


def test_human_dispatch_publishes_user_row_then_streamed_reply(make_manager, fake_seam):
    manager = make_manager()
    result = manager.dispatch_group(SESSION, "d-1", "@claude review this", ["claude"])

    assert result["ok"] is True
    assert result["user_row_appended"] is True
    assert result["failed"] == []
    turn_id = result["turns"][0]["participant_turn_id"]
    assert turn_id.startswith("pturn-")
    assert result["turns"][0]["participant_id"] == "claude:default"

    assert wait_for(lambda: fake_seam.turns.get(turn_id, {}).get("status") == "completed")

    names = [call[0] for call in fake_seam.calls]
    assert names[0] == "register_participants"
    assert names[1] == "append_participant_user_message"
    assert names[2] == "begin_participant_message"
    assert "append_participant_delta" in names
    assert names[-1] == "complete_participant_message"

    user_call = fake_seam.calls_named("append_participant_user_message")[0]
    assert user_call[1:] == (SESSION, PLUGIN_ID, "@claude review this", ["claude"])

    begin_call = fake_seam.calls_named("begin_participant_message")[0]
    assert begin_call[1:] == (SESSION, PLUGIN_ID, "claude:default", turn_id)

    for call in fake_seam.calls_named("append_participant_delta"):
        assert call[1:4] == (SESSION, PLUGIN_ID, turn_id)

    complete = fake_seam.calls_named("complete_participant_message")[0]
    assert complete[1:] == (SESSION, PLUGIN_ID, turn_id, "completed",
                            "mock reply: @claude review this", None)


def test_dispatch_group_without_user_row_appends_nothing(make_manager, fake_seam):
    manager = make_manager()
    result = manager.dispatch_group(
        SESSION, "d-1", "@claude and @hermes", ["claude"], append_user_message=False
    )
    assert result["user_row_appended"] is False
    assert wait_for(lambda: fake_seam.calls_named("complete_participant_message"))
    assert fake_seam.calls_named("append_participant_user_message") == []


def test_group_fanout_hits_each_participant_once(make_manager, fake_seam):
    manager = make_manager()
    result = manager.dispatch_group(SESSION, "d-1", "@claude @codex hi", ["claude", "codex"])
    assert [t["participant_id"] for t in result["turns"]] == ["claude:default", "codex:default"]
    assert wait_for(lambda: len(fake_seam.calls_named("complete_participant_message")) == 2)
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1


def test_unknown_mentions_are_rejected(make_manager):
    manager = make_manager()
    with pytest.raises(ParticipantNotFoundError):
        manager.dispatch_group(SESSION, "d-1", "@nobody", ["nobody"])


# ---------------------------------------------------------------------------
# Hermes tool path
# ---------------------------------------------------------------------------


def test_hermes_dispatch_uses_same_path_without_user_row(make_manager, fake_seam):
    manager = make_manager()
    result = manager.dispatch(SESSION, "claude", "review this diff", wait=True, timeout=5)
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["text"] == "mock reply: review this diff"
    assert result["participant_id"] == "claude:default"

    assert fake_seam.calls_named("append_participant_user_message") == []
    assert len(fake_seam.calls_named("begin_participant_message")) == 1
    assert len(fake_seam.calls_named("complete_participant_message")) == 1


def test_dispatch_resolves_by_id_and_by_handle(make_manager):
    manager = make_manager()
    by_handle = manager.dispatch(SESSION, "claude", "a", wait=True, timeout=5)
    by_id = manager.dispatch(SESSION, "claude:default", "b", wait=True, timeout=5)
    assert by_handle["participant_id"] == by_id["participant_id"] == "claude:default"


# ---------------------------------------------------------------------------
# Queueing, interrupt, failure
# ---------------------------------------------------------------------------


def test_second_dispatch_queues_behind_the_active_turn(make_manager, fake_seam, tmp_path):
    config = build_config(participant("claude", hang=True), cwd=tmp_path)
    manager = make_manager(config)

    first = manager.dispatch(SESSION, "claude", "long one")
    assert wait_for(lambda: fake_seam.calls_named("begin_participant_message"))

    second = manager.dispatch(SESSION, "claude", "queued one")
    assert second["status"] == "queued"
    assert first["participant_turn_id"] != second["participant_turn_id"]
    # Only the first turn has been opened on the seam.
    assert len(fake_seam.calls_named("begin_participant_message")) == 1
    assert wait_for(lambda: manager.queue_depth(SESSION, "claude:default") == 2)

    manager.interrupt(SESSION, "claude:default")
    assert wait_for(lambda: len(fake_seam.calls_named("begin_participant_message")) == 2)

    statuses = [call[4] for call in fake_seam.calls_named("complete_participant_message")]
    assert statuses[0] == "interrupted"


def test_interrupt_without_active_turn_is_reported(make_manager):
    manager = make_manager()
    result = manager.interrupt(SESSION, "claude")
    assert result["ok"] is False
    assert result["status"] == "idle"


def test_adapter_failure_completes_the_row_as_failed(make_manager, fake_seam, tmp_path):
    class ExplodingAdapter(MockAdapter):
        async def start_turn(self, turn: TurnInput) -> None:
            raise AdapterError("claude CLI not found on PATH")

    manager = make_manager(
        build_config(participant("claude"), cwd=tmp_path),
        adapter_factory=lambda p: ExplodingAdapter(participant_id=p.id),
    )
    result = manager.dispatch(SESSION, "claude", "hi", wait=True, timeout=5)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "not found on PATH" in result["error"]

    complete = fake_seam.calls_named("complete_participant_message")[0]
    assert complete[4] == "failed"
    assert "not found on PATH" in complete[6]


def test_failed_durable_finalize_reports_failure_and_suppresses_chain(
    make_manager, fake_seam, tmp_path
):
    """Contract v1.4: a row that could not be persisted is not a success."""

    config = build_config(
        participant("claude", reply_template="done @codex"),
        participant("codex"),
        chain=ChainConfig(enabled=True, turn_cap=5),
        cwd=tmp_path,
    )
    manager = make_manager(config)
    # Both attempts of the bounded retry must fail.
    original = fake_seam.complete_participant_message

    def always_fail(*args, **kwargs):
        raise RuntimeError("database is locked")

    fake_seam.complete_participant_message = always_fail

    result = manager.dispatch(SESSION, "claude", "go", wait=True, timeout=5)
    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "could not be persisted" in result["error"]

    fake_seam.complete_participant_message = original
    time.sleep(0.1)
    # Chain routing was suppressed: codex never got a turn.
    assert not [
        call
        for call in fake_seam.calls_named("begin_participant_message")
        if call[3] == "codex:default"
    ]
    assert list(manager.router.refusals) == []


def test_watchdog_fails_a_wedged_turn_and_frees_the_queue(make_manager, fake_seam, tmp_path):
    """A silent-but-alive child must not wedge the participant's queue forever."""

    config = build_config(participant("claude", hang=True), cwd=tmp_path)
    config = RelayConfig(
        participants=config.participants,
        chain=config.chain,
        tool_timeout_seconds=0.3,  # the watchdog reuses the tool-path bound
        default_cwd=str(tmp_path),
    )
    manager = make_manager(config)
    assert manager.turn_watchdog_seconds == 0.3

    first = manager.dispatch(SESSION, "claude", "wedges")
    second = manager.dispatch(SESSION, "claude", "queued behind it")

    # The wedged turn is finalized as failed, not left streaming.
    assert wait_for(
        lambda: any(
            call[3] == first["participant_turn_id"] and call[4] == "failed"
            for call in fake_seam.calls_named("complete_participant_message")
        ),
        timeout=10,
    )
    failed = [
        call
        for call in fake_seam.calls_named("complete_participant_message")
        if call[3] == first["participant_turn_id"]
    ][0]
    assert failed[4] == "failed"
    assert "timed out" in failed[6]
    # Whatever streamed before the wedge is preserved, not discarded.
    assert failed[5] == "mock reply: wedges"

    # The queue kept moving: the second turn ran on a fresh adapter.
    # begin_participant_message records (name, session, plugin, participant_id,
    # participant_turn_id) — the turn id is index 4.
    assert wait_for(
        lambda: any(
            call[4] == second["participant_turn_id"]
            for call in fake_seam.calls_named("begin_participant_message")
        ),
        timeout=10,
    )
    # Roster is no longer pinned busy by the dead turn.
    assert wait_for(
        lambda: manager.roster(SESSION)[0]["status"] in {"ready", "busy"}, timeout=10
    )


def test_watchdog_does_not_fire_on_healthy_turns(make_manager, fake_seam, tmp_path):
    config = build_config(participant("claude"), cwd=tmp_path)
    manager = make_manager(config)
    result = manager.dispatch(SESSION, "claude", "quick", wait=True, timeout=5)
    assert result["status"] == "completed"
    time.sleep(0.05)
    statuses = [call[4] for call in fake_seam.calls_named("complete_participant_message")]
    assert statuses == ["completed"]


def test_provider_session_id_is_persisted_and_resumed(make_manager, tmp_path):
    store = ProviderSessionStore(tmp_path / "store.json")
    manager = make_manager(build_config(participant("claude"), cwd=tmp_path), store=store)
    manager.dispatch(SESSION, "claude", "one", wait=True, timeout=5)
    assert store.get(SESSION, "claude:default") == "mock-claude:default"
    assert (tmp_path / "store.json").exists()


# ---------------------------------------------------------------------------
# Seam absence (contract section 11)
# ---------------------------------------------------------------------------


def test_roster_reports_error_when_seam_is_missing(tmp_path, seam_absent):
    manager = RelayRuntimeManager(
        build_config(participant("claude"), cwd=tmp_path),
        store=ProviderSessionStore(tmp_path / "store.json"),
    )
    try:
        roster = manager.roster(SESSION)
        assert [entry["status"] for entry in roster] == ["error"]
        assert roster[0]["reason"] == SEAM_MISSING_REASON
        assert manager.seam_available() is False
        with pytest.raises(SeamUnavailableError):
            manager.dispatch(SESSION, "claude", "hi")
    finally:
        manager.shutdown()


def test_roster_uses_seam_module_from_sys_modules(fake_seam, monkeypatch, tmp_path):
    install_seam_module(fake_seam, monkeypatch)
    manager = RelayRuntimeManager(
        build_config(participant("claude"), cwd=tmp_path),
        store=ProviderSessionStore(tmp_path / "store.json"),
    )
    try:
        assert manager.seam_available() is True
        roster = manager.roster(SESSION)
        assert roster[0]["status"] == "ready"
        assert roster[0]["adapter_id"] == "mock"
        assert roster[0]["capabilities"]["streaming"] is True
    finally:
        manager.shutdown()


def test_roster_reports_busy_during_the_begin_write(make_manager, fake_seam, tmp_path):
    """The begin write is synchronous; the participant is busy for its duration.

    Observed from inside the seam call itself, which is exactly the window a
    REST-thread roster read would land in. ``roster``/``queue_depth`` touch no
    loop primitives, so reading them here is safe — and blocking the loop
    instead would also stall the dispatch facade's own result handoff.
    """

    observed = {}
    holder = {}
    original = fake_seam.begin_participant_message

    def recording_begin(*args, **kwargs):
        manager = holder["manager"]
        observed["status"] = manager.roster(SESSION)[0]["status"]
        observed["depth"] = manager.queue_depth(SESSION, "claude:default")
        return original(*args, **kwargs)

    fake_seam.begin_participant_message = recording_begin
    manager = make_manager(build_config(participant("claude"), cwd=tmp_path))
    holder["manager"] = manager

    result = manager.dispatch(SESSION, "claude", "hi", wait=True, timeout=5)
    assert result["status"] == "completed"

    # Claimed before the write, not after it.
    assert observed["status"] == "busy"
    assert observed["depth"] == 1
    # And released once the turn ends.
    assert manager.roster(SESSION)[0]["status"] == "ready"


def test_failed_begin_releases_the_busy_slot(make_manager, fake_seam, tmp_path):
    def rejecting_begin(*args, **kwargs):
        raise RuntimeError("unknown session")

    fake_seam.begin_participant_message = rejecting_begin
    manager = make_manager(build_config(participant("claude"), cwd=tmp_path))
    result = manager.dispatch(SESSION, "claude", "hi", wait=True, timeout=5)

    assert result["ok"] is False
    assert "seam rejected turn" in result["error"]
    # The slot claimed before the write must not leak on the failure branch.
    assert wait_for(lambda: manager.queue_depth(SESSION, "claude:default") == 0)
    assert manager.roster(SESSION)[0]["status"] == "ready"


def test_roster_marks_busy_participant(make_manager, fake_seam, tmp_path):
    manager = make_manager(build_config(participant("claude", hang=True), cwd=tmp_path))
    manager.dispatch(SESSION, "claude", "long")
    assert wait_for(lambda: manager.roster(SESSION)[0]["status"] == "busy")
    assert manager.roster("other-session")[0]["status"] == "ready"


# ---------------------------------------------------------------------------
# Chain safety (contract section 10)
# ---------------------------------------------------------------------------


def test_chain_disabled_by_default_does_not_route(make_manager, fake_seam, tmp_path):
    config = build_config(
        participant("claude", reply_template="over to you @codex"),
        participant("codex"),
        cwd=tmp_path,
    )
    manager = make_manager(config)
    manager.dispatch(SESSION, "claude", "start", wait=True, timeout=5)
    time.sleep(0.1)
    dispatched = {call[3] for call in fake_seam.calls_named("begin_participant_message")}
    assert dispatched == {"claude:default"}


def test_chain_enabled_routes_one_hop(make_manager, fake_seam, tmp_path):
    config = build_config(
        participant("claude", reply_template="over to you @codex"),
        participant("codex", reply_template="codex done"),
        chain=ChainConfig(enabled=True, turn_cap=2),
        cwd=tmp_path,
    )
    manager = make_manager(config)
    manager.dispatch(SESSION, "claude", "start", wait=True, timeout=5)
    assert wait_for(
        lambda: any(
            call[3] == "codex:default"
            for call in fake_seam.calls_named("begin_participant_message")
        )
    )
    assert wait_for(lambda: len(fake_seam.calls_named("complete_participant_message")) == 2)
    assert list(manager.router.refusals) == []


def test_chain_cap_refuses_and_records(make_manager, fake_seam, tmp_path):
    config = build_config(
        participant("claude", reply_template="ping @codex"),
        participant("codex", reply_template="pong @claude"),
        chain=ChainConfig(enabled=True, turn_cap=1),
        cwd=tmp_path,
    )
    manager = make_manager(config)
    manager.dispatch(SESSION, "claude", "start", wait=True, timeout=5)
    assert wait_for(lambda: len(manager.router.refusals) == 1)
    refusal = manager.router.refusals[0]
    assert refusal["target_handle"] == "claude"
    assert refusal["source_participant_id"] == "codex:default"
    assert "turn cap" in refusal["reason"]
    time.sleep(0.05)
    begins = [call[3] for call in fake_seam.calls_named("begin_participant_message")]
    assert begins == ["claude:default", "codex:default"]


def test_chain_ignores_self_mention(make_manager, fake_seam, tmp_path):
    config = build_config(
        participant("claude", reply_template="talking to myself @claude"),
        participant("codex"),
        chain=ChainConfig(enabled=True, turn_cap=5),
        cwd=tmp_path,
    )
    manager = make_manager(config)
    manager.dispatch(SESSION, "claude", "start", wait=True, timeout=5)
    time.sleep(0.1)
    begins = [call[3] for call in fake_seam.calls_named("begin_participant_message")]
    assert begins == ["claude:default"]
    assert list(manager.router.refusals) == []


def test_hermes_mention_is_not_forwarded_in_slice_one(make_manager, fake_seam, tmp_path, caplog):
    config = build_config(
        participant("claude", reply_template="@hermes please look"),
        chain=ChainConfig(enabled=True, turn_cap=5),
        cwd=tmp_path,
    )
    manager = make_manager(config)
    manager.dispatch(SESSION, "claude", "start", wait=True, timeout=5)
    time.sleep(0.1)
    begins = [call[3] for call in fake_seam.calls_named("begin_participant_message")]
    assert begins == ["claude:default"]


# ---------------------------------------------------------------------------
# Exactly-once dispatch (contract sections 6 v1.3 / v1.4)
# ---------------------------------------------------------------------------


class SlowStartManager(RelayRuntimeManager):
    """Suspends inside the accepted region so duplicates observe an in-flight entry."""

    async def _start_turn(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await super()._start_turn(*args, **kwargs)


def test_concurrent_duplicate_dispatch_fans_out_once(make_manager, fake_seam):
    manager = make_manager(cls=SlowStartManager)
    results = {}
    barrier = threading.Barrier(2)

    def call(slot: str) -> None:
        barrier.wait()
        results[slot] = manager.dispatch_group(
            SESSION, "same-dispatch", "@claude @codex go", ["claude", "codex"]
        )

    threads = [threading.Thread(target=call, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert results["a"] == results["b"]
    assert len(results["a"]["turns"]) == 2
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1
    assert wait_for(lambda: len(fake_seam.calls_named("begin_participant_message")) == 2)
    begins = sorted(call[3] for call in fake_seam.calls_named("begin_participant_message"))
    assert begins == ["claude:default", "codex:default"]


def test_retry_after_response_loss_returns_identical_turn_refs(make_manager, fake_seam):
    manager = make_manager()
    first = manager.dispatch_group(SESSION, "d-retry", "@claude go", ["claude"])
    second = manager.dispatch_group(SESSION, "d-retry", "@claude go", ["claude"])

    assert first == second
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1
    assert wait_for(lambda: len(fake_seam.calls_named("begin_participant_message")) == 1)


def test_different_dispatch_ids_are_independent(make_manager, fake_seam):
    manager = make_manager()
    first = manager.dispatch_group(SESSION, "d-1", "@claude go", ["claude"])
    second = manager.dispatch_group(SESSION, "d-2", "@claude go", ["claude"])
    assert first["turns"][0]["participant_turn_id"] != second["turns"][0]["participant_turn_id"]
    assert len(fake_seam.calls_named("append_participant_user_message")) == 2


def test_same_dispatch_id_in_another_session_is_independent(make_manager, fake_seam):
    manager = make_manager()
    first = manager.dispatch_group(SESSION, "d-1", "@claude go", ["claude"])
    second = manager.dispatch_group("gw-session-2", "d-1", "@claude go", ["claude"])
    assert first["turns"][0]["participant_turn_id"] != second["turns"][0]["participant_turn_id"]


def test_pre_acceptance_failure_is_not_memoized(make_manager, fake_seam):
    manager = make_manager()
    with pytest.raises(ParticipantNotFoundError):
        manager.dispatch_group(SESSION, "d-1", "@nobody", ["nobody"])
    assert len(manager.idempotency) == 0
    # The same dispatch_id may be retried with a corrected payload.
    result = manager.dispatch_group(SESSION, "d-1", "@claude go", ["claude"])
    assert result["ok"] is True


def test_missing_dispatch_id_is_rejected(make_manager):
    from hermes_plugin_relay.runtime.manager import DispatchValidationError

    manager = make_manager()
    with pytest.raises(DispatchValidationError):
        manager.dispatch_group(SESSION, "", "@claude go", ["claude"])
    with pytest.raises(DispatchValidationError):
        manager.dispatch_group("", "d-1", "@claude go", ["claude"])


class ExplodingAfterUserRowManager(RelayRuntimeManager):
    """Injects a failure strictly after the first side effect."""

    async def _do_dispatch_group(self, session_id, text, mentions, append_user_message, accepted):
        self._ensure_registered(session_id)
        self.seam().append_participant_user_message(
            session_id, self.plugin_id, text, list(mentions)
        )
        # side_effect is derived from this, so nothing to hand-sync.
        accepted.user_row_appended = True
        raise RuntimeError("exploded after the user row")


def test_failure_after_user_row_is_committed_and_replayed(make_manager, fake_seam):
    manager = make_manager(cls=ExplodingAfterUserRowManager)
    first = manager.dispatch_group(SESSION, "d-boom", "@claude go", ["claude"])

    assert first["ok"] is False
    assert first["user_row_appended"] is True
    assert first["turns"] == []
    assert "exploded after the user row" in first["error"]

    second = manager.dispatch_group(SESSION, "d-boom", "@claude go", ["claude"])
    assert second == first
    # Exactly one user row despite the retry.
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1


class PartialFailManager(RelayRuntimeManager):
    fail_for = "codex:default"

    async def _start_turn(self, session_id, participant, *args, **kwargs):
        if participant.id == self.fail_for:
            raise RuntimeError("could not queue codex")
        return await super()._start_turn(session_id, participant, *args, **kwargs)


def test_partial_group_failure_is_memoized(make_manager, fake_seam):
    manager = make_manager(cls=PartialFailManager)
    first = manager.dispatch_group(SESSION, "d-partial", "@claude @codex go", ["claude", "codex"])

    assert first["ok"] is False
    assert first["user_row_appended"] is True
    assert [t["participant_id"] for t in first["turns"]] == ["claude:default"]
    assert first["failed"] == [
        {"participant_id": "codex:default", "error": "could not queue codex"}
    ]

    second = manager.dispatch_group(SESSION, "d-partial", "@claude @codex go", ["claude", "codex"])
    assert second == first
    assert len(fake_seam.calls_named("append_participant_user_message")) == 1
    assert wait_for(lambda: len(fake_seam.calls_named("begin_participant_message")) == 1)


def test_capacity_pressure_fails_closed_without_evicting_live_entries(make_manager, fake_seam):
    manager = make_manager(cls=SlowStartManager)
    manager.idempotency.max_entries = 2

    errors = []
    started = threading.Barrier(3)

    def fire(dispatch_id: str) -> None:
        started.wait()
        try:
            manager.dispatch_group(SESSION, dispatch_id, "@claude go", ["claude"])
        except Exception as exc:  # noqa: BLE001
            errors.append((dispatch_id, exc))

    threads = [threading.Thread(target=fire, args=(f"d-{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(errors) == 1
    _, exc = errors[0]
    assert isinstance(exc, DispatchCapacityError)
    assert "capacity" in str(exc)
    # The two live entries survived the pressure.
    assert len(manager.idempotency) == 2
    assert len(fake_seam.calls_named("append_participant_user_message")) == 2


def test_idle_workers_are_reaped_so_child_processes_stay_bounded(
    make_manager, fake_seam, tmp_path, monkeypatch
):
    monkeypatch.setattr("hermes_plugin_relay.runtime.manager.MAX_LIVE_WORKERS", 3)
    manager = make_manager(build_config(participant("claude"), cwd=tmp_path))

    for index in range(6):
        manager.dispatch(f"session-{index}", "claude", "hi", wait=True, timeout=5)

    assert wait_for(lambda: len(manager._workers) <= 3)
    assert len(manager._workers) <= 3


def test_busy_workers_are_never_reaped(make_manager, fake_seam, tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.runtime.manager.MAX_LIVE_WORKERS", 2)
    manager = make_manager(build_config(participant("claude", hang=True), cwd=tmp_path))

    manager.dispatch("session-busy", "claude", "stall")
    assert wait_for(lambda: manager.roster("session-busy")[0]["status"] == "busy")

    for index in range(4):
        manager.dispatch(f"session-{index}", "claude", "also stalls")

    time.sleep(0.1)
    assert ("session-busy", "claude:default") in manager._workers
    # Every worker here is busy, so the cap cannot be honored without killing a
    # live turn: the cap yields rather than interrupting anyone.
    assert len(manager._workers) == 5


def test_registered_session_cache_is_bounded(make_manager, fake_seam, monkeypatch):
    monkeypatch.setattr("hermes_plugin_relay.runtime.manager.MAX_REGISTERED_SESSIONS", 3)
    manager = make_manager()
    for index in range(8):
        manager.dispatch(f"s-{index}", "claude", "hi", wait=True, timeout=5)
    assert len(manager._registered_sessions) <= 3
    # Re-registering after a forget is harmless (idempotent upsert on the seam).
    assert len(fake_seam.calls_named("register_participants")) >= 8


def test_ttl_eviction_lets_an_old_dispatch_id_run_again(make_manager, fake_seam):
    clock = {"now": 1000.0}
    manager = make_manager(clock=lambda: clock["now"])

    first = manager.dispatch_group(SESSION, "d-old", "@claude go", ["claude"])
    assert len(manager.idempotency) == 1

    # Still inside the TTL: replayed.
    replay = manager.dispatch_group(SESSION, "d-old", "@claude go", ["claude"])
    assert replay == first

    clock["now"] += manager.idempotency.ttl_seconds + 1
    fresh = manager.dispatch_group(SESSION, "d-old", "@claude go", ["claude"])
    assert fresh["turns"][0]["participant_turn_id"] != first["turns"][0]["participant_turn_id"]
    assert len(fake_seam.calls_named("append_participant_user_message")) == 2
