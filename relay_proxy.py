"""Loopback-only, credential-owning proxy for Relay's stable channel API.

The Desktop renderer reaches this module only through ``dashboard/plugin_api.py``.
Credentials remain in this process and never cross the plugin REST boundary.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

PLUGIN_ID = "hermes-plugin-relay"
DEFAULT_RELAY_IDE_URL = "http://127.0.0.1:3456"
RELAY_REQUEST_TIMEOUT_SECONDS = 5.0
# Relay's stable history route permits a 4 MiB message budget before JSON
# framing. Keep a bounded envelope with enough headroom for that valid payload.
MAX_RELAY_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_TEXT_BYTES = 64 * 1024
MAX_CLIENT_MESSAGE_ID_BYTES = 512
MAX_CHANNEL_ID_BYTES = 512
MAX_HISTORY_LIMIT = 50
OPERATOR_CREDENTIAL_TTL_SECONDS = 15 * 60
OPERATOR_CREDENTIAL_TTL_MS = OPERATOR_CREDENTIAL_TTL_SECONDS * 1000
OPERATOR_CLIENT_METADATA = {
    "id": "desktop-plugin-backend",
    "displayName": "Desktop plugin backend",
    "platform": "linux",
}


class RelayProxyError(Exception):
    """Base class whose public handling never exposes its string value."""


class RelayConfigurationError(RelayProxyError):
    pass


class RelayUnavailableError(RelayProxyError):
    pass


class RelayMalformedResponseError(RelayProxyError):
    pass


class RelayResponseTooLargeError(RelayProxyError):
    pass


class RelayAuthRequiredError(RelayProxyError):
    def __init__(self, status_code: int = 401) -> None:
        self.status_code = status_code


class RelayUpstreamError(RelayProxyError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@dataclass(frozen=True)
class RelayHttpResponse:
    status_code: int
    payload: Any


class RelayTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> RelayHttpResponse:
        """Make one bounded Relay request without logging sensitive data."""


def validate_relay_base_url(value: str) -> str:
    """Accept exactly an HTTP root URL on a literal loopback host."""

    if not isinstance(value, str) or not value:
        raise RelayConfigurationError()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RelayConfigurationError() from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RelayConfigurationError()
    # Never leave `localhost` to resolver policy: canonicalize it to a literal
    # loopback address before any credential-bearing request is constructed.
    host = "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname
    authority = f"[{host}]" if host == "::1" else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"http://{authority}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on redirects so authorization never follows another hop."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _read_limited(stream: Any, headers: Mapping[str, str]) -> bytes:
    content_length = headers.get("Content-Length")
    if content_length is not None:
        try:
            length = int(content_length)
            if length < 0 or length > MAX_RELAY_RESPONSE_BYTES:
                raise RelayResponseTooLargeError()
        except ValueError as exc:
            raise RelayMalformedResponseError() from exc
    try:
        data = stream.read(MAX_RELAY_RESPONSE_BYTES + 1)
    except (TimeoutError, OSError) as exc:
        raise RelayUnavailableError() from exc
    if len(data) > MAX_RELAY_RESPONSE_BYTES:
        raise RelayResponseTooLargeError()
    return data


def _decode_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RelayMalformedResponseError() from exc


class UrlLibRelayTransport:
    """Small stdlib transport with one timeout and bounded JSON responses."""

    def __init__(self, timeout_seconds: float = RELAY_REQUEST_TIMEOUT_SECONDS) -> None:
        self._timeout_seconds = timeout_seconds
        # Loopback traffic must not inherit ambient proxy settings. Combined
        # with the redirect guard, every credential-bearing hop stays literal.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirectHandler(),
        )

    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> RelayHttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                return RelayHttpResponse(
                    status_code=response.status,
                    payload=_decode_json(_read_limited(response, response.headers)),
                )
        except urllib.error.HTTPError as error:
            # The proxy never forwards Relay's error body. Closing it without
            # reading preserves the upstream HTTP classification (especially
            # 401/403) without retaining arbitrary upstream data.
            error.close()
            return RelayHttpResponse(status_code=error.code, payload=None)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RelayUnavailableError() from exc


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RelayMalformedResponseError()
    return value


def _required_string(value: Mapping[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise RelayMalformedResponseError()
    return result


def _optional_string(value: Mapping[str, Any], key: str) -> str | None:
    result = value.get(key)
    return result if isinstance(result, str) else None


def _optional_int(value: Mapping[str, Any], key: str) -> int | None:
    result = value.get(key)
    return result if isinstance(result, int) and not isinstance(result, bool) else None


def _project_sender(value: Any) -> dict[str, str]:
    sender = _mapping(value)
    result = {"kind": _required_string(sender, "kind"), "id": _required_string(sender, "id")}
    display_name = _optional_string(sender, "displayName")
    if display_name is not None:
        result["displayName"] = display_name
    return result


def _project_last_message(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    message = _mapping(value)
    result: dict[str, Any] = {
        "id": _required_string(message, "id"),
        "seq": _optional_int(message, "seq"),
        "preview": _required_string(message, "preview"),
        "senderKind": _required_string(message, "senderKind"),
        "status": _required_string(message, "status"),
        "createdAt": _required_string(message, "createdAt"),
    }
    if result["seq"] is None:
        raise RelayMalformedResponseError()
    sender_name = _optional_string(message, "senderDisplayName")
    if sender_name is not None:
        result["senderDisplayName"] = sender_name
    return result


def project_channel(value: Any) -> dict[str, Any]:
    """Project a channel summary to the Desktop's non-runtime view."""

    channel = _mapping(value)
    result: dict[str, Any] = {
        "id": _required_string(channel, "id"),
        "title": _required_string(channel, "title"),
    }
    for key in ("kind", "visibility"):
        item = _optional_string(channel, key)
        if item is not None:
            result[key] = item
    for key in ("archived",):
        item = channel.get(key)
        if isinstance(item, bool):
            result[key] = item
    for key in ("latestSeq", "messageCount", "threadCount"):
        item = _optional_int(channel, key)
        if item is not None:
            result[key] = item
    if "lastMessage" in channel:
        result["lastMessage"] = _project_last_message(channel["lastMessage"])
    return result


