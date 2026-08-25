"""Dashboard routes, loaded the same flat-module way Hermes loads plugins."""

from __future__ import annotations

import importlib.util
import json
import sys

import pytest
from conftest import ROOT, RecordingTransport
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_plugin_relay import __version__
from hermes_plugin_relay.relay_proxy import (
    MAX_RELAY_RESPONSE_BYTES,
    RelayHttpResponse,
    RelayProxy,
    RelayResponseTooLargeError,
)

PREFIX = "/api/plugins/hermes-plugin-relay"
HOST_MODULE_NAME = "hermes_dashboard_plugin_hermes-plugin-relay"


def channel_list() -> dict:
    return {"channels": [{"id": "topic:one", "title": "One", "latestSeq": 1, "messageCount": 1}]}


def message() -> dict:
    return {
        "schemaVersion": 1,
        "id": "chm:one",
        "channelId": "topic:one",
        "seq": 1,
        "kind": "message",
        "status": "completed",
        "sender": {"kind": "human", "id": "human:operator", "displayName": "Operator"},
        "body": {"text": "hello", "format": "markdown"},
        "threadId": None,
        "parentMessageId": None,
        "createdAt": "2026-01-01T00:00:00.000Z",
        "updatedAt": "2026-01-01T00:00:00.000Z",
    }


@pytest.fixture
def api_module():
    path = ROOT / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(HOST_MODULE_NAME, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[HOST_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(HOST_MODULE_NAME, None)


@pytest.fixture
def client(api_module):
    app = FastAPI()
    app.include_router(api_module.router, prefix=PREFIX)
    with TestClient(app) as test_client:
        yield test_client


def install_proxy(api_module, handler, *, credential="configured-value", grant=None):
    transport = RecordingTransport(handler)
    proxy = RelayProxy(credential=credential, grant=grant, transport=transport)
    api_module._proxy_mod.reset_relay_proxy_for_tests(proxy)
    return transport


def install_actor_lane(api_module, handler, *, actor_token=None, grant=None):
    from hermes_plugin_relay.relay_proxy import ActorLaneClient

    transport = RecordingTransport(handler)
    lane = ActorLaneClient(actor_token=actor_token, grant=grant, transport=transport)
    api_module._proxy_mod.reset_actor_lane_for_tests(lane)
    return transport


def test_flat_module_loads_the_shared_package_and_manifest_is_backend_only(api_module):
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text(encoding="utf-8"))
    plugin_yaml = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert api_module._PKG == "hermes_plugin_relay"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["version"] == __version__ == "0.2.0.dev0"
    assert "provides_tools" not in plugin_yaml
    assert "config_schema" not in plugin_yaml


def test_status_and_channel_routes_use_only_safe_projected_data(client, api_module):
    def handler(**call):
        if call["url"].endswith("/channels"):
            return RelayHttpResponse(200, channel_list())
        if call["method"] == "GET":
            return RelayHttpResponse(200, {"messages": [message()]})
        return RelayHttpResponse(201, {"message": message(), "run": {"id": "chrun:not-for-desktop"}})

    transport = install_proxy(api_module, handler)
    assert client.get(f"{PREFIX}/connection/status").json() == {"status": "ready"}
    channels = client.get(f"{PREFIX}/channels")
    assert channels.status_code == 200
    assert channels.json() == channel_list()

    history = client.get(f"{PREFIX}/channels/topic:one/messages")
    assert history.status_code == 200
    assert history.json()["messages"][0]["body"]["text"] == "hello"
    assert transport.calls[-1]["url"].endswith("?limit=50")

    posted = client.post(
        f"{PREFIX}/channels/topic:one/messages",
        json={"text": "exactly once", "format": "markdown", "clientMessageId": "desktop:retry:1"},
    )
    assert posted.status_code == 200
    assert "run" not in posted.json()
    assert json.loads(transport.calls[-1]["body"]) == {
        "text": "exactly once",
        "format": "markdown",
        "clientMessageId": "desktop:retry:1",
    }
    assert "access-control-allow-origin" not in posted.headers


def test_authorize_never_returns_grant_or_issued_credential(client, api_module):
    issued_material = "relay-occ-v1.test.value"
    actor_material = "relay-sac-v1.test.value"
    grant_material = "approved-one-time-value"

    def handler(**call):
        body = json.loads(call["body"]) if call["body"] else {}
        if call["method"] == "GET" and call["url"].endswith("/channels"):
            return RelayHttpResponse(200, {"channels": []})
        if call["url"].endswith("/cli-gateway/actor-credentials"):
            assert body.get("grantHandle") == "actor-grant-material"
            return RelayHttpResponse(201, {"token": actor_material, "credential": {"id": "sac:test"}})
        assert body.get("grantHandle") == grant_material
        return RelayHttpResponse(201, {"token": issued_material, "credential": {"id": "occ:test"}})

    channel_transport = install_proxy(api_module, handler, credential=None, grant=grant_material)
    actor_transport = install_actor_lane(api_module, handler, grant="actor-grant-material")
    response = client.post(f"{PREFIX}/connection/authorize")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    for secret in (issued_material, actor_material, grant_material, "actor-grant-material"):
        assert secret not in response.text
    assert issued_material not in channel_transport.calls[0]["url"]

    # Re-authorize while both credentials live is a no-op: neither one-time
    # grant is replayed.
    def issue_calls():
        return [
            call for call in [*channel_transport.calls, *actor_transport.calls]
            if call["url"].endswith("-credentials")
        ]

    assert len(issue_calls()) == 2
    response = client.post(f"{PREFIX}/connection/authorize")
    assert response.json() == {"status": "ready"}
    assert len(issue_calls()) == 2


def test_authorize_without_a_grant_is_honestly_auth_required(client, api_module):
    transport = install_proxy(api_module, lambda **_call: pytest.fail("must not call Relay"), credential=None)
    response = client.post(f"{PREFIX}/connection/authorize")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"
    assert transport.calls == []


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/channels/topic:missing/messages", 404, "not_found"),
        ("/channels/topic:conflict/messages", 409, "conflict"),
    ],
)
def test_relay_not_found_and_conflict_meaning_are_preserved(client, api_module, path, status, code):
    upstream_status = status
    install_proxy(api_module, lambda **_call: RelayHttpResponse(upstream_status, None))
    response = client.get(f"{PREFIX}{path}")
    assert response.status_code == status
    assert response.json() == {
        "error": {"code": code, "message": "Relay resource was not found" if status == 404 else "Relay reported a conflict", "retryable": False}
    }


