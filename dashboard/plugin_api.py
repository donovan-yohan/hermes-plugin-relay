"""Standalone Dashboard backend for the Relay Desktop proxy.

Hermes loads this file as a flat module, so package lookup intentionally mirrors
its dashboard loader instead of using a relative import.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

router = APIRouter()

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_FALLBACK_PACKAGE = "hermes_plugin_relay"
MAX_DESKTOP_REQUEST_BYTES = 64 * 1024


def _plugin_package_name() -> str:
    """Find the loaded plugin package or load one shared fallback package."""

    root = str(_PLUGIN_ROOT)
    for name, module in list(sys.modules.items()):
        if not name.startswith("hermes_plugins.") or name.count(".") != 1:
            continue
        for entry in getattr(module, "__path__", None) or []:
            try:
                if str(Path(entry).resolve()) == root:
                    return name
            except OSError:
                continue
    if _FALLBACK_PACKAGE in sys.modules:
        return _FALLBACK_PACKAGE
    spec = importlib.util.spec_from_file_location(
        _FALLBACK_PACKAGE,
        _PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[root],
    )
    if spec is None or spec.loader is None:
        raise ImportError("cannot load the Relay plugin package")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _FALLBACK_PACKAGE
    module.__path__ = [root]  # type: ignore[attr-defined]
    sys.modules[_FALLBACK_PACKAGE] = module
    spec.loader.exec_module(module)
    return _FALLBACK_PACKAGE


_PKG = _plugin_package_name()
_proxy_mod = importlib.import_module(f"{_PKG}.relay_proxy")

RelayAuthRequiredError = _proxy_mod.RelayAuthRequiredError
RelayConfigurationError = _proxy_mod.RelayConfigurationError
ConnectionStatus = _proxy_mod.ConnectionStatus
RelayMalformedResponseError = _proxy_mod.RelayMalformedResponseError
RelayProxyError = _proxy_mod.RelayProxyError
RelayResponseTooLargeError = _proxy_mod.RelayResponseTooLargeError
RelayUnavailableError = _proxy_mod.RelayUnavailableError
RelayUpstreamError = _proxy_mod.RelayUpstreamError
MAX_CHANNEL_ID_BYTES = _proxy_mod.MAX_CHANNEL_ID_BYTES
MAX_CLIENT_MESSAGE_ID_BYTES = _proxy_mod.MAX_CLIENT_MESSAGE_ID_BYTES
MAX_HISTORY_LIMIT = _proxy_mod.MAX_HISTORY_LIMIT
MAX_MESSAGE_TEXT_BYTES = _proxy_mod.MAX_MESSAGE_TEXT_BYTES
MAX_SESSION_ID_BYTES = _proxy_mod.MAX_SESSION_ID_BYTES
NATIVE_PROVIDERS = _proxy_mod.NATIVE_PROVIDERS


def _proxy() -> Any:
    return _proxy_mod.get_relay_proxy()


def _actor_lane() -> Any:
    return _proxy_mod.get_actor_lane()


def _error(status_code: int, code: str, message: str, *, retryable: bool = False) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
    )


def _relay_error(error: RelayProxyError) -> JSONResponse:
    """Map trusted classifications, never an upstream exception message/body."""

    if isinstance(error, RelayAuthRequiredError):
        return _error(
            error.status_code,
            "auth_required",
            "Relay authorization is required",
        )
    if isinstance(error, RelayUpstreamError):
        if error.status_code == 404:
            return _error(404, "not_found", "Relay resource was not found")
        if error.status_code == 409:
            return _error(409, "conflict", "Relay reported a conflict")
        return _error(error.status_code, "relay_rejected", "Relay rejected the request")
    if isinstance(error, (RelayUnavailableError, RelayResponseTooLargeError)):
        return _error(503, "relay_unavailable", "Relay is unavailable", retryable=True)
    if isinstance(error, (RelayConfigurationError, RelayMalformedResponseError)):
        return _error(502, "relay_invalid_response", "Relay returned an invalid response", retryable=True)
    return _error(502, "relay_error", "Relay request failed", retryable=True)


class RequestValidationError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


async def _read_request_body(request: Request) -> bytes:
    """Read a bounded body, including chunked requests with no length header."""

    header = request.headers.get("content-length")
    if header is not None:
        try:
            length = int(header)
            if length < 0 or length > MAX_DESKTOP_REQUEST_BYTES:
                raise ValueError
        except ValueError:
            raise RequestValidationError(413, "request_too_large", "Request body is too large")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_DESKTOP_REQUEST_BYTES:
            raise RequestValidationError(413, "request_too_large", "Request body is too large")
    return bytes(data)


async def _read_json_object(request: Request) -> Mapping[str, Any]:
    """Read a JSON object under a hard byte cap, including chunked requests."""

    data = await _read_request_body(request)
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestValidationError(400, "invalid_json", "Request body must be JSON") from exc
    if not isinstance(body, dict):
        raise RequestValidationError(400, "invalid_body", "Request body must be a JSON object")
    return body


def _channel_id(value: str) -> str:
    if not value or len(value.encode("utf-8")) > MAX_CHANNEL_ID_BYTES:
        raise RequestValidationError(400, "invalid_channel", "Channel id is invalid")
    return value


def _history_limit(request: Request) -> int:
    values = request.query_params.getlist("limit")
    if len(values) > 1 or any(key != "limit" for key in request.query_params):
        raise RequestValidationError(400, "invalid_query", "Only one limit query parameter is supported")
    if not values:
        return MAX_HISTORY_LIMIT
    raw = values[0]
    if not raw.isascii() or not raw.isdecimal():
        raise RequestValidationError(400, "invalid_limit", "Limit must be an integer")
    limit = int(raw)
    if not 1 <= limit <= MAX_HISTORY_LIMIT:
        raise RequestValidationError(400, "invalid_limit", f"Limit must be between 1 and {MAX_HISTORY_LIMIT}")
    return limit


def _post_body(value: Mapping[str, Any]) -> dict[str, str]:
    expected = {"text", "format", "clientMessageId"}
    if set(value) != expected:
        raise RequestValidationError(
            400,
            "invalid_body",
            "Message body must contain exactly text, format, and clientMessageId",
        )
    text = value["text"]
    message_format = value["format"]
    client_message_id = value["clientMessageId"]
    if not isinstance(text, str) or not text.strip():
        raise RequestValidationError(400, "invalid_text", "Text must be a non-empty string")
    if len(text.encode("utf-8")) > MAX_MESSAGE_TEXT_BYTES:
        raise RequestValidationError(413, "text_too_large", "Text is too large")
    if message_format not in {"markdown", "text"}:
        raise RequestValidationError(400, "invalid_format", "Format must be markdown or text")
    if not isinstance(client_message_id, str) or not client_message_id:
        raise RequestValidationError(
            400, "invalid_client_message_id", "clientMessageId must be a non-empty string"
        )
    if len(client_message_id.encode("utf-8")) > MAX_CLIENT_MESSAGE_ID_BYTES:
        raise RequestValidationError(
            413, "client_message_id_too_large", "clientMessageId is too large"
        )
    # The caller's three values are deliberately returned unmodified. In
    # particular, retries must send the exact same clientMessageId to Relay.
    return {"text": text, "format": message_format, "clientMessageId": client_message_id}


def _provider(value: str) -> str:
    if value not in NATIVE_PROVIDERS:
        raise RequestValidationError(400, "invalid_provider", "Unknown harness provider")
    return value


def _native_session_id(value: str) -> str:
    if not value or len(value.encode("utf-8")) > MAX_SESSION_ID_BYTES:
        raise RequestValidationError(400, "invalid_session", "Native session id is invalid")
    return value


@router.get("/connection/status")
async def connection_status() -> Any:
    return (await run_in_threadpool(_proxy().status)).to_wire()


@router.get("/connection/onboarding")
async def connection_onboarding() -> Any:
    try:
        return {"url": await run_in_threadpool(_proxy().onboarding_url)}
    except RelayProxyError as error:
        return _relay_error(error)


@router.post("/connection/authorize")
async def connection_authorize(request: Request) -> Any:
    try:
        # This endpoint has no caller-controlled parameters. Reject a body so a
        # renderer cannot smuggle client metadata, scope, or a grant through it.
        if await _read_request_body(request):
            raise RequestValidationError(400, "invalid_body", "Authorization does not accept a body")
        channel_status = await run_in_threadpool(_proxy().authorize)
        harness_status = await run_in_threadpool(_actor_lane().authorize)
        # Report the single worst lane honestly, with that lane's own message:
        # error > offline > auth_required > ready. Collapsing distinct states
        # would tell the renderer to re-authorize during a plain outage.
        ranking = {"error": 3, "offline": 2, "auth_required": 1, "ready": 0}
        worst = max((channel_status, harness_status), key=lambda s: ranking[s.status])
        return worst.to_wire()
    except RequestValidationError as error:
        return _error(error.status_code, error.code, error.message)
    except RelayProxyError as error:
        return _relay_error(error)


@router.get("/channels")
async def list_channels() -> Any:
    try:
        return await run_in_threadpool(_proxy().list_channels)
    except RelayProxyError as error:
        return _relay_error(error)


@router.get("/channels/{channel_id}/messages")
async def channel_history(channel_id: str, request: Request) -> Any:
    try:
        return await run_in_threadpool(_proxy().history, _channel_id(channel_id), _history_limit(request))
    except RequestValidationError as error:
        return _error(error.status_code, error.code, error.message)
    except RelayProxyError as error:
        return _relay_error(error)


@router.post("/channels/{channel_id}/messages")
async def post_channel_message(channel_id: str, request: Request) -> Any:
    try:
        body = _post_body(await _read_json_object(request))
        return await run_in_threadpool(_proxy().post, _channel_id(channel_id), body)
    except RequestValidationError as error:
        return _error(error.status_code, error.code, error.message)
    except RelayProxyError as error:
        return _relay_error(error)


@router.get("/harnesses")
async def list_harnesses() -> Any:
    try:
        rows = await run_in_threadpool(_actor_lane().harnesses)
        return {"harnesses": rows}
    except RelayProxyError as error:
        return _relay_error(error)


@router.get("/harnesses/{provider}/sessions")
async def list_harness_sessions(provider: str) -> Any:
    try:
        rows = await run_in_threadpool(
            _actor_lane().harness_sessions, _provider(provider)
        )
        return {"sessions": rows}
    except RequestValidationError as error:
        return _error(error.status_code, error.code, error.message)
    except RelayProxyError as error:
        return _relay_error(error)


@router.get("/harnesses/{provider}/sessions/{native_id}")
async def get_harness_session(provider: str, native_id: str) -> Any:
    try:
        snapshot = await run_in_threadpool(
            _actor_lane().harness_session,
            _provider(provider),
            _native_session_id(native_id),
        )
        return {"snapshot": snapshot}
    except RequestValidationError as error:
        return _error(error.status_code, error.code, error.message)
    except RelayProxyError as error:
        return _relay_error(error)


__all__ = ["MAX_DESKTOP_REQUEST_BYTES", "router"]
