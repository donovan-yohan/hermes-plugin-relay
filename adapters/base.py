"""Adapter contract and shared subprocess plumbing.

Participant seam contract v1 section 8. Adapters are asyncio-native, own one
provider child process each, and emit only the normalized event vocabulary in
:mod:`runtime.events`.

Rules enforced here so every concrete adapter inherits them:

* argv lists only, never a shell string;
* ``shutil.which`` before spawn so a missing binary is a clean error, not an
  ``OSError`` from deep inside asyncio;
* bounded stderr ring buffer (50 lines) whose tail is attached to spawn/crash
  errors;
* bounded stdout line size, malformed lines warned and skipped;
* teardown kills the child's process group, SIGTERM then SIGKILL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from ..runtime.events import AdapterEvent

logger = logging.getLogger(__name__)

#: How many stderr lines to retain for crash diagnostics.
STDERR_RING_LINES = 50

#: Hard cap on a single protocol line. Longer lines are dropped with a warning.
MAX_LINE_BYTES = 2 * 1024 * 1024

#: StreamReader buffer limit handed to ``create_subprocess_exec``. Larger than
#: ``MAX_LINE_BYTES`` so oversize lines surface as our own bound, not asyncio's.
STREAM_LIMIT_BYTES = 8 * 1024 * 1024

#: Grace period between SIGTERM and SIGKILL during teardown.
TERMINATE_GRACE_SECONDS = 3.0

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


class AdapterError(RuntimeError):
    """Base class for adapter-visible failures."""


class AdapterNotAvailableError(AdapterError):
    """The provider binary or its credentials are not usable on this machine."""


@dataclass(frozen=True)
class AdapterCapabilities:
    """Honest capability declaration surfaced in the participant roster."""

    text: bool = True
    streaming: bool = False
    tools: bool = False
    reasoning: bool = False
    interrupt: bool = False
    resume: bool = False
    approvals: bool = False
    questions: bool = False
    attachments: bool = False

    def to_dict(self) -> Dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True)
class Availability:
    """Result of a cheap, side-effect-free readiness probe."""

    status: str  # "ready" | "offline" | "error"
    reason: Optional[str] = None


@dataclass(frozen=True)
class TurnInput:
    """One participant turn handed to an adapter."""

    text: str
    cwd: str
    participant_turn_id: str
    resume_session_id: Optional[str] = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Structural type every adapter satisfies."""

    id: str
    capabilities: AdapterCapabilities

    async def start_turn(self, turn: TurnInput) -> None: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...

    def subscribe(self, handler: Callable[[AdapterEvent], None]) -> Callable[[], None]: ...


ProcessFactory = Callable[..., Awaitable[Any]]


async def default_process_factory(
    argv: List[str],
    *,
    cwd: str,
    env: Optional[Dict[str, str]] = None,
) -> Any:
    """Spawn a real child process in its own session/process group."""

    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT_BYTES,
        start_new_session=True,
    )


def probe_binary(binary: str) -> Optional[str]:
    """Return the resolved path of ``binary`` or ``None`` when absent."""

    return shutil.which(binary)


