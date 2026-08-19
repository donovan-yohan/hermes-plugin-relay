"""Claude Code adapter: ``claude -p`` stream-json over stdio.

Wire protocol (participant seam contract v1 section 8):

* argv ``claude -p --input-format stream-json --output-format stream-json
  --verbose --include-partial-messages --permission-mode default`` plus
  ``--model <m>`` and ``--resume <session_id>`` when configured/resuming;
* stdin NDJSON user frames ``{"type":"user","message":{"role":"user","content":…}}``;
* stdout NDJSON: ``system/init`` carries the durable ``session_id``,
  ``stream_event`` carries ``content_block_delta`` text deltas, ``result`` is the
  terminal frame (failure when ``subtype != "success"`` or ``is_error``);
* interrupt via ``control_request {subtype:"interrupt"}`` acked by
  ``control_response`` whose ``response.request_id`` echoes the request.

The child is kept warm across turns: ``system/init`` is re-emitted each turn and
``--resume`` is only needed when a fresh process must reattach.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, ClassVar, Dict, List, Optional

from ..runtime.events import MessageDelta, SessionUpdated, TurnCompleted, TurnStarted
from .base import (
    AdapterCapabilities,
    AdapterError,
    LineProcessAdapter,
    TurnInput,
)

logger = logging.getLogger(__name__)

#: How long to wait for the CLI to acknowledge an interrupt control request.
INTERRUPT_ACK_SECONDS = 5.0


class ClaudeAdapter(LineProcessAdapter):
    """Adapter for the Claude Code CLI in streaming print mode."""

    id: ClassVar[str] = "claude-code-stream-json"
    binary: ClassVar[str] = "claude"
    capabilities: ClassVar[AdapterCapabilities] = AdapterCapabilities(
        text=True,
        streaming=True,
        interrupt=True,
        resume=True,
    )

    def __init__(self, *, permission_mode: str = "default", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.permission_mode = permission_mode
        self._session_id: Optional[str] = None
        self._active_turn: Optional[str] = None
        self._delta_buffer: List[str] = []
        self._interrupt_requested = False
        self._interrupt_seq = 0
        self._pending_acks: Dict[str, "asyncio.Future[None]"] = {}

    # -- argv -------------------------------------------------------------------

    def build_argv(self, resume_session_id: Optional[str]) -> List[str]:
        argv = [
            self.binary,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--permission-mode",
            self.permission_mode,
        ]
        if self.model:
            argv += ["--model", self.model]
        if resume_session_id:
            argv += ["--resume", resume_session_id]
        argv += self.extra_args
        return argv

    # -- turn lifecycle ---------------------------------------------------------

    async def start_turn(self, turn: TurnInput) -> None:
        if self._active_turn is not None:
            raise AdapterError(
                f"{self.id}: a turn is already active ({self._active_turn})"
            )

        cwd = self.resolve_cwd(turn.cwd)
        async with self._spawn_lock:
            if not self.running:
                resume_id = self._session_id or turn.resume_session_id
                await self._spawn(self.build_argv(resume_id), cwd)
                if resume_id:
                    self._session_id = resume_id

        self._active_turn = turn.participant_turn_id
        self._delta_buffer = []
        self._interrupt_requested = False
        self.emit(TurnStarted())

        frame = {
            "type": "user",
            "message": {"role": "user", "content": turn.text},
        }
        try:
            await self.write_line(json.dumps(frame))
        except Exception as exc:  # noqa: BLE001
            self._finish("failed", error=self._with_stderr(f"stdin write failed: {exc}"))
            raise

    async def interrupt(self) -> None:
        if self._active_turn is None:
            return
        if not self.running:
            self._finish("interrupted")
            return

        self._interrupt_requested = True
        self._interrupt_seq += 1
        request_id = f"relay-int-{self._interrupt_seq}"
        loop = asyncio.get_running_loop()
        ack: "asyncio.Future[None]" = loop.create_future()
        self._pending_acks[request_id] = ack

        frame = {
            "type": "control_request",
            "request_id": request_id,
            "request": {"subtype": "interrupt"},
        }
        try:
            await self.write_line(json.dumps(frame))
        except Exception:  # noqa: BLE001
            self._pending_acks.pop(request_id, None)
            await self._terminate_process()
            self._finish("interrupted")
            return

        try:
            await asyncio.wait_for(ack, timeout=INTERRUPT_ACK_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("%s: interrupt ack timed out; killing child", self.id)
            await self._terminate_process()
        except Exception:  # noqa: BLE001
            await self._terminate_process()
        finally:
            self._pending_acks.pop(request_id, None)

        self._finish("interrupted")

    # -- protocol ---------------------------------------------------------------

    def handle_line(self, line: str) -> None:
        try:
            frame = json.loads(line)
        except (ValueError, TypeError):
            logger.warning("%s: skipping malformed stdout line", self.id)
            return
        if not isinstance(frame, dict):
            logger.warning("%s: skipping non-object stdout line", self.id)
            return

        kind = frame.get("type")
        if kind == "system":
            self._handle_system(frame)
        elif kind == "stream_event":
            self._handle_stream_event(frame)
        elif kind == "control_response":
            self._handle_control_response(frame)
        elif kind == "result":
            self._handle_result(frame)
        elif kind in ("assistant", "user"):
            # Echo frames: the text already streamed through stream_event.
            return
        else:
            logger.debug("%s: ignoring frame type %r", self.id, kind)

    def _handle_system(self, frame: Dict[str, Any]) -> None:
        session_id = frame.get("session_id")
        if isinstance(session_id, str) and session_id and session_id != self._session_id:
            self._session_id = session_id
            self.emit(SessionUpdated(session_id))

    def _handle_stream_event(self, frame: Dict[str, Any]) -> None:
        event = frame.get("event")
        if not isinstance(event, dict):
            return
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta")
        if not isinstance(delta, dict):
            return
        if delta.get("type") != "text_delta":
            # thinking_delta / input_json_delta are out of scope for slice 1.
            return
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return
        if self._active_turn is None:
            return
        self._delta_buffer.append(text)
        self.emit(MessageDelta(text))

    def _handle_control_response(self, frame: Dict[str, Any]) -> None:
        response = frame.get("response")
        request_id = None
        if isinstance(response, dict):
            request_id = response.get("request_id")
        if not isinstance(request_id, str):
            request_id = frame.get("request_id")
        if not isinstance(request_id, str):
            return
        future = self._pending_acks.get(request_id)
        if future is not None and not future.done():
            future.set_result(None)

    def _handle_result(self, frame: Dict[str, Any]) -> None:
        session_id = frame.get("session_id")
        if isinstance(session_id, str) and session_id and session_id != self._session_id:
            self._session_id = session_id
            self.emit(SessionUpdated(session_id))

        if self._active_turn is None:
            return

        if self._interrupt_requested:
            self._finish("interrupted")
            return

        subtype = frame.get("subtype")
        is_error = bool(frame.get("is_error"))
        if subtype == "success" and not is_error:
            self._finish("completed", fallback_text=frame.get("result"))
            return

        self._finish("failed", error=self._result_error(frame))

    @staticmethod
    def _result_error(frame: Dict[str, Any]) -> str:
        errors = frame.get("errors")
        if isinstance(errors, list):
            parts = [str(item) for item in errors if item]
            if parts:
                return "; ".join(parts)
        error = frame.get("error")
        if isinstance(error, str) and error:
            return error
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        subtype = frame.get("subtype")
        if isinstance(subtype, str) and subtype:
            return f"claude turn failed ({subtype})"
        return "claude turn failed"

    def on_stdout_eof(self) -> None:
        if self._active_turn is None:
            return
        if self._interrupt_requested:
            # The child died because the interrupt ladder killed it. That is an
            # interrupt, not a failure, and EOF must not win the race against
            # interrupt()'s own finalization.
            self._finish("interrupted")
            return
        self._finish(
            "failed",
            error=self._with_stderr("claude CLI exited before completing the turn"),
        )

    # -- helpers ----------------------------------------------------------------

    def _finish(
        self,
        status: str,
        *,
        error: Optional[str] = None,
        fallback_text: Any = None,
    ) -> None:
        if self._active_turn is None:
            return
        self._active_turn = None
        text = "".join(self._delta_buffer)
        if not text and isinstance(fallback_text, str):
            text = fallback_text
        self._delta_buffer = []
        self._interrupt_requested = False
        self.emit(TurnCompleted(status=status, text=text, error=error))

    async def close(self) -> None:
        if self._active_turn is not None:
            self._finish("interrupted")
        await super().close()


__all__ = ["ClaudeAdapter", "INTERRUPT_ACK_SECONDS"]
