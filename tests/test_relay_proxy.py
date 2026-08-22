"""Unit coverage for Relay connection, projection, and credential boundaries."""

from __future__ import annotations

import json
from io import BytesIO

import pytest
from conftest import RecordingTransport
from hermes_plugin_relay.relay_proxy import (
    MAX_RELAY_RESPONSE_BYTES,
    OPERATOR_CLIENT_METADATA,
    RelayAuthRequiredError,
    RelayConfigurationError,
    RelayHttpResponse,
    RelayMalformedResponseError,
    RelayProxy,
    RelayResponseTooLargeError,
    RelayUnavailableError,
    _decode_json,
    _read_limited,
    project_history,
    validate_relay_base_url,
)


def channel_list() -> dict:
    return {
        "channels": [
            {
                "id": "topic:one",
                "title": "One",
                "latestSeq": 3,
                "messageCount": 2,
                "lastMessage": {
                    "id": "chm:latest",
                    "seq": 3,
                    "preview": "hello",
                    "senderKind": "human",
                    "senderId": "human:operator",
                    "status": "completed",
                    "createdAt": "2026-01-01T00:00:00.000Z",
                },
                "members": [{"id": "human:operator"}],
            }
        ]
    }


def message(message_id: str = "chm:one") -> dict:
    return {
        "schemaVersion": 1,
        "id": message_id,
        "channelId": "topic:one",
        "seq": 1,
        "kind": "message",
        "status": "completed",
        "sender": {
            "kind": "agent",
            "id": "agent:one",
            "displayName": "Agent One",
            "runtimeId": "runtime-private",
        },
        "body": {"text": "hello", "format": "markdown"},
        "threadId": None,
        "parentMessageId": None,
        "source": {"runtimeId": "runtime-private", "turnId": "turn-private"},
        "agentDetail": {"itemId": "item-private"},
        "asyncRun": {"id": "chrun:private"},
        "meta": {"sourceRuntimeId": "runtime-private"},
        "clientMessageId": "desktop:1",
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:3456", "http://localhost:3456"),
        ("http://127.0.0.1", "http://127.0.0.1"),
        ("http://[::1]:3456/", "http://[::1]:3456"),
    ],
)
def test_loopback_root_urls_are_accepted(url, expected):
    assert validate_relay_base_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:3456",
        "http://relay.example:3456",
        "http://user@127.0.0.1:3456",
        "http://127.0.0.1:3456/api",
        "http://127.0.0.1:3456/?q=1",
        "http://127.0.0.1:3456/#fragment",
    ],
)
def test_non_loopback_or_non_root_urls_are_rejected(url):
    with pytest.raises(RelayConfigurationError):
        validate_relay_base_url(url)


def test_status_classifies_ready_offline_auth_and_invalid_url_without_connecting():
    ready_transport = RecordingTransport(
        lambda **_call: RelayHttpResponse(200, channel_list())
    )
    ready = RelayProxy(credential="configured-value", transport=ready_transport)
    assert ready.status().to_wire() == {"status": "ready"}
    assert ready_transport.calls[0]["headers"]["x-relay-cli-command"] == "channels.list"

    offline = RelayProxy(
        credential="configured-value",
        transport=RecordingTransport(lambda **_call: (_ for _ in ()).throw(RelayUnavailableError())),
    )
    assert offline.status().to_wire()["status"] == "offline"

    no_credential_transport = RecordingTransport(
        lambda **_call: RelayHttpResponse(200, channel_list())
    )
    assert RelayProxy(transport=no_credential_transport).status().to_wire()["status"] == "auth_required"
    assert no_credential_transport.calls == []

    invalid = RelayProxy(base_url="https://127.0.0.1:3456", credential="configured-value")
    assert invalid.status().to_wire()["status"] == "error"


def test_channel_headers_are_backend_only_and_never_enter_url_or_projection():
    marker = "configured-value"
    transport = RecordingTransport(lambda **_call: RelayHttpResponse(200, channel_list()))
    proxy = RelayProxy(credential=marker, transport=transport)

    payload = proxy.list_channels()
    call = transport.calls[0]
    assert call["url"] == "http://127.0.0.1:3456/channels"
    assert call["headers"] == {
        "Accept": "application/json",
        "Authorization": f"Bearer {marker}",
        "x-relay-operator-client-token": "v1",
        "x-relay-cli-gateway": "v1",
        "x-relay-cli-command": "channels.list",
        "x-relay-capabilities": "context:read",
    }
    wire = json.dumps(payload)
    assert marker not in call["url"]
    assert marker not in wire
    assert "members" not in wire
    assert "senderId" not in wire


