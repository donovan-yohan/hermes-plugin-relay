"""Loopback-only, credential-owning proxy for Relay's stable channel API plus
its read-only native harness-session lane.

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
from datetime import datetime
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
ACTOR_CREDENTIAL_TTL_MS = 15 * 60 * 1000
ACTOR_AUDIENCE = "relay:cli-gateway:v1"
# Relay stamps this permissive read marker into every session:read actor
# credential's scope; a grant must cover it for the mint to succeed.
ACTOR_READ_TASK_REF = "relay:cli-gateway:v1:read"
OPERATOR_CLIENT_METADATA = {
    "id": "desktop-plugin-backend",
    "displayName": "Desktop plugin backend",
    "platform": "linux",
}
ACTOR_CLIENT_ID = "desktop-plugin-harness-view"
NATIVE_PROVIDERS = ("claude", "codex", "hermes", "opencode", "pi", "prime-agent", "dsh", "antigravity")
MAX_HARNESS_SESSIONS = 200
MAX_SESSION_ID_BYTES = 512
# `relay-ide login` (#1435) stores the scoped actor credential at this path with
# chmod 600. The plugin reads it as a third token source (after env/flag) so a
# machine that already ran `relay-ide login` works with zero plugin config.
RELAY_ACTOR_TOKEN_FILE = os.path.expanduser("~/.config/relay-ide/actor-token.json")
# Renew when <120s remain, matching the CLI's own threshold.
ACTOR_RENEW_MARGIN_SECONDS = 120
ACTOR_RENEW_COMMAND = "actor-credentials.renew"
ACTOR_FILE_RECHECK_SECONDS = 5
ACTOR_LOGIN_DISPLAY_NAME = "Relay desktop plugin"
MAX_LOGIN_FLOW_ID_BYTES = 256


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


def validate_relay_public_url(value: str) -> str:
    """Validate a display-only browser origin used for Relay login approval."""

    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            for character in value
        )
    ):
        raise RelayConfigurationError()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise RelayConfigurationError() from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RelayConfigurationError()
    host = parsed.hostname
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme}://{authority}"


def _iso_timestamp(value: Any) -> float:
    if not isinstance(value, str):
        raise RelayMalformedResponseError()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.timestamp()
    except ValueError as exc:
        raise RelayMalformedResponseError() from exc


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


def _project_preview(block: Any) -> tuple[str, bool]:
    """Extract the shared redacted preview pair from a native-session block."""

    if not isinstance(block, Mapping):
        return "", False
    text_value = block.get("text")
    preview = text_value if isinstance(text_value, str) else ""
    return preview, block.get("redacted") is True


def project_harness_rows(payload: Any) -> list[dict[str, Any]]:
    """Project the native-session registry report into ordered harness rows."""

    data = _mapping(payload)
    sessions = data.get("sessions")
    providers = data.get("providers")
    if not isinstance(sessions, list) or not isinstance(providers, list):
        raise RelayMalformedResponseError()
    counts: dict[str, int] = {}
    for row in sessions:
        summary = _mapping(row)
        provider = summary.get("provider")
        if not isinstance(provider, str):
            raise RelayMalformedResponseError()
        counts[provider] = counts.get(provider, 0) + 1
    statuses: dict[str, Mapping[str, Any]] = {}
    for row in providers:
        item = _mapping(row)
        provider = item.get("provider")
        status = item.get("status")
        if not isinstance(provider, str) or status not in (
            "installed",
            "unavailable",
            "unsupported",
        ):
            raise RelayMalformedResponseError()
        statuses[provider] = item
    rows: list[dict[str, Any]] = []
    for provider in NATIVE_PROVIDERS:
        item = statuses.get(provider)
        if item is None:
            continue
        row: dict[str, Any] = {
            "provider": provider,
            "status": item["status"],
            "sessionCount": counts.get(provider, 0),
        }
        version = item.get("version")
        if isinstance(version, str) and version:
            row["version"] = version
        rows.append(row)
    return rows


def project_native_session_summaries(payload: Any) -> list[dict[str, Any]]:
    """Project native session summaries into bounded, renderer-safe rows."""

    data = _mapping(payload)
    sessions = data.get("sessions")
    if not isinstance(sessions, list):
        raise RelayMalformedResponseError()
    projected: list[dict[str, Any]] = []
    for row in sessions[:MAX_HARNESS_SESSIONS]:
        summary = _mapping(row)
        provider = summary.get("provider")
        native_id = summary.get("nativeId")
        if not isinstance(provider, str) or not isinstance(native_id, str) or not native_id:
            raise RelayMalformedResponseError()
        preview, redacted = _project_preview(summary.get("preview"))
        title = summary.get("title")
        cwd = summary.get("cwd")
        stamp = summary.get("lastMessageAt")
        if not isinstance(stamp, str):
            stamp = summary.get("updatedAt")
        if not isinstance(stamp, str):
            stamp = summary.get("createdAt")
        if not isinstance(stamp, str):
            stamp = ""
        capabilities = summary.get("capabilities")
        can_watch = (
            isinstance(capabilities, Mapping)
            and capabilities.get("canStreamLiveEvents") is True
        )
        projected.append(
            {
                "id": native_id,
                "provider": provider,
                "title": title if isinstance(title, str) else "",
                "cwd": cwd if isinstance(cwd, str) else "",
                "preview": preview,
                "redacted": redacted,
                # ISO-8601 stamps sort lexicographically; empty sorts oldest.
                "updatedAt": stamp,
                "canWatch": can_watch,
            }
        )
    projected.sort(key=lambda row: row["updatedAt"], reverse=True)
    return projected


def project_native_session_snapshot(payload: Any) -> dict[str, Any]:
    """Project one bounded provider-state snapshot for renderer display."""

    data = _mapping(payload)
    snapshot = _mapping(data.get("snapshot"))
    ref = _mapping(snapshot.get("ref"))
    summary = _mapping(snapshot.get("summary"))
    captured_at = snapshot.get("capturedAt")
    native_id = ref.get("nativeId")
    provider = ref.get("provider")
    if (
        not isinstance(captured_at, str)
        or not isinstance(native_id, str)
        or not native_id
        or not isinstance(provider, str)
    ):
        raise RelayMalformedResponseError()
    preview, redacted = _project_preview(summary.get("preview"))
    result: dict[str, Any] = {
        "id": native_id,
        "provider": provider,
        "capturedAt": captured_at,
        "preview": preview,
        "redacted": redacted,
    }
    for key in ("lineCount", "byteCount"):
        value = summary.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            result[key] = value
    event_types = summary.get("eventTypes")
    if isinstance(event_types, list):
        names = [name for name in event_types if isinstance(name, str)]
        result["eventTypes"] = names[:12]
    return result


@dataclass(frozen=True)
class ConnectionStatus:
    status: str
    message: str | None = None

    def to_wire(self) -> dict[str, str]:
        result = {"status": self.status}
        if self.message is not None:
            result["message"] = self.message
        return result


@dataclass(frozen=True)
class HarnessLoginFlow:
    flow_id: str
    code: str
    expires_at: str
    deadline: float
    verification_url: str

    def to_wire(self) -> dict[str, str]:
        return {
            "status": "pending",
            "code": self.code,
            "expiresAt": self.expires_at,
            "verificationUrl": self.verification_url,
        }


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
            return ConnectionStatus("auth_required", "Channel authorization is required")
        try:
            project_channels(
                self._channel_request(
                    method="GET", command="channels.list", path="/channels"
                )
            )
        except RelayAuthRequiredError:
            return ConnectionStatus("auth_required", "Channel authorization is required")
        except (RelayUnavailableError, RelayResponseTooLargeError):
            return ConnectionStatus("offline", "Relay is unavailable")
        except (RelayConfigurationError, RelayMalformedResponseError, RelayUpstreamError):
            return ConnectionStatus("error", "Relay returned an invalid response")
        return ConnectionStatus("ready")

    def onboarding_url(self) -> str:
        if self._configuration_error:
            raise RelayConfigurationError()
        return f"{self._base_url}/"

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


class ActorLaneClient:
    """Scoped actor-token lane client for read-only native harness sessions.

    Deliberately separate from ``RelayProxy``: the two credential families have
    different audiences, capabilities, and failure domains, and mixing them
    would let a channels-only credential drift into session reads or vice
    versa. Tokens stay inside this process, exactly like the operator-client
    credential.

    Token resolution precedence: env ``RELAY_IDE_ACTOR_TOKEN`` > a one-time
    ``RELAY_IDE_ACTOR_GRANT`` > the ``relay-ide login`` credential file at
    ``~/.config/relay-ide/actor-token.json`` (#1435). A token from any source
    is renewed ~2 min before expiry via ``POST /cli-gateway/actor-credentials/
    renew``; the predecessor is never revoked (it expires naturally), so a
    lost renew response cannot lock the plugin out.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_RELAY_IDE_URL,
        public_url: str | None = None,
        actor_token: str | None = None,
        grant: str | None = None,
        token_file: str | None = RELAY_ACTOR_TOKEN_FILE,
        transport: RelayTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configuration_error = False
        try:
            self._base_url = validate_relay_base_url(base_url)
        except RelayConfigurationError:
            self._base_url = ""
            self._configuration_error = True
        self._login_configuration_error = False
        if public_url:
            try:
                self._approval_base_url = validate_relay_public_url(public_url)
            except RelayConfigurationError:
                self._approval_base_url = ""
                self._login_configuration_error = True
        else:
            self._approval_base_url = self._base_url
        self._transport = transport or UrlLibRelayTransport()
        self._clock = clock
        self._configured_token = actor_token if actor_token else None
        self._grant = grant if grant else None
        self._token_file = token_file
        # A configured environment token's real TTL is unknown; bound it to the
        # standard ceiling so stale tokens fail closed here too.
        self._configured_deadline = (
            self._clock() + ACTOR_CREDENTIAL_TTL_MS / 1000
            if self._configured_token is not None
            else None
        )
        self._file_token: str | None = None
        self._file_deadline: float | None = None
        self._file_checked = False
        self._file_next_check = 0.0
        self._issued_token: str | None = None
        self._issued_deadline: float | None = None
        self._lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._login_flow: HarnessLoginFlow | None = None

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ActorLaneClient:
        env = os.environ if environment is None else environment
        return cls(
            base_url=env.get("RELAY_IDE_URL", DEFAULT_RELAY_IDE_URL),
            public_url=env.get("RELAY_IDE_PUBLIC_URL"),
            actor_token=env.get("RELAY_IDE_ACTOR_TOKEN"),
            grant=env.get("RELAY_IDE_ACTOR_GRANT"),
        )

    def _load_file_token(self) -> tuple[str | None, float | None]:
        """Read the `relay-ide login` credential file under a short cache.

        Returns ``(token, deadline)`` or ``(None, None)``. The file's own
        ``expiresAt`` is authoritative; a missing/expired/loose-perms file
        yields nothing, and the caller fails closed.

        The deadline is a Unix-epoch timestamp (parsed from the file's
        ``expiresAt``); it is compared against wall-clock time, not the
        process-local monotonic clock used for internally-managed deadlines.
        """

        if not self._token_file:
            return None, None
        try:
            with open(self._token_file, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except OSError:
            return None, None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
        if not isinstance(parsed, dict):
            return None, None
        token = parsed.get("token")
        expires_at = parsed.get("expiresAt")
        if not isinstance(token, str) or not token.startswith("relay-sac-v1."):
            return None, None
        if not isinstance(expires_at, str):
            return None, None
        try:
            deadline = _iso_timestamp(expires_at)
        except RelayMalformedResponseError:
            return None, None
        if deadline <= time.time():
            return None, None
        return token, deadline

    def _renew_locked(self, token: str) -> None:
        """Renew a still-valid token ~2 min before expiry (#1435).

        Caller MUST hold ``self._lock``. The hub mints a successor with the
        same actor/capabilities/scope and does NOT revoke the predecessor,
        so a lost response can't lock us out.
        """

        renew_body = json.dumps(
            {"correlationId": f"{ACTOR_CLIENT_ID}-renew"},
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport.request(
            method="POST",
            url=self._url("/cli-gateway/actor-credentials/renew"),
            headers=self._headers(ACTOR_RENEW_COMMAND, token),
            body=renew_body,
        )
        if response.status_code < 200 or response.status_code >= 300:
            return
        issued = _mapping(response.payload)
        new_token = issued.get("token")
        if not isinstance(new_token, str) or not new_token.startswith("relay-sac-v1."):
            return
        new_credential = _mapping(issued.get("credential"))
        expires_at = new_credential.get("expiresAt")
        new_deadline = None
        try:
            wall_deadline = _iso_timestamp(expires_at)
            # Convert wall-clock expiry to the monotonic domain so every
            # consumer of _issued_deadline can compare against _clock().
            new_deadline = self._clock() + max(0, wall_deadline - time.time())
        except RelayMalformedResponseError:
            pass
        if new_deadline is None:
            new_deadline = self._clock() + ACTOR_CREDENTIAL_TTL_MS / 1000
        self._issued_token = new_token
        self._issued_deadline = new_deadline

    def _active_token(self) -> str | None:
        with self._lock:
            if (
                self._configured_token is not None
                and self._configured_deadline is not None
                and self._clock() >= self._configured_deadline
            ):
                self._configured_token = None
                self._configured_deadline = None
            if (
                self._issued_token is not None
                and self._issued_deadline is not None
                and self._clock() >= self._issued_deadline
            ):
                self._issued_token = None
                self._issued_deadline = None
            # Re-check a missing login file on a short cadence so running
            # `relay-ide login` after Desktop starts does not require a restart.
            if not self._file_checked or (
                self._file_token is None and self._clock() >= self._file_next_check
            ):
                file_token, file_deadline = self._load_file_token()
                self._file_token = file_token
                self._file_deadline = file_deadline
                self._file_checked = True
                self._file_next_check = self._clock() + ACTOR_FILE_RECHECK_SECONDS
            if (
                self._file_token is not None
                and self._file_deadline is not None
                and time.time() >= self._file_deadline
            ):
                self._file_token = None
                self._file_deadline = None
                # Allow a re-read: the operator may have run
                # `relay-ide login` again while the old file token was live.
                self._file_checked = False
                self._file_next_check = 0.0
            return self._issued_token or self._configured_token or self._file_token

    def _clear_issued_token(self, token: str) -> None:
        with self._lock:
            if self._issued_token == token:
                self._issued_token = None
                self._issued_deadline = None

    def _url(self, path: str) -> str:
        if self._configuration_error:
            raise RelayConfigurationError()
        return f"{self._base_url}{path}"

    @staticmethod
    def _headers(command: str, token: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "x-relay-cli-gateway": "v1",
            "x-relay-cli-command": command,
            # The versioned actor marker selects the scoped actor lane before
            # any bearer-prefix sniffing happens on the hub.
            "x-relay-cli-actor-token": "v1",
            "x-relay-capabilities": "session:read",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _actor_request(
        self,
        *,
        command: str,
        method: str,
        path: str,
        body: bytes | None = None,
    ) -> Any:
        token = self._active_token()
        if not token:
            raise RelayAuthRequiredError()
        # Auto-renew ~2 min before expiry so a long-lived desktop session
        # never silently drops into auth_required mid-poll. After a successful
        # renewal the issued token supersedes the one we captured above.
        self._maybe_renew(token)
        token = self._active_token() or token
        headers = self._headers(command, token)
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
            self._clear_issued_token(token)
            raise RelayAuthRequiredError(response.status_code)
        if response.status_code >= 500:
            raise RelayUnavailableError()
        raise RelayUpstreamError(response.status_code)

    def _maybe_renew(self, token: str) -> None:
        """Renew if the active token is within the margin of expiry.

        Single-flight: the lock is held across the margin check AND the
        renew I/O so two concurrent poll threads can't both pass the check
        and mint duplicate successors.
        """

        with self._lock:
            if token == self._issued_token and self._issued_deadline is not None:
                remaining = self._issued_deadline - self._clock()
            elif token == self._file_token and self._file_deadline is not None:
                remaining = self._file_deadline - time.time()
            elif token == self._configured_token and self._configured_deadline is not None:
                remaining = self._configured_deadline - self._clock()
            else:
                return
            if remaining > ACTOR_RENEW_MARGIN_SECONDS:
                return
            # Hold the lock through the I/O so a concurrent caller
            # sees the renewed token and skips its own renew.
            try:
                self._renew_locked(token)
            except (RelayUnavailableError, RelayUpstreamError, RelayMalformedResponseError):
                pass

    def authorize(self) -> ConnectionStatus:
        """Redeem at most one grant; the returned token never leaves here.

        If a ``relay-ide login`` file or an environment token is already
        live, this is a no-op that reports ``ready`` without any I/O.
        """

        if self._configuration_error:
            return ConnectionStatus("error", "Relay URL is invalid")
        if self._active_token() is not None:
            return ConnectionStatus("ready")
        with self._lock:
            grant = self._grant
            # Claim before I/O so a retry can never replay the one-time handle.
            self._grant = None
        if grant is None:
            raise RelayAuthRequiredError()
        issue_body = json.dumps(
            {
                "grantHandle": grant,
                "audience": ACTOR_AUDIENCE,
                "actor": {"type": "cli", "id": ACTOR_CLIENT_ID},
                "capabilities": ["session:read"],
                "ttlMs": ACTOR_CREDENTIAL_TTL_MS,
                "scope": {"taskRefs": [ACTOR_READ_TASK_REF]},
                "correlationId": f"{ACTOR_CLIENT_ID}-authorize",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        response = self._transport.request(
            method="POST",
            url=self._url("/cli-gateway/actor-credentials"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            body=issue_body,
        )
        if response.status_code < 200 or response.status_code >= 300:
            if 400 <= response.status_code < 500:
                raise RelayAuthRequiredError(response.status_code)
            raise RelayUnavailableError()
        issued = _mapping(response.payload)
        token = issued.get("token")
        if not isinstance(token, str) or not token.startswith("relay-sac-v1."):
            raise RelayMalformedResponseError()
        _mapping(issued.get("credential"))
        with self._lock:
            self._issued_token = token
            self._issued_deadline = self._clock() + ACTOR_CREDENTIAL_TTL_MS / 1000
        return ConnectionStatus("ready")

    def status(self) -> ConnectionStatus:
        """Probe only the read-only harness lane with its own credential."""

        if self._configuration_error:
            return ConnectionStatus("error", "Relay URL is invalid")
        if self._active_token() is None:
            if self._login_configuration_error:
                return ConnectionStatus("error", "Relay public approval URL is invalid")
            return ConnectionStatus("auth_required", "Harness authorization is required")
        try:
            self.harnesses()
        except RelayAuthRequiredError:
            return ConnectionStatus("auth_required", "Harness authorization is required")
        except (RelayUnavailableError, RelayResponseTooLargeError):
            return ConnectionStatus("offline", "Relay is unavailable")
        except (RelayConfigurationError, RelayMalformedResponseError, RelayUpstreamError):
            return ConnectionStatus("error", "Relay returned an invalid response")
        return ConnectionStatus("ready")

    def login_available(self) -> bool:
        return not self._configuration_error and not self._login_configuration_error

    def accept_login_credential(self, token: Any, credential: Any) -> None:
        """Install one device-flow actor token without exposing it to callers."""

        if not isinstance(token, str) or not token.startswith("relay-sac-v1."):
            raise RelayMalformedResponseError()
        record = _mapping(credential)
        wall_deadline = _iso_timestamp(record.get("expiresAt"))
        remaining = wall_deadline - time.time()
        if remaining <= 0:
            raise RelayMalformedResponseError()
        with self._lock:
            self._issued_token = token
            self._issued_deadline = self._clock() + remaining

    def start_login(self) -> dict[str, str]:
        """Start Relay's browser/PIN flow and return only public approval data."""

        if not self.login_available():
            raise RelayConfigurationError()
        if self._active_token() is not None:
            return {"status": "ready"}
        with self._login_lock:
            if self._active_token() is not None:
                return {"status": "ready"}
            body = json.dumps(
                {
                    "actorId": ACTOR_CLIENT_ID,
                    "displayName": ACTOR_LOGIN_DISPLAY_NAME,
                    "capabilities": ["session:read"],
                },
                separators=(",", ":"),
            ).encode("utf-8")
            response = self._transport.request(
                method="POST",
                url=self._url("/cli-gateway/login/start"),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                body=body,
            )
            if response.status_code < 200 or response.status_code >= 300:
                if response.status_code >= 500:
                    raise RelayUnavailableError()
                raise RelayUpstreamError(response.status_code)
            payload = _mapping(response.payload)
            flow_id = _required_string(payload, "flowId")
            code = _required_string(payload, "code")
            expires_at = _required_string(payload, "expiresAt")
            _required_string(payload, "verificationUrl")
            deadline = _iso_timestamp(expires_at)
            code_body = code[:4] + code[5:]
            if (
                len(flow_id.encode("utf-8")) > MAX_LOGIN_FLOW_ID_BYTES
                or "/" in flow_id
                or deadline <= time.time()
                or len(code) != 9
                or code[4:5] != "-"
                or not all(
                    character.isascii() and character.isalnum()
                    for character in code_body
                )
                or code != code.upper()
            ):
                raise RelayMalformedResponseError()
            flow = HarnessLoginFlow(
                flow_id=flow_id,
                code=code,
                expires_at=expires_at,
                deadline=deadline,
                verification_url=(
                    f"{self._approval_base_url}/cli-gateway/login/"
                    f"{quote(flow_id, safe='')}/approve"
                ),
            )
            self._login_flow = flow
            return flow.to_wire()

    def poll_login(self) -> dict[str, str]:
        """Poll one flow; capture an approved token exactly once in-process."""

        with self._login_lock:
            if self._active_token() is not None:
                self._login_flow = None
                return {"status": "ready"}
            flow = self._login_flow
            if flow is None:
                return {"status": "idle"}
            if time.time() >= flow.deadline:
                self._login_flow = None
                return {"status": "expired", "message": "The Relay login code expired."}
            response = self._transport.request(
                method="GET",
                url=self._url(f"/cli-gateway/login/{quote(flow.flow_id, safe='')}"),
                headers={"Accept": "application/json"},
                body=None,
            )
            if response.status_code == 404:
                self._login_flow = None
                return {"status": "expired", "message": "The Relay login code expired."}
            if response.status_code < 200 or response.status_code >= 300:
                if response.status_code >= 500:
                    raise RelayUnavailableError()
                raise RelayUpstreamError(response.status_code)
            payload = _mapping(response.payload)
            status = _required_string(payload, "status")
            if status == "pending":
                return flow.to_wire()
            if status == "approved":
                # Relay returns this token once. Clear the flow before validating
                # so malformed one-shot material can never be polled/replayed.
                self._login_flow = None
                self.accept_login_credential(payload.get("token"), payload.get("credential"))
                return {"status": "ready"}
            if status in {"denied", "expired", "consumed"}:
                self._login_flow = None
                messages = {
                    "denied": "Relay login was denied.",
                    "expired": "The Relay login code expired.",
                    "consumed": "This Relay login was already used. Start again.",
                }
                return {"status": status, "message": messages[status]}
            raise RelayMalformedResponseError()

    def cancel_login(self) -> dict[str, str]:
        """Forget the local flow; Relay expires the orphaned flow shortly."""

        with self._login_lock:
            self._login_flow = None
        return {"status": "ready" if self._active_token() is not None else "idle"}

    def harnesses(self) -> list[dict[str, Any]]:
        """Per-harness install rows with live session counts, hub-ordered."""

        return project_harness_rows(
            self._actor_request(
                command="sessions.native.list", method="GET", path="/sessions/native"
            )
        )

    def harness_sessions(self, provider: str) -> list[dict[str, Any]]:
        """Bounded session summaries for one provider, newest first."""

        if provider not in NATIVE_PROVIDERS:
            raise RelayConfigurationError()
        encoded = quote(provider, safe="")
        return project_native_session_summaries(
            self._actor_request(
                command="sessions.native.list",
                method="GET",
                path=f"/sessions/native?provider={encoded}",
            )
        )

    def harness_session(self, provider: str, native_id: str) -> dict[str, Any]:
        """One bounded provider-state snapshot."""

        if provider not in NATIVE_PROVIDERS:
            raise RelayConfigurationError()
        if not native_id or len(native_id.encode("utf-8")) > MAX_SESSION_ID_BYTES:
            raise RelayConfigurationError()
        encoded_provider = quote(provider, safe="")
        encoded_id = quote(native_id, safe="")
        return project_native_session_snapshot(
            self._actor_request(
                command="sessions.native.get",
                method="GET",
                path=f"/sessions/native/{encoded_provider}/{encoded_id}",
            )
        )


_actor_lane_lock = threading.Lock()
_actor_lane: ActorLaneClient | None = None


def get_actor_lane() -> ActorLaneClient:
    """Return the one process-local actor-lane client for this package."""

    global _actor_lane
    with _actor_lane_lock:
        if _actor_lane is None:
            _actor_lane = ActorLaneClient.from_environment()
        return _actor_lane


def reset_actor_lane_for_tests(lane: ActorLaneClient | None = None) -> None:
    """Test-only seam; production never persists or swaps credentials."""

    global _actor_lane
    with _actor_lane_lock:
        _actor_lane = lane


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
    "ACTOR_AUDIENCE",
    "ACTOR_CREDENTIAL_TTL_MS",
    "ACTOR_FILE_RECHECK_SECONDS",
    "ACTOR_CLIENT_ID",
    "ACTOR_LOGIN_DISPLAY_NAME",
    "ACTOR_READ_TASK_REF",
    "ACTOR_RENEW_COMMAND",
    "ACTOR_RENEW_MARGIN_SECONDS",
    "ActorLaneClient",
    "DEFAULT_RELAY_IDE_URL",
    "MAX_CHANNEL_ID_BYTES",
    "MAX_CLIENT_MESSAGE_ID_BYTES",
    "MAX_HARNESS_SESSIONS",
    "MAX_HISTORY_LIMIT",
    "MAX_LOGIN_FLOW_ID_BYTES",
    "MAX_MESSAGE_TEXT_BYTES",
    "MAX_RELAY_RESPONSE_BYTES",
    "MAX_SESSION_ID_BYTES",
    "NATIVE_PROVIDERS",
    "OPERATOR_CLIENT_METADATA",
    "OPERATOR_CREDENTIAL_TTL_MS",
    "RELAY_ACTOR_TOKEN_FILE",
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
    "get_actor_lane",
    "get_relay_proxy",
    "project_channel",
    "project_channels",
    "project_harness_rows",
    "project_history",
    "project_message",
    "project_native_session_snapshot",
    "project_native_session_summaries",
    "project_post",
    "reset_actor_lane_for_tests",
    "reset_relay_proxy_for_tests",
    "validate_relay_base_url",
    "validate_relay_public_url",
]
