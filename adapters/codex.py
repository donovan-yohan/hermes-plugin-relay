"""Codex adapter: ``codex app-server`` NDJSON JSON-RPC 2.0 over stdio.

Wire protocol (participant seam contract v1 section 8):

* argv ``codex app-server --listen stdio://`` — stdio must be opted into
  explicitly because newer codex builds default to a TCP listener;
* one JSON object per line, no ``Content-Length`` framing;
* handshake: request ``initialize {clientInfo}`` then notification
  ``initialized`` (no id, no params);
* thread: ``thread/start {cwd}`` or ``thread/resume {threadId}``; the durable id
  also arrives as the ``thread/started`` notification;
* turn: request ``turn/start {threadId, input:[{type:"text",text}]}``;
  notifications ``turn/started``, ``item/agentMessage/delta``,
  ``item/completed`` (agentMessage text is authoritative) and ``turn/completed``;
* interrupt: request ``turn/interrupt {threadId, turnId}``, errors swallowed,
  then wait for the ``interrupted`` ``turn/completed``.

``turn/completed`` can legitimately arrive before the final agentMessage
``item/completed``; finalization is deferred by
:data:`TERMINAL_ITEM_GRACE_SECONDS` so the authoritative text is not lost.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Set

from ..runtime.events import MessageDelta, SessionUpdated, TurnCompleted, TurnStarted
from .base import (
    AdapterCapabilities,
    AdapterError,
    Availability,
    LineProcessAdapter,
    TurnInput,
)

logger = logging.getLogger(__name__)

#: Codex can notify ``turn/completed`` before the last agentMessage's
#: ``item/completed``. Hold the turn open briefly so the authoritative text wins.
TERMINAL_ITEM_GRACE_SECONDS = 0.25

#: Default timeout for handshake / thread / turn-start requests.
RPC_TIMEOUT_SECONDS = 60.0

#: How long to wait for the interrupted ``turn/completed`` before forcing it.
INTERRUPT_WAIT_SECONDS = 5.0

#: JSON-RPC "Method not found". Slice 1 implements no server-initiated requests.
METHOD_NOT_FOUND = -32601

CLIENT_INFO = {
    "name": "hermes-plugin-relay",
    "title": "Hermes Relay Participants",
    "version": "0.1.0",
}


class CodexAdapter(LineProcessAdapter):
    """Adapter for the Codex app-server JSON-RPC transport."""

    id: ClassVar[str] = "codex-app-server"
    binary: ClassVar[str] = "codex"
    capabilities: ClassVar[AdapterCapabilities] = AdapterCapabilities(
        text=True,
        streaming=True,
        interrupt=True,
        resume=True,
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._next_id = 0
        self._pending: Dict[int, "asyncio.Future[Any]"] = {}
        self._thread_id: Optional[str] = None
        self._native_turn_id: Optional[str] = None
        self._active_turn: Optional[str] = None
        self._delta_buffer: List[str] = []
        #: Every completed agentMessage of the CURRENT turn, in arrival order. A
        #: codex turn routinely emits a preamble message and then a final one
        #: (tool interleaving); keeping only the last would silently drop text
        #: that already streamed to the user.
        self._final_parts: List[str] = []
        self._open_agent_items: Set[str] = set()
        self._deferred: Optional[Dict[str, Any]] = None
        self._deferred_handle: Optional[asyncio.TimerHandle] = None
        self._item_completed_in_grace = False
        self._turn_done = asyncio.Event()
        self._interrupt_requested = False

    # -- argv -------------------------------------------------------------------

    def build_argv(self) -> List[str]:
        return [self.binary, "app-server", "--listen", "stdio://"] + self.extra_args

    # -- JSON-RPC plumbing ------------------------------------------------------

    async def _call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        timeout: float = RPC_TIMEOUT_SECONDS,
    ) -> Any:
        self._next_id += 1
        request_id = self._next_id
        loop = asyncio.get_running_loop()
        future: "asyncio.Future[Any]" = loop.create_future()
        self._pending[request_id] = future

        message: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        try:
            await self.write_line(json.dumps(message))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AdapterError(f"codex {method} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self.write_line(json.dumps(message))

    async def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self.write_line(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )
        )

    # -- connection -------------------------------------------------------------

    async def _connect(self, cwd: str, resume_session_id: Optional[str]) -> None:
        await self._spawn(self.build_argv(), cwd)
        await self._call("initialize", {"clientInfo": dict(CLIENT_INFO)})
        await self._notify("initialized")

        requested = self._thread_id or resume_session_id
        if requested:
            result = await self._call("thread/resume", {"threadId": requested})
        else:
            result = await self._call("thread/start", {"cwd": cwd})

        thread_id = _thread_id_from(result) or requested
        if thread_id:
            self._set_thread_id(thread_id)

    def _set_thread_id(self, thread_id: str) -> None:
        if thread_id and thread_id != self._thread_id:
            self._thread_id = thread_id
            self.emit(SessionUpdated(thread_id))

    # -- turn lifecycle ---------------------------------------------------------

    async def start_turn(self, turn: TurnInput) -> None:
        if self._active_turn is not None:
            raise AdapterError(
                f"{self.id}: a turn is already active ({self._active_turn})"
            )

        cwd = self.resolve_cwd(turn.cwd)
        async with self._spawn_lock:
            if not self.running:
                await self._connect(cwd, turn.resume_session_id)

        if not self._thread_id:
            raise AdapterError("codex app-server did not return a thread id")

        self._active_turn = turn.participant_turn_id
        self._delta_buffer = []
        self._final_parts = []
        self._open_agent_items.clear()
        self._native_turn_id = None
        self._interrupt_requested = False
        self._clear_deferred()
        self._turn_done = asyncio.Event()
        self.emit(TurnStarted())

        params = {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": turn.text}],
        }
        try:
            result = await self._call("turn/start", params)
        except Exception as exc:  # noqa: BLE001
            self._finish("failed", error=self._with_stderr(str(exc)))
            return
        if isinstance(result, dict):
            native = result.get("turnId") or _dig(result, "turn", "id")
            if isinstance(native, str):
                self._native_turn_id = native

    async def interrupt(self) -> None:
        if self._active_turn is None:
            return
        self._interrupt_requested = True

        if self.running and self._thread_id and self._native_turn_id:
            try:
                await self._call(
                    "turn/interrupt",
                    {"threadId": self._thread_id, "turnId": self._native_turn_id},
                    timeout=INTERRUPT_WAIT_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - best effort by contract
                logger.warning("%s: turn/interrupt failed: %s", self.id, exc)

        try:
            await asyncio.wait_for(self._turn_done.wait(), timeout=INTERRUPT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("%s: no interrupted turn/completed; forcing", self.id)
            self._finish("interrupted")

    # -- protocol ---------------------------------------------------------------

    def handle_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            logger.warning("%s: skipping malformed JSON-RPC line", self.id)
            return
        if not isinstance(message, dict):
            logger.warning("%s: skipping non-object JSON-RPC line", self.id)
            return

        has_id = "id" in message and message.get("id") is not None
        method = message.get("method")

        if has_id and not method:
            self._handle_response(message)
            return
        if has_id and method:
            # Server-initiated request (approvals, user input): slice 1 declines.
            asyncio.ensure_future(
                self._respond_error(
                    message.get("id"), METHOD_NOT_FOUND, "Method not found"
                )
            )
            return
        if isinstance(method, str):
            params = message.get("params")
            self._handle_notification(method, params if isinstance(params, dict) else {})
            return
        logger.debug("%s: unrecognised JSON-RPC message shape", self.id)

    def _handle_response(self, message: Dict[str, Any]) -> None:
        request_id = message.get("id")
        future = self._pending.get(request_id) if isinstance(request_id, int) else None
        if future is None or future.done():
            logger.debug("%s: response for unknown id %r", self.id, request_id)
            return
        error = message.get("error")
        if isinstance(error, dict):
            text = error.get("message") or "codex rpc error"
            code = error.get("code")
            future.set_exception(AdapterError(f"codex rpc error {code}: {text}"))
            return
        future.set_result(message.get("result"))

    def _handle_notification(self, method: str, params: Dict[str, Any]) -> None:
        if method == "thread/started":
            thread_id = _thread_id_from(params)
            if thread_id:
                self._set_thread_id(thread_id)
        elif method == "turn/started":
            native = _dig(params, "turn", "id") or params.get("turnId")
            if isinstance(native, str):
                self._native_turn_id = native
        elif method == "item/agentMessage/delta":
            self._handle_agent_delta(params)
        elif method == "item/completed":
            self._handle_item_completed(params)
        elif method == "turn/completed":
            self._handle_turn_completed(params)
        else:
            logger.debug("%s: ignoring notification %s", self.id, method)

    def _handle_agent_delta(self, params: Dict[str, Any]) -> None:
        if self._active_turn is None:
            return
        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        item_id = params.get("itemId")
        if isinstance(item_id, str) and item_id:
            self._open_agent_items.add(item_id)
        self._delta_buffer.append(delta)
        self.emit(MessageDelta(delta))

    def _handle_item_completed(self, params: Dict[str, Any]) -> None:
        item = params.get("item")
        if not isinstance(item, dict):
            item = params
        item_type = item.get("type") or params.get("itemType")
        item_id = item.get("id") or params.get("itemId")
        if isinstance(item_id, str):
            self._open_agent_items.discard(item_id)
        if item_type == "agentMessage":
            if self._deferred is not None:
                self._item_completed_in_grace = True
            text = item.get("text")
            if not isinstance(text, str):
                text = item.get("content")
            # Accumulate: a turn may complete several agentMessages and each is
            # authoritative for its own item, never for the whole turn.
            if isinstance(text, str) and text:
                self._final_parts.append(text)
        self._release_deferred_if_ready()

    def _handle_turn_completed(self, params: Dict[str, Any]) -> None:
        if self._active_turn is None:
            return
        turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
        native_status = turn.get("status") or params.get("status")
        error = turn.get("error") or params.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        if not isinstance(error, str):
            error = None

        if native_status == "interrupted" or self._interrupt_requested:
            status = "interrupted"
        elif native_status == "failed":
            status = "failed"
            error = error or "codex turn failed"
        else:
            status = "completed"

        # Terminal grace (contract v1.4): app-server can deliver the final
        # `item/completed` after `turn/completed`, and that item may be the FIRST
        # sight of the text (no prior delta, no prior item/started). The grace
        # therefore always runs; it is released early only when a late
        # `item/completed` actually lands inside the window, which is the one
        # signal that proves the trailing item has arrived.
        self._defer_completion(status, error)

    # -- deferred completion ----------------------------------------------------

    def _defer_completion(self, status: str, error: Optional[str]) -> None:
        self._clear_deferred()
        self._deferred = {"status": status, "error": error}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop, finalize now
            self._finish(status, error=error)
            return
        self._deferred_handle = loop.call_later(
            TERMINAL_ITEM_GRACE_SECONDS, self._release_deferred_now
        )

    def _release_deferred_if_ready(self) -> None:
        """Finalize early once a late agentMessage has closed inside the grace."""

        if self._deferred is None or self._open_agent_items:
            return
        if not self._item_completed_in_grace:
            return
        self._release_deferred_now()

    def _release_deferred_now(self) -> None:
        deferred = self._deferred
        if deferred is None:
            return
        self._clear_deferred()
        self._finish(deferred["status"], error=deferred["error"])

    def _clear_deferred(self) -> None:
        if self._deferred_handle is not None:
            self._deferred_handle.cancel()
            self._deferred_handle = None
        self._deferred = None
        self._item_completed_in_grace = False

    # -- teardown ---------------------------------------------------------------

    def on_stdout_eof(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(AdapterError("codex app-server exited"))
        self._pending.clear()
        if self._active_turn is not None:
            if self._interrupt_requested:
                # Killed as part of the interrupt path: report the interrupt.
                self._finish("interrupted")
                return
            self._finish(
                "failed",
                error=self._with_stderr("codex app-server exited before the turn completed"),
            )

    def _finish(self, status: str, *, error: Optional[str] = None) -> None:
        if self._active_turn is None:
            return
        self._active_turn = None
        self._clear_deferred()
        # Completed items are authoritative when we have any; otherwise fall
        # back to what actually streamed.
        if self._final_parts:
            text = "\n\n".join(self._final_parts)
        else:
            text = "".join(self._delta_buffer)
        self._delta_buffer = []
        self._final_parts = []
        self._open_agent_items.clear()
        self._interrupt_requested = False
        self._turn_done.set()
        self.emit(TurnCompleted(status=status, text=text, error=error))

    async def close(self) -> None:
        if self._active_turn is not None:
            self._finish("interrupted")
        await super().close()

    @classmethod
    def availability(cls, *, home: Optional[Path] = None) -> Availability:
        probed = super().availability()
        if probed.status != "ready":
            return probed
        auth_path = (home or Path.home()) / ".codex" / "auth.json"
        try:
            present = auth_path.exists()
        except OSError:
            present = False
        if not present:
            return Availability(
                "error", "codex is not signed in (~/.codex/auth.json missing)"
            )
        return Availability("ready")


def _dig(source: Any, *keys: str) -> Any:
    current = source
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _thread_id_from(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    candidate = _dig(payload, "thread", "id")
    if isinstance(candidate, str) and candidate:
        return candidate
    candidate = payload.get("threadId")
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


__all__ = [
    "CLIENT_INFO",
    "CodexAdapter",
    "INTERRUPT_WAIT_SECONDS",
    "TERMINAL_ITEM_GRACE_SECONDS",
]
