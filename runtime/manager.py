"""The single dispatch path for every participant turn.

Human submits (REST ``/dispatch``), Hermes tool calls (``agent_message``) and
chained participant-to-participant hops all funnel through one
:class:`RelayRuntimeManager`. There is deliberately no second adapter/session
implementation anywhere in this plugin.

Threading model
---------------
The manager owns a dedicated background thread running its own asyncio loop, so
it behaves identically under the gateway (which has a loop), the dashboard
threadpool (which does not) and a plain CLI tool call. Every public method is a
synchronous facade that hands work to that loop.

Because all state mutation happens on that one loop thread, the exactly-once
idempotency reservation for ``/dispatch`` is atomic for free: ``lookup`` and
``reserve`` run with no ``await`` between them.

Seam coupling
-------------
``tui_gateway.participants`` is imported lazily on every use. When it is absent
(contract section 11) the roster reports ``error`` / "hermes core seam missing",
tools and REST return a visible error, and plugin registration still succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..adapters import (
    Availability,
    LineProcessAdapter,
    TurnInput,
    adapter_availability,
    adapter_capabilities,
)
from ..adapters import build_adapter as _build_adapter
from ..config import PLUGIN_ID, ParticipantConfig, RelayConfig, load_config, resolve_cwd
from .events import MessageDelta, SessionUpdated, TurnCompleted, TurnStarted
from .persistence import ProviderSessionStore
from .router import ChainRouter

logger = logging.getLogger(__name__)

#: How long a ``(session_id, dispatch_id)`` reservation stays de-duplicating.
IDEMPOTENCY_TTL_SECONDS = 600.0

#: Hard cap on tracked dispatch ids; oldest are evicted first.
IDEMPOTENCY_MAX_ENTRIES = 512

#: Safety margin added to the sync facade's wait over the async timeout.
FACADE_TIMEOUT_MARGIN_SECONDS = 15.0

#: How long ``shutdown()`` waits for adapters to close.
SHUTDOWN_TIMEOUT_SECONDS = 10.0

#: Cap on live (session, participant) workers. Each can own a provider child
#: process, and the gateway runs for days, so idle ones are reaped LRU-first
#: rather than accumulating one subprocess per session forever.
MAX_LIVE_WORKERS = 64

#: Cap on remembered "already registered with the seam" sessions. Registration
#: is an idempotent upsert, so forgetting one only costs a re-register.
MAX_REGISTERED_SESSIONS = 512

SEAM_MISSING_REASON = "hermes core seam missing"


class RelayRuntimeError(RuntimeError):
    """Base class for manager-visible failures.

    Each subclass carries its own HTTP classification so the REST layer never
    has to re-derive it from the exception type.

    ``pre_acceptance`` is the load-bearing half. The Desktop composer middleware
    is allowed to fall back to passing the draft on to Hermes when ``/dispatch``
    answers 4xx, which is only safe if a 4xx really means "nothing happened".
    An UNCLASSIFIED failure is ambiguous — it may have appended the user row
    already — so the default here is 500, and the REST layer must never
    downgrade an unknown exception to 4xx.
    """

    http_status = 500
    pre_acceptance = False


class SeamUnavailableError(RelayRuntimeError):
    """``tui_gateway.participants`` is not importable in this process."""

    # 424 Failed Dependency: the request failed because a dependency did.
    http_status = 424
    pre_acceptance = True


class ParticipantNotFoundError(RelayRuntimeError):
    """No enabled participant matches the requested id/handle."""

    http_status = 400
    pre_acceptance = True


class DispatchValidationError(RelayRuntimeError):
    """The request was rejected before any side effect (pre-acceptance)."""

    http_status = 400
    pre_acceptance = True


class DispatchCapacityError(RelayRuntimeError):
    """The idempotency map is full of live entries; fail closed rather than evict."""

    # 429: retryable, and nothing was accepted.
    http_status = 429
    pre_acceptance = True


def load_seam() -> Any:
    """Import the Hermes core participant publisher, lazily and every time.

    Not cached on purpose: the module must be re-resolved so a process that
    gains (or, in tests, swaps) the seam sees it immediately.
    """

    try:
        import tui_gateway.participants as seam  # type: ignore
    except Exception as exc:  # noqa: BLE001 - ImportError and broken-module errors alike
        raise SeamUnavailableError(SEAM_MISSING_REASON) from exc
    return seam


# ---------------------------------------------------------------------------
# Exactly-once dispatch bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class _IdempotencyEntry:
    """One ``(session_id, dispatch_id)`` reservation.

    Lifecycle: reserved (in-flight) -> either *committed* (a side effect landed,
    so the deterministic result is memoized and replayed to every duplicate) or
    *discarded* (pre-acceptance failure: nothing happened, a retry may
    re-validate from scratch).
    """

    future: "asyncio.Future[Dict[str, Any]]"
    settled_at: Optional[float] = None

    def __post_init__(self) -> None:
        # Mark exceptions retrieved so a duplicate nobody waits on stays quiet.
        self.future.add_done_callback(lambda fut: fut.cancelled() or fut.exception())

    @property
    def in_flight(self) -> bool:
        return not self.future.done()


class _IdempotencyCache:
    """Bounded TTL map keyed ``(session_id, dispatch_id)``.

    Only ever touched from the manager's loop thread, so ``lookup``/``reserve``
    are atomic with respect to each other — two simultaneous identical POSTs
    cannot both fan out.

    Eviction rules (contract v1.4): in-flight and committed-unexpired entries are
    NEVER evicted under size pressure, because dropping one would let a retry
    duplicate an already-persisted user row or participant fanout. Only
    settled+expired entries are reclaimed; a full map of live entries fails
    closed with a visible capacity error.
    """

    def __init__(self, *, clock: Optional[Callable[[], float]] = None) -> None:
        self.ttl_seconds = IDEMPOTENCY_TTL_SECONDS
        self.max_entries = IDEMPOTENCY_MAX_ENTRIES
        self._clock = clock or time.monotonic
        self._entries: "Dict[Tuple[str, str], _IdempotencyEntry]" = {}

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def in_flight_count(self) -> int:
        return sum(1 for entry in self._entries.values() if entry.in_flight)

    def purge(self) -> None:
        """Reclaim settled entries whose TTL has elapsed. Never touches live ones."""

        now = self._clock()
        expired = [
            key
            for key, entry in self._entries.items()
            if entry.settled_at is not None and now - entry.settled_at > self.ttl_seconds
        ]
        for key in expired:
            self._entries.pop(key, None)

    def lookup(self, key: Tuple[str, str]) -> Optional[_IdempotencyEntry]:
        self.purge()
        return self._entries.get(key)

    def reserve(self, key: Tuple[str, str], loop: asyncio.AbstractEventLoop) -> _IdempotencyEntry:
        self.purge()
        if len(self._entries) >= self.max_entries:
            raise DispatchCapacityError(
                f"dispatch capacity reached ({len(self._entries)} live dispatch ids, "
                f"{self.in_flight_count} in flight); retry shortly"
            )
        entry = _IdempotencyEntry(future=loop.create_future())
        self._entries[key] = entry
        return entry

    def commit(self, key: Tuple[str, str], payload: Dict[str, Any]) -> None:
        """Memoize a result whose side effects already landed. Never evicted early."""

        entry = self._entries.get(key)
        if entry is None:  # pragma: no cover - reserve always precedes commit
            return
        entry.settled_at = self._clock()
        if not entry.future.done():
            entry.future.set_result(payload)

    def discard(self, key: Tuple[str, str], exc: Optional[BaseException] = None) -> None:
        """Drop a pre-acceptance reservation so a retry can re-validate fresh."""

        entry = self._entries.pop(key, None)
        if entry is not None and exc is not None and not entry.future.done():
            entry.future.set_exception(exc)


# ---------------------------------------------------------------------------
# Turn + worker
# ---------------------------------------------------------------------------


def _idle_payload(participant_id: str) -> Dict[str, Any]:
    """The "nothing to interrupt" answer, shared by both interrupt entry points."""

    return {
        "ok": False,
        "participant_id": participant_id,
        "status": "idle",
        "error": "no active turn",
    }


@dataclass
class _GroupAcceptance:
    """Tracks whether a ``/dispatch`` fanout has produced any side effect yet.

    The response shape is stable and additive to contract section 6's success
    example, so a partial failure is machine-readable rather than an opaque
    error:

    ``{"ok", "turns": [...], "failed": [{"participant_id", "error"}], "user_row_appended"}``
    """

    user_row_appended: bool = False
    turns: List[Dict[str, str]] = field(default_factory=list)
    failed: List[Dict[str, str]] = field(default_factory=list)

    @property
    def side_effect(self) -> bool:
        """True once anything durable happened. Derived, never hand-synced."""

        return self.user_row_appended or bool(self.turns)

    def payload(self) -> Dict[str, Any]:
        return {
            "ok": not self.failed,
            "turns": list(self.turns),
            "failed": list(self.failed),
            "user_row_appended": self.user_row_appended,
        }

    def partial_payload(self, error: str) -> Dict[str, Any]:
        payload = self.payload()
        payload["ok"] = False
        payload["error"] = error
        return payload


@dataclass
class _Turn:
    session_id: str
    participant: ParticipantConfig
    participant_turn_id: str
    text: str
    chain_depth: int
    result: "asyncio.Future[Dict[str, Any]]"
    #: Resolved once when the turn opens. Every publish for this turn then uses
    #: it instead of re-importing the seam module per streamed token; a hot-swap
    #: still takes effect at the next turn boundary.
    seam: Any = None
    row_id: Optional[int] = None
    completed: bool = False
    #: Everything published as a delta, so a watchdog timeout can still report
    #: what actually streamed instead of discarding it.
    text_parts: List[str] = field(default_factory=list)


class _ParticipantWorker:
    """Serial turn queue and adapter owner for one (session, participant)."""

    def __init__(
        self,
        manager: "RelayRuntimeManager",
        session_id: str,
        participant: ParticipantConfig,
    ) -> None:
        self._manager = manager
        self.session_id = session_id
        self.participant = participant
        self._queue: "asyncio.Queue[_Turn]" = asyncio.Queue()
        self._adapter: Optional[LineProcessAdapter] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._active: Optional[_Turn] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self.last_used = time.monotonic()

    # -- queue ------------------------------------------------------------------

    @property
    def active_turn_id(self) -> Optional[str]:
        turn = self._active
        return turn.participant_turn_id if turn is not None else None

    @property
    def depth(self) -> int:
        return self._queue.qsize() + (1 if self._active is not None else 0)

    @property
    def idle(self) -> bool:
        return self._active is None and self._queue.empty()

    def touch(self) -> None:
        self.last_used = time.monotonic()

    async def enqueue(self, turn: _Turn) -> None:
        await self._queue.put(turn)
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        while not self._closing:
            turn = await self._queue.get()
            try:
                await self._execute(turn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("relay: participant turn crashed")
                self._complete(turn, "failed", "", str(exc))
            finally:
                self._queue.task_done()

    # -- execution --------------------------------------------------------------

    def _ensure_adapter(self) -> LineProcessAdapter:
        if self._adapter is None:
            adapter = self._manager.build_adapter(self.participant)
            self._unsubscribe = adapter.subscribe(self._on_event)
            self._adapter = adapter
        return self._adapter

    async def _execute(self, turn: _Turn) -> None:
        manager = self._manager
        try:
            seam = turn.seam = manager.seam()
        except SeamUnavailableError as exc:
            self._resolve(turn, self._failure_payload(turn, str(exc)))
            return

        # Claim the slot BEFORE the seam write. That write is synchronous and
        # can take a while (SQLite), and roster()/queue_depth() are read from
        # the REST thread meanwhile: leaving `_active` unset would report the
        # participant `ready` when it is already committed to this turn.
        self._active = turn
        try:
            turn.row_id = seam.begin_participant_message(
                turn.session_id,
                manager.plugin_id,
                turn.participant.id,
                turn.participant_turn_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._active = None
            logger.warning("relay: begin_participant_message failed: %s", exc)
            self._resolve(turn, self._failure_payload(turn, f"seam rejected turn: {exc}"))
            return


        try:
            adapter = self._ensure_adapter()
            resume_id = manager.store.get(self.session_id, self.participant.id)
            await adapter.start_turn(
                TurnInput(
                    text=turn.text,
                    cwd=manager.participant_cwd(self.participant, self.session_id),
                    participant_turn_id=turn.participant_turn_id,
                    resume_session_id=resume_id,
                )
            )
        except Exception as exc:  # noqa: BLE001 - spawn/auth failures must be visible
            logger.warning(
                "relay: adapter start_turn failed for %s: %s", self.participant.id, exc
            )
            self._complete(turn, "failed", "", str(exc))
            return

        # Watchdog: without it a silent-but-alive child wedges this
        # participant's serial queue forever, leaves a `streaming` row in the
        # transcript, and pins the roster to `busy`. The wait=True tool path has
        # its own timeout; this covers every non-waiting dispatch (composer
        # submits and chained hops).
        timeout = self._manager.turn_watchdog_seconds
        try:
            await asyncio.wait_for(asyncio.shield(turn.result), timeout=timeout)
        except asyncio.TimeoutError:
            await self._force_timeout(turn, timeout)

    async def _force_timeout(self, turn: _Turn, timeout: float) -> None:
        """Finalize a wedged turn as failed and recycle the adapter."""

        logger.warning(
            "relay: participant turn %s exceeded %ss; failing it and recycling the adapter",
            turn.participant_turn_id,
            timeout,
        )
        # Finalize FIRST: any terminal event the adapter emits while being torn
        # down then lands on an already-completed turn and is ignored, so the
        # row cannot end up labelled `interrupted` by our own cleanup.
        self._complete(
            turn,
            "failed",
            "".join(turn.text_parts),
            f"participant turn timed out after {timeout}s with no response",
        )
        try:
            adapter = self._adapter
            if adapter is not None:
                await adapter.interrupt()
        except Exception:  # noqa: BLE001 - best effort before the harder teardown
            logger.warning("relay: timeout interrupt failed", exc_info=True)
        # The child proved unresponsive, so do not keep it warm: a stale active
        # turn inside the adapter would reject every queued turn behind it.
        await self._recycle_adapter()

    async def _recycle_adapter(self) -> None:
        adapter, self._adapter = self._adapter, None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if adapter is None:
            return
        try:
            await adapter.close()
        except Exception:  # noqa: BLE001
            logger.warning("relay: adapter close failed during recycle", exc_info=True)

    # -- adapter events ---------------------------------------------------------

    def _on_event(self, event: Any) -> None:
        if isinstance(event, SessionUpdated):
            try:
                self._manager.store.set(
                    self.session_id, self.participant.id, event.provider_session_id
                )
            except Exception:  # noqa: BLE001
                logger.warning("relay: failed persisting provider session id", exc_info=True)
            return

        turn = self._active
        if turn is None:
            return

        if isinstance(event, TurnStarted):
            return
        if isinstance(event, MessageDelta):
            self._publish_delta(turn, event.text)
            return
        if isinstance(event, TurnCompleted):
            self._complete(turn, event.status, event.text, event.error)

    def _turn_seam(self, turn: _Turn) -> Any:
        """The seam this turn opened with, or a fresh resolve if it never did."""

        return turn.seam if turn.seam is not None else self._manager.seam()

    def _publish_delta(self, turn: _Turn, text: str) -> None:
        if not text:
            return
        turn.text_parts.append(text)
        try:
            self._turn_seam(turn).append_participant_delta(
                turn.session_id, self._manager.plugin_id, turn.participant_turn_id, text
            )
        except Exception:  # noqa: BLE001 - a delta must never break the stream
            logger.warning("relay: append_participant_delta failed", exc_info=True)

    def _complete(
        self,
        turn: _Turn,
        status: str,
        text: str,
        error: Optional[str] = None,
    ) -> None:
        if turn.completed:
            return
        turn.completed = True
        if self._active is turn:
            self._active = None

        finalize_error = self._finalize_durably(turn, status, text, error)

        if finalize_error is not None:
            # The canonical attributed row would be left streaming/empty, so the
            # turn is NOT a success from the caller's point of view, and its text
            # must not drive chain routing (contract v1.4).
            payload = {
                "ok": False,
                "participant_id": turn.participant.id,
                "participant_turn_id": turn.participant_turn_id,
                "status": "failed",
                "text": text,
                "error": (
                    "participant reply could not be persisted to the transcript: "
                    f"{finalize_error}"
                ),
            }
            self._resolve(turn, payload)
            return

        payload = {
            "ok": status == "completed",
            "participant_id": turn.participant.id,
            "participant_turn_id": turn.participant_turn_id,
            "status": status,
            "text": text,
        }
        if error:
            payload["error"] = error
        self._resolve(turn, payload)
        self._manager.schedule_chain_routing(turn, status, text)

    def _finalize_durably(
        self,
        turn: _Turn,
        status: str,
        text: str,
        error: Optional[str],
    ) -> Optional[Exception]:
        """Persist the terminal row state. Returns the failure, or ``None``.

        One bounded retry, because the common failure is a transient SQLite
        write lock rather than a validation error.
        """

        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                self._turn_seam(turn).complete_participant_message(
                    turn.session_id,
                    self._manager.plugin_id,
                    turn.participant_turn_id,
                    status=status,
                    text=text,
                    error=error,
                )
                return None
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "relay: complete_participant_message failed (attempt %d): %s",
                    attempt + 1,
                    exc,
                )
        return last_exc

    def _failure_payload(self, turn: _Turn, error: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "participant_id": turn.participant.id,
            "participant_turn_id": turn.participant_turn_id,
            "status": "failed",
            "text": "",
            "error": error,
        }

    @staticmethod
    def _resolve(turn: _Turn, payload: Dict[str, Any]) -> None:
        if not turn.result.done():
            turn.result.set_result(payload)

    # -- control ----------------------------------------------------------------

    async def interrupt(self) -> Dict[str, Any]:
        turn = self._active
        adapter = self._adapter
        if turn is None or adapter is None:
            return _idle_payload(self.participant.id)
        turn_id = turn.participant_turn_id
        try:
            await adapter.interrupt()
        except Exception as exc:  # noqa: BLE001 - best effort by contract
            logger.warning("relay: interrupt failed for %s: %s", self.participant.id, exc)
            return {
                "ok": False,
                "participant_id": self.participant.id,
                "participant_turn_id": turn_id,
                "status": "error",
                "error": str(exc),
            }
        return {
            "ok": True,
            "participant_id": self.participant.id,
            "participant_turn_id": turn_id,
            "status": "interrupt_requested",
        }

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._adapter is not None:
            try:
                await self._adapter.close()
            except Exception:  # noqa: BLE001
                logger.warning("relay: adapter close failed", exc_info=True)
            self._adapter = None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

AdapterFactory = Callable[[ParticipantConfig], LineProcessAdapter]


class RelayRuntimeManager:
    """Owns participant workers, the seam publishing calls and chain routing."""

    def __init__(
        self,
        config: Optional[RelayConfig] = None,
        *,
        seam: Any = None,
        adapter_factory: Optional[AdapterFactory] = None,
        store: Optional[ProviderSessionStore] = None,
        clock: Optional[Callable[[], float]] = None,
        session_cwd_resolver: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
        self.config = config or load_config()
        self.plugin_id = self.config.plugin_id or PLUGIN_ID
        self._injected_seam = seam
        self._adapter_factory = adapter_factory
        self.store = store or ProviderSessionStore.default()
        self._clock = clock or time.monotonic
        self._session_cwd_resolver = session_cwd_resolver

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lifecycle_lock = threading.Lock()
        self._workers: Dict[Tuple[str, str], _ParticipantWorker] = {}
        self._registered_sessions: set = set()
        self._idempotency = _IdempotencyCache(clock=self._clock)
        self.router = ChainRouter(
            chain=self.config.chain,
            roster_handles=lambda: list(self.config.handles),
            dispatch=self._dispatch_from_router,
        )

    # -- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._loop is not None:
                return
            ready = threading.Event()

            def _run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:  # noqa: BLE001
                        pass
                    loop.close()

            thread = threading.Thread(target=_run, name="relay-participants", daemon=True)
            self._thread = thread
            thread.start()
            if not ready.wait(timeout=10.0):
                raise RelayRuntimeError("relay runtime loop failed to start")

    def shutdown(self) -> None:
        with self._lifecycle_lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
        if loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._close_all(), loop)
            future.result(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("relay: shutdown did not close cleanly", exc_info=True)
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if thread is not None:
            thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)

    async def _close_all(self) -> None:
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            await worker.close()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self.start()
        loop = self._loop
        if loop is None:  # pragma: no cover - start() raises first
            raise RelayRuntimeError("relay runtime loop is not running")
        return loop

    def _submit(self, coro: Any, timeout: Optional[float] = None) -> Any:
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    # -- seam -------------------------------------------------------------------

    def seam(self) -> Any:
        if self._injected_seam is not None:
            return self._injected_seam
        return load_seam()

    def seam_available(self) -> bool:
        try:
            self.seam()
        except SeamUnavailableError:
            return False
        return True

    # -- roster -----------------------------------------------------------------

    def participant_status(
        self,
        participant: ParticipantConfig,
        session_id: Optional[str],
        *,
        seam_ok: Optional[bool] = None,
        availability: Optional[Availability] = None,
    ) -> Tuple[str, Optional[str]]:
        """Honest status for one participant.

        ``seam_ok`` / ``availability`` let :meth:`roster` hoist the two
        expensive probes out of the loop; both are resolved here when omitted.
        """

        if seam_ok is None:
            seam_ok = self.seam_available()
        if not seam_ok:
            return "error", SEAM_MISSING_REASON
        if availability is None:
            availability = adapter_availability(participant.adapter)
        if availability.status != "ready":
            return availability.status, availability.reason
        if session_id and self.queue_depth(session_id, participant.id) > 0:
            return "busy", None
        return "ready", None

    def roster(self, session_id: Optional[str] = None) -> List[Dict[str, Any]]:
        # Both probes walk PATH (and, for codex, stat a file). Resolve the seam
        # once and each adapter kind once per call instead of once per
        # participant — the roster is polled by the composer's @-menu.
        seam_ok = self.seam_available()
        probed: Dict[str, Availability] = {}

        entries: List[Dict[str, Any]] = []
        for participant in self.config.enabled_participants:
            availability = probed.get(participant.adapter)
            if availability is None:
                availability = probed[participant.adapter] = adapter_availability(
                    participant.adapter
                )
            status, reason = self.participant_status(
                participant, session_id, seam_ok=seam_ok, availability=availability
            )
            entry: Dict[str, Any] = {
                "id": participant.id,
                "handle": participant.handle,
                "display_name": participant.display_name,
                "adapter_id": participant.adapter_id,
                "status": status,
                "capabilities": adapter_capabilities(participant.adapter),
            }
            if reason:
                entry["reason"] = reason
            entries.append(entry)
        return entries

    # -- worker plumbing --------------------------------------------------------

    def build_adapter(self, participant: ParticipantConfig) -> LineProcessAdapter:
        if self._adapter_factory is not None:
            return self._adapter_factory(participant)
        options = dict(participant.options)
        if participant.adapter == "mock":
            options.setdefault("handle", participant.handle)
        return _build_adapter(
            participant.adapter,
            participant_id=participant.id,
            model=participant.model,
            **options,
        )

    @property
    def turn_watchdog_seconds(self) -> float:
        """Per-turn ceiling on the worker path. Shares the tool-path bound."""

        return float(self.config.tool_timeout_seconds)

    def participant_cwd(self, participant: ParticipantConfig, session_id: str) -> str:
        session_cwd = None
        if self._session_cwd_resolver is not None:
            try:
                session_cwd = self._session_cwd_resolver(session_id)
            except Exception:  # noqa: BLE001
                session_cwd = None
        return resolve_cwd(participant, self.config, session_cwd)

    def _worker(self, session_id: str, participant: ParticipantConfig) -> _ParticipantWorker:
        key = (session_id, participant.id)
        worker = self._workers.get(key)
        if worker is None:
            self._reap_idle_workers()
            worker = _ParticipantWorker(self, session_id, participant)
            self._workers[key] = worker
        worker.touch()
        return worker

    def _reap_idle_workers(self) -> None:
        """Close least-recently-used idle workers so child processes stay bounded.

        A busy worker is never reaped: closing it would kill a turn mid-stream.
        """

        overflow = len(self._workers) - MAX_LIVE_WORKERS + 1
        if overflow <= 0:
            return
        idle = sorted(
            (worker for worker in self._workers.values() if worker.idle),
            key=lambda worker: worker.last_used,
        )
        for worker in idle[:overflow]:
            self._workers.pop((worker.session_id, worker.participant.id), None)
            logger.debug(
                "relay: reaping idle worker %s/%s", worker.session_id, worker.participant.id
            )
            asyncio.ensure_future(worker.close())

    def _ensure_registered(self, session_id: str) -> None:
        if session_id in self._registered_sessions:
            return
        seam = self.seam()
        seam.register_participants(session_id, self.plugin_id, self.roster(session_id))
        if len(self._registered_sessions) >= MAX_REGISTERED_SESSIONS:
            self._registered_sessions.clear()
        self._registered_sessions.add(session_id)

    def _resolve_participant(self, reference: str) -> ParticipantConfig:
        participant = self.config.resolve(reference)
        if participant is None or not participant.enabled:
            known = ", ".join(f"@{h}" for h in self.config.handles) or "(none configured)"
            raise ParticipantNotFoundError(
                f"unknown participant {reference!r}; known participants: {known}"
            )
        return participant

    # -- dispatch (async core) --------------------------------------------------

    async def _start_turn(
        self,
        session_id: str,
        participant: ParticipantConfig,
        text: str,
        *,
        chain_depth: int,
    ) -> _Turn:
        loop = asyncio.get_running_loop()
        turn = _Turn(
            session_id=session_id,
            participant=participant,
            participant_turn_id=f"pturn-{uuid.uuid4()}",
            text=text,
            chain_depth=chain_depth,
            result=loop.create_future(),
        )
        await self._worker(session_id, participant).enqueue(turn)
        return turn

    async def _dispatch_async(
        self,
        session_id: str,
        reference: str,
        text: str,
        *,
        append_user_message: bool,
        chain_depth: int,
        mentions: Optional[Sequence[str]],
        wait: bool,
        timeout: Optional[float],
    ) -> Dict[str, Any]:
        participant = self._resolve_participant(reference)
        self._ensure_registered(session_id)

        if append_user_message:
            self.seam().append_participant_user_message(
                session_id,
                self.plugin_id,
                text,
                list(mentions) if mentions else [participant.handle],
            )

        turn = await self._start_turn(session_id, participant, text, chain_depth=chain_depth)
        if not wait:
            return {
                "ok": True,
                "participant_id": participant.id,
                "participant_turn_id": turn.participant_turn_id,
                "status": "queued",
            }
        return await self._await_turn(turn, timeout)

    async def _await_turn(self, turn: _Turn, timeout: Optional[float]) -> Dict[str, Any]:
        try:
            return await asyncio.wait_for(asyncio.shield(turn.result), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "relay: turn %s timed out after %ss; requesting interrupt",
                turn.participant_turn_id,
                timeout,
            )
            worker = self._workers.get((turn.session_id, turn.participant.id))
            if worker is not None and worker.active_turn_id == turn.participant_turn_id:
                try:
                    await worker.interrupt()
                except Exception:  # noqa: BLE001
                    logger.warning("relay: timeout interrupt failed", exc_info=True)
            return {
                "ok": False,
                "participant_id": turn.participant.id,
                "participant_turn_id": turn.participant_turn_id,
                "status": "timeout",
                "text": "",
                "error": f"participant turn timed out after {timeout}s",
            }

    async def _dispatch_from_router(
        self,
        *,
        session_id: str,
        reference: str,
        text: str,
        source: str,
        append_user_message: bool,
        chain_depth: int,
    ) -> Dict[str, Any]:
        # `source` is part of the router's call shape; the dispatch path does
        # not branch on it.
        del source
        return await self._dispatch_async(
            session_id,
            reference,
            text,
            append_user_message=append_user_message,
            chain_depth=chain_depth,
            mentions=None,
            wait=False,
            timeout=None,
        )

    # -- dispatch (group, exactly-once) -----------------------------------------

    async def _dispatch_group_async(
        self,
        session_id: str,
        dispatch_id: str,
        text: str,
        mentions: Sequence[str],
        append_user_message: bool,
    ) -> Dict[str, Any]:
        key = (session_id, dispatch_id)
        existing = self._idempotency.lookup(key)
        if existing is not None:
            # Concurrent duplicate waits on the in-flight reservation; a retry
            # after response loss gets the committed result verbatim.
            return await asyncio.shield(existing.future)

        loop = asyncio.get_running_loop()
        entry = self._idempotency.reserve(key, loop)
        accepted = _GroupAcceptance()
        try:
            payload = await self._do_dispatch_group(
                session_id, text, mentions, append_user_message, accepted
            )
        except BaseException as exc:
            if accepted.side_effect:
                # A side effect already landed (user row and/or queued turns).
                # Discarding here would let a retry duplicate it, so commit a
                # deterministic partial result instead.
                logger.warning(
                    "relay: dispatch %s failed after a side effect; memoizing partial result",
                    dispatch_id,
                    exc_info=True,
                )
                payload = accepted.partial_payload(str(exc))
                self._idempotency.commit(key, payload)
                if isinstance(exc, Exception):
                    return payload
                # Cancellation / KeyboardInterrupt still propagate so loop
                # teardown works; the entry stays committed for duplicates.
                raise
            # Pre-acceptance: nothing happened, so a retry may re-validate.
            self._idempotency.discard(key, exc)
            raise
        self._idempotency.commit(key, payload)
        return payload

    async def _do_dispatch_group(
        self,
        session_id: str,
        text: str,
        mentions: Sequence[str],
        append_user_message: bool,
        accepted: "_GroupAcceptance",
    ) -> Dict[str, Any]:
        # ---- pre-acceptance: no side effects may occur above this line -------
        participants: List[ParticipantConfig] = []
        seen: set = set()
        for mention in mentions:
            participant = self.config.resolve(str(mention))
            if participant is None or not participant.enabled:
                continue
            if participant.id in seen:
                continue
            seen.add(participant.id)
            participants.append(participant)

        if not participants:
            raise ParticipantNotFoundError(
                "no known participants in mentions: " + ", ".join(str(m) for m in mentions)
            )

        self._ensure_registered(session_id)

        if append_user_message:
            # A raised seam error here is a validation failure (unknown session,
            # ownership) that persists nothing, so it stays pre-acceptance and
            # remains retryable. Only a successful return commits the dispatch.
            self.seam().append_participant_user_message(
                session_id,
                self.plugin_id,
                text,
                [participant.handle for participant in participants],
            )
            accepted.user_row_appended = True

        # ---- accepted: from here failures are recorded, never propagated -----
        for participant in participants:
            try:
                turn = await self._start_turn(session_id, participant, text, chain_depth=0)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "relay: could not queue a turn for %s: %s", participant.id, exc
                )
                accepted.failed.append({"participant_id": participant.id, "error": str(exc)})
                continue
            accepted.turns.append(
                {
                    "participant_id": participant.id,
                    "participant_turn_id": turn.participant_turn_id,
                }
            )

        if not accepted.side_effect:
            # Nothing landed at all: still pre-acceptance, so let the caller retry.
            raise RelayRuntimeError(
                "no participant turn could be queued: "
                + "; ".join(f"{f['participant_id']}: {f['error']}" for f in accepted.failed)
            )
        return accepted.payload()

    # -- chain routing ----------------------------------------------------------

    def schedule_chain_routing(self, turn: _Turn, status: str, text: str) -> None:
        if not self.config.chain.enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop, nothing to schedule on
            return
        loop.create_task(
            self.router.route_completed_turn(
                session_id=turn.session_id,
                source_handle=turn.participant.handle,
                source_participant_id=turn.participant.id,
                text=text,
                status=status,
                chain_depth=turn.chain_depth,
            )
        )

    # -- public sync facade -----------------------------------------------------

    def dispatch(
        self,
        session_id: str,
        participant_id: str,
        text: str,
        *,
        append_user_message: bool = False,
        chain_depth: int = 0,
        mentions: Optional[Sequence[str]] = None,
        wait: bool = False,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Dispatch one participant turn. Blocks only when ``wait=True``.

        ``timeout`` defaults to the runtime's own per-turn watchdog bound, so
        there is one timeout policy rather than a caller-supplied second one.
        """

        if wait and timeout is None:
            timeout = self.turn_watchdog_seconds
        facade_timeout = None
        if wait and timeout is not None:
            facade_timeout = timeout + FACADE_TIMEOUT_MARGIN_SECONDS
        return self._submit(
            self._dispatch_async(
                session_id,
                participant_id,
                text,
                append_user_message=append_user_message,
                chain_depth=chain_depth,
                mentions=mentions,
                wait=wait,
                timeout=timeout,
            ),
            timeout=facade_timeout,
        )

    def dispatch_group(
        self,
        session_id: str,
        dispatch_id: str,
        text: str,
        mentions: Sequence[str],
        *,
        append_user_message: bool = True,
    ) -> Dict[str, Any]:
        """Fan one human submit out to every mentioned participant, exactly once."""

        if not session_id or not str(session_id).strip():
            raise DispatchValidationError("session_id is required")
        if not dispatch_id or not str(dispatch_id).strip():
            raise DispatchValidationError("dispatch_id is required")
        return self._submit(
            self._dispatch_group_async(
                str(session_id).strip(),
                str(dispatch_id).strip(),
                text,
                list(mentions or []),
                append_user_message,
            )
        )

    def interrupt(self, session_id: str, participant_id: str) -> Dict[str, Any]:
        participant = self._resolve_participant(participant_id)
        return self._submit(self._interrupt_async(session_id, participant))

    async def _interrupt_async(
        self, session_id: str, participant: ParticipantConfig
    ) -> Dict[str, Any]:
        worker = self._workers.get((session_id, participant.id))
        if worker is None:
            return _idle_payload(participant.id)
        return await worker.interrupt()

    # -- test/introspection affordances ----------------------------------------

    @property
    def idempotency(self) -> _IdempotencyCache:
        return self._idempotency

    def queue_depth(self, session_id: str, participant_id: str) -> int:
        worker = self._workers.get((session_id, participant_id))
        return worker.depth if worker is not None else 0


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_SINGLETON_LOCK = threading.Lock()
_SINGLETON: Optional[RelayRuntimeManager] = None