class LineProcessAdapter:
    """Base class for adapters that speak newline-delimited JSON over stdio."""

    id: ClassVar[str] = "line-process"
    binary: ClassVar[str] = ""
    capabilities: ClassVar[AdapterCapabilities] = AdapterCapabilities()

    def __init__(
        self,
        *,
        participant_id: str,
        model: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        process_factory: Optional[ProcessFactory] = None,
    ) -> None:
        self.participant_id = participant_id
        self.model = model
        self.extra_args = list(extra_args or [])
        self._env = env
        self._process_factory = process_factory or default_process_factory
        #: Immutable so ``emit`` can iterate without copying: deltas are the
        #: hottest path in the plugin and every token used to allocate a list.
        self._subscribers: Tuple[Callable[[AdapterEvent], None], ...] = ()
        self._proc: Any = None
        self._stderr_ring: "deque[str]" = deque(maxlen=STDERR_RING_LINES)
        self._tasks: List[asyncio.Task] = []
        self._closed = False
        self._spawn_lock = asyncio.Lock()

    # -- public adapter surface -------------------------------------------------

    def subscribe(self, handler: Callable[[AdapterEvent], None]) -> Callable[[], None]:
        self._subscribers = self._subscribers + (handler,)

        def _unsubscribe() -> None:
            self._subscribers = tuple(h for h in self._subscribers if h is not handler)

        return _unsubscribe

    def emit(self, event: AdapterEvent) -> None:
        for handler in self._subscribers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - a bad subscriber must not kill the adapter
                logger.exception("participant adapter subscriber raised")

    @property
    def running(self) -> bool:
        return self._proc is not None and getattr(self._proc, "returncode", None) is None

    async def start_turn(self, turn: TurnInput) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def interrupt(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    async def close(self) -> None:
        self._closed = True
        await self._terminate_process()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    # -- availability -----------------------------------------------------------

    @classmethod
    def availability(cls) -> Availability:
        if not cls.binary:
            return Availability("ready")
        if probe_binary(cls.binary) is None:
            return Availability("offline", f"{cls.binary} CLI not found on PATH")
        return Availability("ready")

    # -- process plumbing -------------------------------------------------------

    @staticmethod
    def resolve_cwd(turn_cwd: Optional[str]) -> str:
        """Working directory for a turn. The manager always resolves one."""

        if turn_cwd:
            return str(Path(turn_cwd).expanduser())
        return str(Path.home())

    async def _spawn(self, argv: List[str], cwd: str) -> Any:
        """Spawn the child and start the stdout/stderr readers."""

        if self.binary and self._process_factory is default_process_factory:
            if probe_binary(argv[0]) is None:
                raise AdapterNotAvailableError(f"{argv[0]} CLI not found on PATH")

        cwd_path = Path(cwd)
        if not cwd_path.is_dir():
            raise AdapterError(f"working directory does not exist: {cwd}")

        try:
            proc = await self._process_factory(argv, cwd=str(cwd_path), env=self._env)
        except FileNotFoundError as exc:
            raise AdapterNotAvailableError(f"{argv[0]} CLI not found on PATH") from exc
        except OSError as exc:
            raise AdapterError(f"failed to spawn {argv[0]}: {exc}") from exc

        self._proc = proc
        self._stderr_ring.clear()
        if getattr(proc, "stdout", None) is not None:
            self._tasks.append(asyncio.ensure_future(self._read_stdout(proc.stdout)))
        if getattr(proc, "stderr", None) is not None:
            self._tasks.append(asyncio.ensure_future(self._read_stderr(proc.stderr)))
        return proc

    async def _read_stdout(self, stream: Any) -> None:
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                logger.warning("%s: dropped oversize stdout line", self.id)
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("%s: stdout reader failed", self.id)
                break
            if not raw:
                break
            if len(raw) > MAX_LINE_BYTES:
                logger.warning("%s: dropped stdout line of %d bytes", self.id, len(raw))
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                self.handle_line(line)
            except Exception:  # noqa: BLE001 - protocol handling must never crash the reader
                logger.exception("%s: failed handling protocol line", self.id)
        try:
            self.on_stdout_eof()
        except Exception:  # noqa: BLE001
            logger.exception("%s: stdout EOF handler failed", self.id)

    async def _read_stderr(self, stream: Any) -> None:
        while True:
            try:
                raw = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                break
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_ring.append(text[:2000])

    def stderr_tail(self, limit: int = 10) -> str:
        lines = list(self._stderr_ring)[-limit:]
        return "\n".join(lines)

    def _with_stderr(self, message: str) -> str:
        """Attach the child's recent stderr to a crash/spawn error message."""

        tail = self.stderr_tail()
        return f"{message}\n{tail}" if tail else message

    def handle_line(self, line: str) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def on_stdout_eof(self) -> None:
        """Called once the child's stdout closes. Subclasses may react."""

    async def write_line(self, payload: str) -> None:
        proc = self._proc
        stdin = getattr(proc, "stdin", None) if proc is not None else None
        if stdin is None:
            raise AdapterError(f"{self.id}: child process has no stdin")
        data = (payload + "\n").encode("utf-8")
        stdin.write(data)
        drain = getattr(stdin, "drain", None)
        if drain is not None:
            await drain()

    async def _terminate_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if getattr(proc, "returncode", None) is not None:
            return

        stdin = getattr(proc, "stdin", None)
        if stdin is not None:
            try:
                stdin.close()
            except Exception:  # noqa: BLE001
                pass

        self._signal_process_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001
            return

        self._signal_process_group(proc, _SIGKILL)
        try:
            await asyncio.wait_for(proc.wait(), timeout=TERMINATE_GRACE_SECONDS)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _signal_process_group(proc: Any, sig: int) -> None:
        pid = getattr(proc, "pid", None)
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        if pid and killpg is not None and getpgid is not None:
            try:
                killpg(getpgid(pid), sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            if sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass


__all__ = [
    "AdapterCapabilities",
    "AdapterError",
    "AdapterNotAvailableError",
    "AgentAdapter",
    "Availability",
    "LineProcessAdapter",
    "MAX_LINE_BYTES",
    "STDERR_RING_LINES",
    "TurnInput",
    "default_process_factory",
    "probe_binary",
]