def test_recoverable_relay_failures_use_safe_retryable_envelopes(client, api_module):
    install_proxy(
        api_module,
        lambda **_call: (_ for _ in ()).throw(RelayResponseTooLargeError()),
    )
    response = client.get(f"{PREFIX}/channels")
    assert response.status_code == 503
    assert response.json() == {
        "error": {"code": "relay_unavailable", "message": "Relay is unavailable", "retryable": True}
    }
    assert str(MAX_RELAY_RESPONSE_BYTES) not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hi", "format": "markdown"},
        {"text": "hi", "format": "markdown", "clientMessageId": "id", "sender": {}},
        {"text": "hi", "format": "html", "clientMessageId": "id"},
        {"text": "   ", "format": "text", "clientMessageId": "id"},
        {"text": "hi", "format": "text", "clientMessageId": ""},
    ],
)
def test_post_requires_the_exact_stable_body_before_any_forwarding(client, api_module, body):
    transport = install_proxy(api_module, lambda **_call: pytest.fail("must not forward invalid body"))
    response = client.post(f"{PREFIX}/channels/topic:one/messages", json=body)
    assert response.status_code == 400
    assert transport.calls == []


def test_request_body_and_history_query_bounds_are_enforced_before_relay(client, api_module):
    transport = install_proxy(api_module, lambda **_call: pytest.fail("must not forward invalid request"))
    oversized = b"{" + b"x" * (api_module.MAX_DESKTOP_REQUEST_BYTES + 1)
    response = client.post(
        f"{PREFIX}/channels/topic:one/messages",
        content=oversized,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"

    assert client.get(f"{PREFIX}/channels/topic:one/messages?limit=51").status_code == 400
    assert client.get(f"{PREFIX}/channels/topic:one/messages?limit=50&afterSeq=1").status_code == 400
    assert transport.calls == []


def harness_report() -> dict:
    return {
        "sessions": [
            {"provider": "claude", "nativeId": "a1"},
            {"provider": "codex", "nativeId": "c1"},
        ],
        "providers": [
            {
                "provider": "claude",
                "status": "installed",
                "detectedAt": "2026-08-25T00:00:00.000Z",
                "stateRoots": ["/home/test/.claude"],
                "diagnostics": [],
                "version": "2.1.0",
            },
            {
                "provider": "codex",
                "status": "installed",
                "detectedAt": "2026-08-25T00:00:00.000Z",
                "stateRoots": ["/home/test/.codex"],
                "diagnostics": [],
            },
            {
                "provider": "pi",
                "status": "unsupported",
                "detectedAt": "2026-08-25T00:00:00.000Z",
                "stateRoots": [],
                "diagnostics": [],
            },
        ],
    }


def harness_session_summaries() -> dict:
    return {
        "sessions": [
            {
                "provider": "codex",
                "nativeId": "c2-newer",
                "sourcePath": "/home/test/.codex/secret-new.jsonl",
                "cwd": "/repo",
                "title": "newest",
                "createdAt": "2026-08-24T00:00:00.000Z",
                "updatedAt": "2026-08-24T09:00:00.000Z",
                "lastMessageAt": "2026-08-24T09:00:00.000Z",
                "preview": {"text": "fresh", "source": "transcript", "redacted": True, "charCount": 5},
                "metadata": {},
                "capabilities": {
                    "canImportTranscript": True,
                    "canReadProviderState": True,
                    "canStreamLiveEvents": True,
                    "readOnly": True,
                },
            },
            {
                "provider": "codex",
                "nativeId": "c1-older",
                "cwd": "/repo",
                "preview": {"text": "", "source": "none", "redacted": False, "charCount": 0},
                "capabilities": {
                    "canImportTranscript": True,
                    "canReadProviderState": True,
                    "canStreamLiveEvents": False,
                    "readOnly": True,
                },
            },
        ]
    }


def test_harness_routes_project_only_safe_rows(client, api_module):
    def handler(**call):
        if call["url"].endswith("/sessions/native"):
            return RelayHttpResponse(200, harness_report())
        if call["url"].endswith("provider=codex"):
            return RelayHttpResponse(200, harness_session_summaries())
        return pytest.fail(f"unexpected relay call {call['url']}")

    transport = install_actor_lane(api_module, handler, actor_token="configured-sac")

    harnesses = client.get(f"{PREFIX}/harnesses")
    assert harnesses.status_code == 200
    rows = harnesses.json()["harnesses"]
    assert rows == [
        {"provider": "claude", "status": "installed", "sessionCount": 1, "version": "2.1.0"},
        {"provider": "codex", "status": "installed", "sessionCount": 1},
        {"provider": "pi", "status": "unsupported", "sessionCount": 0},
    ]

    sessions = client.get(f"{PREFIX}/harnesses/codex/sessions")
    assert sessions.status_code == 200
    body = sessions.json()["sessions"]
    assert [row["id"] for row in body] == ["c2-newer", "c1-older"]
    wire = json.dumps(body)
    for private in ("secret-new", "/home/test", "hashSha256"):
        assert private not in wire
    assert body[0]["canWatch"] is True
    assert body[0]["redacted"] is True

    list_call, scoped_call = transport.calls
    assert list_call["headers"]["x-relay-cli-command"] == "sessions.native.list"
    assert scoped_call["url"].endswith("/sessions/native?provider=codex")


def test_harness_snapshot_route_is_bounded_and_private_path_free(client, api_module):
    def handler(**_call):
        return RelayHttpResponse(
            200,
            {
                "snapshot": {
                    "ref": {"provider": "claude", "nativeId": "a1", "sourcePath": "/secret/a1.jsonl"},
                    "capturedAt": "2026-08-25T01:00:00.000Z",
                    "sourcePath": "/secret/a1.jsonl",
                    "summary": {
                        "lineCount": 3,
                        "byteCount": 512,
                        "hashSha256": "beef",
                        "eventTypes": ["user-message"],
                        "preview": {"text": "hi", "source": "transcript", "redacted": False, "charCount": 2},
                    },
                    "redaction": {"rawPayloadStored": False, "strategy": "preview", "classes": []},
                }
            },
        )

    install_actor_lane(api_module, handler, actor_token="t")
    response = client.get(f"{PREFIX}/harnesses/claude/sessions/a1")
    assert response.status_code == 200
    snapshot = response.json()["snapshot"]
    assert snapshot == {
        "id": "a1",
        "provider": "claude",
        "capturedAt": "2026-08-25T01:00:00.000Z",
        "preview": "hi",
        "redacted": False,
        "lineCount": 3,
        "byteCount": 512,
        "eventTypes": ["user-message"],
    }


def test_invalid_provider_paths_fail_closed(client, api_module):
    install_actor_lane(api_module, lambda **_call: pytest.fail("must not dial"))
    response = client.get(f"{PREFIX}/harnesses/nope/sessions")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_provider"


def test_oversized_native_ids_reject_before_relay(client, api_module):
    install_actor_lane(api_module, lambda **_call: pytest.fail("must not dial"))
    response = client.get(f"{PREFIX}/harnesses/claude/sessions/{'x' * 600}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_session"


def test_actor_auth_required_maps_to_the_shared_envelope(client, api_module):
    install_actor_lane(api_module, lambda **_call: pytest.fail("must not dial"))
    response = client.get(f"{PREFIX}/harnesses")
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "auth_required", "message": "Relay authorization is required", "retryable": False}
    }