def get_manager(
    config: Optional[RelayConfig] = None, **kwargs: Any
) -> RelayRuntimeManager:
    """Return the process-wide manager, creating and starting it on first use."""

    global _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = RelayRuntimeManager(config, **kwargs)
            _SINGLETON.start()
        return _SINGLETON


def set_manager(manager: Optional[RelayRuntimeManager]) -> None:
    """Install (or clear) the singleton. Used by ``register`` and by tests."""

    global _SINGLETON
    with _SINGLETON_LOCK:
        _SINGLETON = manager


def shutdown_manager() -> None:
    """Tear down the singleton if one exists."""

    global _SINGLETON
    with _SINGLETON_LOCK:
        manager = _SINGLETON
        _SINGLETON = None
    if manager is not None:
        manager.shutdown()


__all__ = [
    "DispatchCapacityError",
    "DispatchValidationError",
    "FACADE_TIMEOUT_MARGIN_SECONDS",
    "IDEMPOTENCY_MAX_ENTRIES",
    "IDEMPOTENCY_TTL_SECONDS",
    "MAX_LIVE_WORKERS",
    "MAX_REGISTERED_SESSIONS",
    "ParticipantNotFoundError",
    "RelayRuntimeError",
    "RelayRuntimeManager",
    "SEAM_MISSING_REASON",
    "SeamUnavailableError",
    "get_manager",
    "load_seam",
    "set_manager",
    "shutdown_manager",
]
