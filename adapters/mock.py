"""In-process mock adapter.

No subprocess, no network. It exists so the runtime manager, router, tools and
REST surface can be exercised end to end (including chain-safety behavior) in
unit tests and in a live Hermes without any provider CLI installed.

Configurable through participant ``options`` in config.yaml:

``reply_template``
    Format string rendered with ``{text}`` (the prompt) and ``{handle}``.
``chunk_size``
    Characters per streamed delta (default 8).
``delay``
    Seconds slept between deltas (default 0, so tests stay fast).
``fail_error``
    When set, the turn completes with ``status="failed"`` and this error.
``hang``
    When true the turn never completes on its own — used to test interrupts.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, ClassVar, List, Optional

from ..runtime.events import MessageDelta, SessionUpdated, TurnCompleted, TurnStarted
from .base import AdapterCapabilities, AdapterError, LineProcessAdapter, TurnInput

DEFAULT_REPLY_TEMPLATE = "mock reply: {text}"


class MockAdapter(LineProcessAdapter):
    """Deterministic adapter used by tests and by the ``mock`` participant."""

    id: ClassVar[str] = "mock"
    binary: ClassVar[str] = ""
    capabilities: ClassVar[AdapterCapabilities] = AdapterCapabilities(
        text=True,
        streaming=True,
        tools=True,
        reasoning=True,
        interrupt=True,
        resume=True,
        approvals=True,
        questions=True,
        attachments=True,
    )

    def __init__(
        self,
        *,
        reply: Optional[Callable[[str], str]] = None,
        reply_template: str = DEFAULT_REPLY_TEMPLATE,
        chunk_size: int = 8,
        delay: float = 0.0,
        fail_error: Optional[str] = None,
        hang: bool = False,
        handle: str = "mock",
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._reply = reply
        self._reply_template = reply_template
        self._chunk_size = max(1, int(chunk_size))
        self._delay = max(0.0, float(delay))
        self._fail_error = fail_error
        self._hang = bool(hang)
        self._handle = handle
        self._active_turn: Optional[str] = None
        self._buffer: List[str] = []
        self._task: Optional[asyncio.Task] = None
        self._session_id: Optional[str] = None
        #: Every prompt this adapter has been handed, in order. Test affordance.
        self.prompts: List[str] = []

    @property
    def running(self) -> bool:
        return self._active_turn is not None

    def render_reply(self, text: str) -> str:
        if self._reply is not None:
            return self._reply(text)
        try:
            return self._reply_template.format(text=text, handle=self._handle)
        except (KeyError, IndexError, ValueError):
            return self._reply_template

    async def start_turn(self, turn: TurnInput) -> None:
        if self._active_turn is not None:
            raise AdapterError(f"{self.id}: a turn is already active ({self._active_turn})")

        self.prompts.append(turn.text)
        self._active_turn = turn.participant_turn_id
        self._buffer = []

        session_id = self._session_id or turn.resume_session_id or f"mock-{self.participant_id}"
        if session_id != self._session_id:
            self._session_id = session_id
            self.emit(SessionUpdated(session_id))

        self.emit(TurnStarted())
        self._task = asyncio.ensure_future(self._run(self.render_reply(turn.text)))
        self._tasks.append(self._task)

    async def _run(self, reply: str) -> None:
        try:
            for start in range(0, len(reply), self._chunk_size):
                chunk = reply[start : start + self._chunk_size]
                if not chunk:
                    continue
                self._buffer.append(chunk)
                self.emit(MessageDelta(chunk))
                if self._delay:
                    await asyncio.sleep(self._delay)
                else:
                    await asyncio.sleep(0)
            if self._hang:
                while True:
                    await asyncio.sleep(3600)
            if self._fail_error:
                self._finish("failed", error=self._fail_error)
            else:
                self._finish("completed")
        except asyncio.CancelledError:
            raise

    async def interrupt(self) -> None:
        if self._active_turn is None:
            return
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._finish("interrupted")

    def _finish(self, status: str, *, error: Optional[str] = None) -> None:
        if self._active_turn is None:
            return
        self._active_turn = None
        text = "".join(self._buffer)
        self._buffer = []
        self.emit(TurnCompleted(status=status, text=text, error=error))

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
        self._finish("interrupted")
        await super().close()


__all__ = ["DEFAULT_REPLY_TEMPLATE", "MockAdapter"]
