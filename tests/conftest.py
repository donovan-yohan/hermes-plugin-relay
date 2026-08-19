"""Shared fixtures for the hermes-plugin-relay suite.

Three things every test needs:

1. the plugin package importable under a valid Python name (the repo directory
   is ``hermes-plugin-relay``, which is not an identifier — Hermes imports it as
   ``hermes_plugins.hermes_plugin_relay``, we mirror that as
   ``hermes_plugin_relay``);
2. a temp ``HERMES_HOME`` so nothing ever touches the user's real Hermes state;
3. a :class:`FakeSeam` standing in for ``tui_gateway.participants``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "hermes_plugin_relay"


def _bootstrap_package() -> types.ModuleType:
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PACKAGE
    module.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_bootstrap_package()


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def temp_hermes_home(tmp_path_factory, monkeypatch) -> Path:
    """Point every Hermes-home lookup at a throwaway directory."""

    home = tmp_path_factory.mktemp("hermes-home")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_manager_singleton():
    from hermes_plugin_relay.runtime.manager import set_manager, shutdown_manager

    set_manager(None)
    yield
    shutdown_manager()


# ---------------------------------------------------------------------------
# Fake core seam
# ---------------------------------------------------------------------------


class ParticipantSeamError(Exception):
    """Mirror of the core error hierarchy."""


class UnknownSessionError(ParticipantSeamError):
    pass


class OwnershipError(ParticipantSeamError):
    pass


class UnknownTurnError(ParticipantSeamError):
    pass


class FakeSeam:
    """Records every publisher call the manager makes, per contract section 3."""

    ParticipantSeamError = ParticipantSeamError
    UnknownSessionError = UnknownSessionError
    OwnershipError = OwnershipError
    UnknownTurnError = UnknownTurnError

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.registered: Dict[str, List[dict]] = {}
        self.user_messages: List[dict] = []
        self.turns: Dict[str, dict] = {}
        self._next_row_id = 1
        self._lock = threading.Lock()

    # -- helpers ---------------------------------------------------------------

    def _row_id(self) -> int:
        with self._lock:
            row_id = self._next_row_id
            self._next_row_id += 1
            return row_id

    def calls_named(self, name: str) -> List[tuple]:
        return [call for call in self.calls if call[0] == name]

    # -- contract section 3 surface --------------------------------------------

    def register_participants(self, session_id, plugin_id, participants) -> None:
        with self._lock:
            self.calls.append(("register_participants", session_id, plugin_id, list(participants)))
            self.registered[session_id] = list(participants)

    def append_participant_user_message(self, session_id, plugin_id, text, mentions) -> int:
        row_id = self._row_id()
        with self._lock:
            self.calls.append(
                ("append_participant_user_message", session_id, plugin_id, text, list(mentions))
            )
            self.user_messages.append(
                {
                    "row_id": row_id,
                    "session_id": session_id,
                    "text": text,
                    "mentions": list(mentions),
                }
            )
        return row_id

    def begin_participant_message(
        self, session_id, plugin_id, participant_id, participant_turn_id
    ) -> int:
        row_id = self._row_id()
        with self._lock:
            self.calls.append(
                (
                    "begin_participant_message",
                    session_id,
                    plugin_id,
                    participant_id,
                    participant_turn_id,
                )
            )
            self.turns[participant_turn_id] = {
                "row_id": row_id,
                "session_id": session_id,
                "participant_id": participant_id,
                "status": "streaming",
                "deltas": [],
                "text": "",
                "error": None,
            }
        return row_id

    def append_participant_delta(self, session_id, plugin_id, participant_turn_id, delta) -> None:
        with self._lock:
            self.calls.append(
                ("append_participant_delta", session_id, plugin_id, participant_turn_id, delta)
            )
            turn = self.turns.get(participant_turn_id)
            if turn is None:
                raise UnknownTurnError(participant_turn_id)
            turn["deltas"].append(delta)

    def complete_participant_message(
        self,
        session_id,
        plugin_id,
        participant_turn_id,
        *,
        status: str = "completed",
        text: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.calls.append(
                (
                    "complete_participant_message",
                    session_id,
                    plugin_id,
                    participant_turn_id,
                    status,
                    text,
                    error,
                )
            )
            turn = self.turns.get(participant_turn_id)
            if turn is None:
                raise UnknownTurnError(participant_turn_id)
            turn["status"] = status
            turn["text"] = text if text is not None else "".join(turn["deltas"])
            turn["error"] = error


@pytest.fixture
def fake_seam() -> FakeSeam:
    return FakeSeam()


def install_seam_module(seam: Any, monkeypatch) -> None:
    package = sys.modules.get("tui_gateway")
    if package is None:
        package = types.ModuleType("tui_gateway")
        package.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tui_gateway", package)
    monkeypatch.setitem(sys.modules, "tui_gateway.participants", seam)
    monkeypatch.setattr(package, "participants", seam, raising=False)


def force_seam_absent(monkeypatch) -> None:
    """Make ``import tui_gateway.participants`` fail, whatever sys.path holds.

    ``tests/test_integration_seam.py`` inserts a real Hermes checkout into
    ``sys.path`` for the whole process, so "the seam is missing" cannot be
    asserted by relying on the ambient environment. A ``None`` entry in
    ``sys.modules`` makes the import raise ``ImportError`` deterministically.
    """

    monkeypatch.setitem(sys.modules, "tui_gateway.participants", None)


def force_hermes_absent(monkeypatch) -> None:
    """Make every Hermes-side lazy import fail (seam and gateway session env)."""

    force_seam_absent(monkeypatch)
    monkeypatch.setitem(sys.modules, "gateway.session_context", None)


@pytest.fixture
def seam_absent(monkeypatch):
    force_seam_absent(monkeypatch)


@pytest.fixture
def hermes_absent(monkeypatch):
    force_hermes_absent(monkeypatch)


def install_session_context(get_session_env: Callable[..., str], monkeypatch) -> types.ModuleType:
    """Install a fake ``gateway.session_context`` exposing ``get_session_env``."""

    package = sys.modules.get("gateway")
    if package is None or not hasattr(package, "__path__"):
        package = types.ModuleType("gateway")
        package.__path__ = []  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "gateway", package)

    module = types.ModuleType("gateway.session_context")
    module.get_session_env = get_session_env  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)
    monkeypatch.setattr(package, "session_context", module, raising=False)
    return module


# ---------------------------------------------------------------------------
# Fake subprocess plumbing
# ---------------------------------------------------------------------------


class FakeStdin:
    """Collects what an adapter writes and optionally reacts to each line."""

    def __init__(self, on_line: Optional[Callable[[str], None]] = None) -> None:
        self.on_line = on_line
        self.raw: bytes = b""
        self.lines: List[str] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.raw += data
        while b"\n" in self.raw:
            head, _, self.raw = self.raw.partition(b"\n")
            line = head.decode("utf-8").strip()
            if not line:
                continue
            self.lines.append(line)
            if self.on_line is not None:
                self.on_line(line)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    @property
    def json_lines(self) -> List[dict]:
        return [json.loads(line) for line in self.lines]


class FakeProcess:
    """Process-like object backed by real ``asyncio.StreamReader`` streams."""

    def __init__(self, on_stdin_line: Optional[Callable[["FakeProcess", str], None]] = None) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(lambda line: self._on_line(line))
        self._on_stdin_line = on_stdin_line
        self.pid = None  # keeps teardown off os.killpg
        self.returncode: Optional[int] = None
        self.argv: List[str] = []
        self.cwd: Optional[str] = None
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    def _on_line(self, line: str) -> None:
        if self._on_stdin_line is not None:
            self._on_stdin_line(self, line)

    # -- feeding ---------------------------------------------------------------

    def feed(self, payload: Any) -> None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.stdout.feed_data((text + "\n").encode("utf-8"))

    def feed_stderr(self, text: str) -> None:
        self.stderr.feed_data((text + "\n").encode("utf-8"))

    def feed_eof(self) -> None:
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    # -- process API -----------------------------------------------------------

    async def wait(self) -> int:
        await self._exited.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.terminated = True
        self._exit(-15)

    def kill(self) -> None:
        self.killed = True
        self._exit(-9)

    def _exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
        if not self.stderr.at_eof():
            self.stderr.feed_eof()
        self._exited.set()


def process_factory(process: FakeProcess) -> Callable[..., Any]:
    """Adapter ``process_factory`` that always hands back ``process``."""

    async def _factory(argv: List[str], *, cwd: str, env: Any = None) -> FakeProcess:
        process.argv = list(argv)
        process.cwd = cwd
        return process

    return _factory


async def drain(times: int = 8) -> None:
    """Yield to the loop enough times for reader tasks to make progress."""

    for _ in range(times):
        await asyncio.sleep(0)


async def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """Poll ``predicate`` on the loop until true or ``timeout`` elapses."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.002)
    return predicate()


def collect(adapter: Any) -> List[Any]:
    """Subscribe to an adapter and return the (mutating) event list."""

    events: List[Any] = []
    adapter.subscribe(events.append)
    return events