def test_grant_is_redeemed_once_and_issued_credential_has_a_fifteen_minute_ceiling():
    clock = [100.0]

    def now() -> float:
        return clock[0]

    def handler(**call):
        assert call["method"] == "POST"
        assert call["url"].endswith("/operator-client-credentials")
        issued = json.loads(call["body"])
        assert issued["client"] == OPERATOR_CLIENT_METADATA
        assert issued["capabilities"] == ["context:read", "context:write"]
        assert issued["ttlMs"] == 900000
        return RelayHttpResponse(201, {"token": "relay-occ-v1.test.value", "credential": {"id": "occ:test"}})

    transport = RecordingTransport(handler)
    proxy = RelayProxy(grant="approved-one-time-value", transport=transport, clock=now)
    assert proxy.authorize().to_wire() == {"status": "ready"}
    assert len(transport.calls) == 1

    clock[0] += 900
    assert proxy.status().to_wire()["status"] == "auth_required"
    assert len(transport.calls) == 1
    with pytest.raises(RelayAuthRequiredError):
        proxy.authorize()
    assert len(transport.calls) == 1


def test_environment_credential_is_also_cleared_at_the_fifteen_minute_ceiling():
    clock = [0.0]
    transport = RecordingTransport(lambda **_call: pytest.fail("expired credential must not be sent"))
    proxy = RelayProxy(credential="configured-value", transport=transport, clock=lambda: clock[0])
    clock[0] += 900
    assert proxy.status().status == "auth_required"
    assert transport.calls == []


def test_relay_401_or_403_clears_an_issued_credential_and_reports_auth_required():
    calls = [0]

    def handler(**_call):
        calls[0] += 1
        if calls[0] == 1:
            return RelayHttpResponse(201, {"token": "relay-occ-v1.test.value", "credential": {"id": "occ:test"}})
        return RelayHttpResponse(403, None)

    proxy = RelayProxy(grant="approved-one-time-value", transport=RecordingTransport(handler))
    assert proxy.authorize().status == "ready"
    assert proxy.status().status == "auth_required"
    assert proxy.status().status == "auth_required"
    assert calls[0] == 2


def test_history_projection_strips_runtime_turn_item_source_and_metadata():
    payload = project_history({"messages": [message()], "hasMore": True, "nextCursor": {"beforeSeq": 1}})
    wire = json.dumps(payload)
    for private in ("runtime-private", "turn-private", "item-private", "asyncRun", "meta", "source"):
        assert private not in wire
    assert payload["messages"][0]["sender"] == {
        "kind": "agent",
        "id": "agent:one",
        "displayName": "Agent One",
    }
    assert payload["nextCursor"] == {"beforeSeq": 1}


def test_history_and_post_forward_stable_schema_once_with_exact_client_message_id():
    def handler(**call):
        if call["method"] == "GET":
            return RelayHttpResponse(200, {"messages": [message()]})
        return RelayHttpResponse(201, {"message": message("chm:posted"), "run": {"id": "chrun:private"}})

    transport = RecordingTransport(handler)
    proxy = RelayProxy(credential="configured-value", transport=transport)
    assert proxy.history("topic:one", 50)["messages"][0]["id"] == "chm:one"
    result = proxy.post(
        "topic:one",
        {"text": "do not trim  ", "format": "markdown", "clientMessageId": "desktop:retry:1"},
    )
    assert result["message"]["id"] == "chm:posted"
    assert len(transport.calls) == 2
    assert transport.calls[0]["url"].endswith("/channels/topic%3Aone/messages?limit=50")
    post = transport.calls[1]
    assert post["headers"]["x-relay-cli-command"] == "channels.post"
    assert post["headers"]["x-relay-capabilities"] == "context:write"
    assert json.loads(post["body"]) == {
        "text": "do not trim  ",
        "format": "markdown",
        "clientMessageId": "desktop:retry:1",
    }
    assert "run" not in result


def test_post_does_not_retry_a_recoverable_upstream_failure():
    transport = RecordingTransport(lambda **_call: RelayHttpResponse(503, None))
    proxy = RelayProxy(credential="configured-value", transport=transport)
    with pytest.raises(RelayUnavailableError):
        proxy.post(
            "topic:one",
            {"text": "once", "format": "text", "clientMessageId": "desktop:one"},
        )
    assert len(transport.calls) == 1


def test_relay_response_size_and_json_are_bounded_before_projection():
    with pytest.raises(RelayResponseTooLargeError):
        _read_limited(BytesIO(b"x" * (MAX_RELAY_RESPONSE_BYTES + 1)), {})
    with pytest.raises(RelayResponseTooLargeError):
        _read_limited(BytesIO(b"{}"), {"Content-Length": str(MAX_RELAY_RESPONSE_BYTES + 1)})
    with pytest.raises(RelayMalformedResponseError):
        _decode_json(b"not-json")