def project_message(value: Any) -> dict[str, Any]:
    """Drop provider/source/runtime/item fields before the Desktop sees a row."""

    message = _mapping(value)
    body = _mapping(message.get("body"))
    result: dict[str, Any] = {
        "schemaVersion": message.get("schemaVersion"),
        "id": _required_string(message, "id"),
        "channelId": _required_string(message, "channelId"),
        "seq": _optional_int(message, "seq"),
        "kind": _required_string(message, "kind"),
        "status": _required_string(message, "status"),
        "sender": _project_sender(message.get("sender")),
        "body": {
            "text": _required_string(body, "text"),
            "format": _required_string(body, "format"),
        },
        "threadId": message.get("threadId"),
        "parentMessageId": message.get("parentMessageId"),
        "createdAt": _required_string(message, "createdAt"),
        "updatedAt": _required_string(message, "updatedAt"),
    }
    if result["schemaVersion"] != 1 or result["seq"] is None:
        raise RelayMalformedResponseError()
    if result["threadId"] is not None and not isinstance(result["threadId"], str):
        raise RelayMalformedResponseError()
    if result["parentMessageId"] is not None and not isinstance(result["parentMessageId"], str):
        raise RelayMalformedResponseError()
    for key in ("replyCount",):
        item = _optional_int(message, key)
        if item is not None:
            result[key] = item
    for key in ("truncated",):
        item = message.get(key)
        if isinstance(item, bool):
            result[key] = item
    for key in ("clientMessageId", "completedAt"):
        item = _optional_string(message, key)
        if item is not None:
            result[key] = item
    attribution = message.get("agentAttribution")
    if isinstance(attribution, Mapping):
        safe_attribution = {
            key: attribution[key]
            for key in ("model", "effort")
            if isinstance(attribution.get(key), str)
        }
        if safe_attribution:
            result["agentAttribution"] = safe_attribution
    return result


def project_channels(payload: Any) -> dict[str, list[dict[str, Any]]]:
    data = _mapping(payload)
    channels = data.get("channels")
    if not isinstance(channels, list):
        raise RelayMalformedResponseError()
    return {"channels": [project_channel(channel) for channel in channels]}


def project_history(payload: Any) -> dict[str, Any]:
    data = _mapping(payload)
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise RelayMalformedResponseError()
    result: dict[str, Any] = {"messages": [project_message(message) for message in messages]}
    if isinstance(data.get("hasMore"), bool):
        result["hasMore"] = data["hasMore"]
    cursor = data.get("nextCursor")
    if isinstance(cursor, Mapping):
        safe_cursor = {
            key: value
            for key in ("beforeSeq", "afterSeq")
            if isinstance((value := cursor.get(key)), int) and not isinstance(value, bool)
        }
        if safe_cursor:
            result["nextCursor"] = safe_cursor
    return result


def project_post(payload: Any) -> dict[str, dict[str, Any]]:
    data = _mapping(payload)
    return {"message": project_message(data.get("message"))}


@dataclass(frozen=True)
class ConnectionStatus:
    status: str
    message: str | None = None

    def to_wire(self) -> dict[str, str]:
        result = {"status": self.status}
        if self.message is not None:
            result["message"] = self.message
        return result


class RelayProxy:
    """Stateful only for an issued credential and unredeemed one-time grant."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_RELAY_IDE_URL,
        credential: str | None = None,
        grant: str | None = None,
        transport: RelayTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configuration_error = False
        try:
            self._base_url = validate_relay_base_url(base_url)
        except RelayConfigurationError:
            self._base_url = ""
            self._configuration_error = True
        self._transport = transport or UrlLibRelayTransport()
        self._clock = clock
        self._configured_credential = credential if credential else None
        self._configured_deadline = (
            self._clock() + OPERATOR_CREDENTIAL_TTL_SECONDS
            if self._configured_credential is not None
            else None
        )
        self._grant = grant if grant else None
        self._issued_credential: str | None = None
        self._issued_deadline: float | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> RelayProxy:
        env = os.environ if environment is None else environment
        return cls(
            base_url=env.get("RELAY_IDE_URL", DEFAULT_RELAY_IDE_URL),
            credential=env.get("RELAY_IDE_OPERATOR_CLIENT_TOKEN"),
            grant=env.get("RELAY_IDE_OPERATOR_GRANT"),
        )

    def _active_credential(self) -> str | None:
        with self._lock:
            if (
                self._configured_credential is not None
                and self._configured_deadline is not None
                and self._clock() >= self._configured_deadline
            ):
                self._configured_credential = None
                self._configured_deadline = None
            if (
                self._issued_credential is not None
                and self._issued_deadline is not None
                and self._clock() >= self._issued_deadline
            ):
                self._issued_credential = None
                self._issued_deadline = None
            return self._issued_credential or self._configured_credential

    def _clear_issued_credential(self, token: str) -> None:
        with self._lock:
            if self._issued_credential == token:
                self._issued_credential = None
                self._issued_deadline = None

    def _url(self, path: str) -> str:
        if self._configuration_error:
            raise RelayConfigurationError()
        return f"{self._base_url}{path}"

    @staticmethod
    def _headers(command: str, token: str, *, write: bool) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "x-relay-operator-client-token": "v1",
            "x-relay-cli-gateway": "v1",
            "x-relay-cli-command": command,
            "x-relay-capabilities": "context:write" if write else "context:read",
        }

    def _channel_request(
        self,
        *,
        method: str,
        command: str,
        path: str,
        write: bool = False,
        body: bytes | None = None,
    ) -> Any:
        token = self._active_credential()
        if not token:
            raise RelayAuthRequiredError()
        headers = self._headers(command, token, write=write)
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self._transport.request(
            method=method,
            url=self._url(path),
            headers=headers,
            body=body,
        )
        if 200 <= response.status_code < 300:
            return response.payload
        if response.status_code in (401, 403):
            self._clear_issued_credential(token)
            raise RelayAuthRequiredError(response.status_code)
        if response.status_code >= 500:
            raise RelayUnavailableError()
        raise RelayUpstreamError(response.status_code)

    def status(self) -> ConnectionStatus:
        if self._configuration_error:
            return ConnectionStatus("error", "Relay URL is invalid")
        if self._active_credential() is None:
            return ConnectionStatus("auth_required", "Relay authorization is required")
        try:
            project_channels(
                self._channel_request(
                    method="GET", command="channels.list", path="/channels"
                )
            )
        except RelayAuthRequiredError:
            return ConnectionStatus("auth_required", "Relay authorization is required")
        except (RelayUnavailableError, RelayResponseTooLargeError):
            return ConnectionStatus("offline", "Relay is unavailable")
        except (RelayConfigurationError, RelayMalformedResponseError, RelayUpstreamError):
            return ConnectionStatus("error", "Relay returned an invalid response")
        return ConnectionStatus("ready")

    def authorize(self) -> ConnectionStatus:
        """Redeem at most one grant; the returned token never leaves this object."""

        if self._configuration_error:
            return ConnectionStatus("error", "Relay URL is invalid")
        if self._active_credential() is not None:
            return self.status()
        with self._lock:
            grant = self._grant
            # Claim before I/O. Retrying a one-time grant could mint or replay a
            # credential, so this process never makes a second redemption attempt.
            self._grant = None
        if grant is None:
            raise RelayAuthRequiredError()
        body = json.dumps(
            {
                "grantHandle": grant,
                "client": OPERATOR_CLIENT_METADATA,
                "capabilities": ["context:read", "context:write"],
                "ttlMs": OPERATOR_CREDENTIAL_TTL_MS,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport.request(
            method="POST",
            url=self._url("/operator-client-credentials"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body=body,
        )
        if response.status_code < 200 or response.status_code >= 300:
            if 400 <= response.status_code < 500:
                raise RelayAuthRequiredError(response.status_code)
            raise RelayUnavailableError()
        issued = _mapping(response.payload)
        token = issued.get("token")
        if not isinstance(token, str) or not token.startswith("relay-occ-v1."):
            raise RelayMalformedResponseError()
        # Validate that this was the documented issue response without retaining
        # metadata. The token and credential record are intentionally discarded
        # from every route result and never written to disk.
        _mapping(issued.get("credential"))
        with self._lock:
            self._issued_credential = token
            self._issued_deadline = self._clock() + OPERATOR_CREDENTIAL_TTL_SECONDS
        return ConnectionStatus("ready")

    def list_channels(self) -> dict[str, list[dict[str, Any]]]:
        return project_channels(
            self._channel_request(method="GET", command="channels.list", path="/channels")
        )

    def history(self, channel_id: str, limit: int) -> dict[str, Any]:
        encoded = quote(channel_id, safe="")
        return project_history(
            self._channel_request(
                method="GET",
                command="channels.history",
                path=f"/channels/{encoded}/messages?limit={limit}",
            )
        )

    def post(self, channel_id: str, body: Mapping[str, str]) -> dict[str, dict[str, Any]]:
        encoded = quote(channel_id, safe="")
        wire_body = json.dumps(
            {
                "text": body["text"],
                "format": body["format"],
                "clientMessageId": body["clientMessageId"],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return project_post(
            self._channel_request(
                method="POST",
                command="channels.post",
                path=f"/channels/{encoded}/messages",
                write=True,
                body=wire_body,
            )
        )


_proxy_lock = threading.Lock()
_proxy: RelayProxy | None = None


def get_relay_proxy() -> RelayProxy:
    """Return the one process-local credential holder for this plugin package."""

    global _proxy
    with _proxy_lock:
        if _proxy is None:
            _proxy = RelayProxy.from_environment()
        return _proxy


def reset_relay_proxy_for_tests(proxy: RelayProxy | None = None) -> None:
    """Test-only seam; production never persists or swaps credentials."""

    global _proxy
    with _proxy_lock:
        _proxy = proxy


__all__ = [
    "DEFAULT_RELAY_IDE_URL",
    "MAX_CHANNEL_ID_BYTES",
    "MAX_CLIENT_MESSAGE_ID_BYTES",
    "MAX_HISTORY_LIMIT",
    "MAX_MESSAGE_TEXT_BYTES",
    "MAX_RELAY_RESPONSE_BYTES",
    "OPERATOR_CLIENT_METADATA",
    "OPERATOR_CREDENTIAL_TTL_MS",
    "ConnectionStatus",
    "RelayAuthRequiredError",
    "RelayConfigurationError",
    "RelayHttpResponse",
    "RelayMalformedResponseError",
    "RelayProxy",
    "RelayProxyError",
    "RelayResponseTooLargeError",
    "RelayUnavailableError",
    "RelayUpstreamError",
    "UrlLibRelayTransport",
    "get_relay_proxy",
    "project_channel",
    "project_channels",
    "project_history",
    "project_message",
    "project_post",
    "reset_relay_proxy_for_tests",
    "validate_relay_base_url",
]
