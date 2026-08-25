"""Actor-lane client and native-session projection coverage."""

from __future__ import annotations

import json

import pytest
from conftest import RecordingTransport
from hermes_plugin_relay.relay_proxy import (
    ACTOR_AUDIENCE,
    ACTOR_CREDENTIAL_TTL_MS,
    ACTOR_READ_TASK_REF,
    ActorLaneClient,
    RelayAuthRequiredError,
    RelayConfigurationError,
    RelayHttpResponse,
    RelayMalformedResponseError,
    project_harness_rows,
    project_native_session_snapshot,
    project_native_session_summaries,
)

ACTOR_ISSUE_PATH = "/cli-gateway/actor-credentials"
NATIVE_LIST_PATH = "/sessions/native"


def provider_status(provider: str, status: str, version: str | None = None) -> dict:
    row = {
        "provider": provider,
        "status": status,
        "detectedAt": "2026-08-25T00:00:00.000Z",
        "stateRoots": ["/home/test/.claude"],
        "diagnostics": [],
    }
    if version is not None:
        row["version"] = version
    return row


def harness_report() -> dict:
    return {
        "sessions": [
            {"provider": "claude", "nativeId": "a1"},
            {"provider": "codex", "nativeId": "c1"},
            {"provider": "codex", "nativeId": "c2"},
        ],
        "providers": [
            provider_status("claude", "installed", "2.1.0"),
            provider_status("codex", "installed"),
            provider_status("hermes", "unsupported"),
            provider_status("opencode", "unavailable"),
        ],
    }


def session_summary(
    provider: str,
    native_id: str,
    *,
    updated: str = "2026-08-20T10:00:00.000Z",
    can_watch: bool = False,
) -> dict:
    return {
        "provider": provider,
        "nativeId": native_id,
        "sourcePath": f"/home/test/.{provider}/secret-path-{native_id}.jsonl",
        "cwd": "/repo",
        "title": f"{native_id} title",
        "createdAt": "2026-08-19T00:00:00.000Z",
        "updatedAt": updated,
        "lastMessageAt": updated,
        "preview": {
            "text": f"preview for {native_id}",
            "source": "transcript",
            "redacted": True,
            "charCount": 18,
        },
        "metadata": {"lineCount": 42, "hashSha256": "deadbeef", "nativeSessionId": native_id},
        "capabilities": {
            "canImportTranscript": True,
            "canReadProviderState": True,
            "canStreamLiveEvents": can_watch,
            "readOnly": True,
        },
    }


def snapshot_payload() -> dict:
    return {
        "snapshot": {
            "ref": {
                "provider": "codex",
                "nativeId": "c1",
                "sourcePath": "/home/test/.codex/secret-c1.jsonl",
                "stateRoot": "/home/test/.codex",
            },
            "capturedAt": "2026-08-24T12:00:00.000Z",
            "sourcePath": "/home/test/.codex/secret-c1.jsonl",
            "summary": {
                "lineCount": 7,
                "byteCount": 4096,
                "hashSha256": "cafe",
                "eventTypes": ["user-message", "assistant-message"],
                "preview": {
                    "text": "snapshot preview",
                    "source": "transcript",
                    "redacted": True,
                    "charCount": 16,
                },
            },
            "redaction": {
                "rawPayloadStored": False,
                "strategy": "preview",
                "classes": ["transcript"],
            },
        }
    }


# --- projections -------------------------------------------------------------


def test_harness_rows_are_hub_ordered_with_counts_and_versions():
    rows = project_harness_rows(harness_report())
    assert [row["provider"] for row in rows] == ["claude", "codex", "hermes", "opencode"]
    assert rows[0] == {
        "provider": "claude",
        "status": "installed",
        "sessionCount": 1,
        "version": "2.1.0",
    }
    assert "version" not in rows[1]
    assert rows[2]["status"] == "unsupported"


def test_harness_projection_rejects_unknown_shapes():
    with pytest.raises(RelayMalformedResponseError):
        project_harness_rows({"sessions": "nope"})
    with pytest.raises(RelayMalformedResponseError):
        project_harness_rows({"sessions": [], "providers": [{"provider": "x"}]})


def test_session_summaries_drop_private_paths_and_sort_newest_first():
    payload = {
        "sessions": [
            session_summary("codex", "old", updated="2026-08-01T00:00:00.000Z"),
            session_summary("codex", "new", updated="2026-08-22T09:30:00.000Z"),
        ]
    }
    rows = project_native_session_summaries(payload)
    assert [row["id"] for row in rows] == ["new", "old"]
    wire = json.dumps(rows)
    for private in ("secret-path-old", "secret-path-new", "hashSha256"):
        assert private not in wire
    assert rows[0] == {
        "id": "new",
        "provider": "codex",
        "title": "new title",
        "cwd": "/repo",
        "preview": "preview for new",
        "redacted": True,
        "updatedAt": "2026-08-22T09:30:00.000Z",
        "canWatch": False,
    }


def test_summary_can_watch_follows_capability_bit():
    payload = {"sessions": [session_summary("pi", "live", can_watch=True)]}
    (row,) = project_native_session_summaries(payload)
    assert row["canWatch"] is True


def test_summary_projection_is_bounded_and_validated():
    sessions = [session_summary("claude", f"s{i}") for i in range(300)]
    rows = project_native_session_summaries({"sessions": sessions})
    assert len(rows) == 200
    with pytest.raises(RelayMalformedResponseError):
        project_native_session_summaries({"sessions": [{"provider": "claude"}]})
    with pytest.raises(RelayMalformedResponseError):
        project_native_session_summaries({"sessions": "nope"})


def test_snapshot_projection_keeps_bounds_and_drops_paths():
    result = project_native_session_snapshot(snapshot_payload())
    wire = json.dumps(result)
    assert result == {
        "id": "c1",
        "provider": "codex",
        "capturedAt": "2026-08-24T12:00:00.000Z",
        "preview": "snapshot preview",
        "redacted": True,
        "lineCount": 7,
        "byteCount": 4096,
        "eventTypes": ["user-message", "assistant-message"],
    }
    assert "secret" not in wire
    with pytest.raises(RelayMalformedResponseError):
        project_native_session_snapshot({"snapshot": {}})


# --- ActorLaneClient ---------------------------------------------------------


def make_lane(handler, **kwargs):
    transport = RecordingTransport(handler)
    lane = ActorLaneClient(transport=transport, **kwargs)
    return lane, transport


def issue_ok(**_call):
    return RelayHttpResponse(
        201,
        {"token": "relay-sac-v1.test.value", "credential": {"id": "sac:test"}},
    )


def test_authorize_redeems_grant_once_with_exact_issue_body():
    calls = []

    def handler(**call):
        calls.append(call)
        return issue_ok()

    clock = [0.0]
    lane, _ = make_lane(handler, grant="approved-grant-value", clock=lambda: clock[0])
    assert lane.authorize().to_wire() == {"status": "ready"}
    body = json.loads(calls[0]["body"])
    assert calls[0]["url"].endswith(ACTOR_ISSUE_PATH)
    assert body == {
        "grantHandle": "approved-grant-value",
        "audience": ACTOR_AUDIENCE,
        "actor": {"type": "cli", "id": "desktop-plugin-harness-view"},
        "capabilities": ["session:read"],
        "ttlMs": ACTOR_CREDENTIAL_TTL_MS,
        "scope": {"taskRefs": [ACTOR_READ_TASK_REF]},
        "correlationId": "desktop-plugin-harness-view-authorize",
    }

    # A second authorize must not re-redeem; an expired credential fails closed.
    assert lane.authorize().to_wire() == {"status": "ready"}
    assert len(calls) == 1
    clock[0] += ACTOR_CREDENTIAL_TTL_MS / 1000
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()
    assert len(calls) == 1


def test_actor_headers_carry_lane_markers_and_command():
    def handler(**_call):
        return RelayHttpResponse(
            200,
            harness_report(),
        )

    lane, transport = make_lane(handler, actor_token="configured-sac")
    lane.harnesses()
    call = transport.calls[0]
    assert call["url"].endswith(NATIVE_LIST_PATH)
    # Exact header dict: the actor marker must be present, the operator-client
    # channel-lane markers must never bleed into this lane.
    assert call["headers"] == {
        "Accept": "application/json",
        "x-relay-cli-gateway": "v1",
        "x-relay-cli-command": "sessions.native.list",
        "x-relay-cli-actor-token": "v1",
        "x-relay-capabilities": "session:read",
        "Authorization": "Bearer configured-sac",
    }
    assert "x-relay-operator-client-token" not in call["headers"]


def test_provider_scoped_list_encodes_query_and_get_encodes_path_segments():
    def handler(**_call):
        return RelayHttpResponse(200, snapshot_payload())

    lane, transport = make_lane(handler, actor_token="t")
    assert lane.harness_session("codex", "c1")["id"] == "c1"
    assert transport.calls[-1]["url"].endswith("/sessions/native/codex/c1")

    lane.harness_session("codex", "abc/123:def")
    get_call = transport.calls[-1]
    assert get_call["url"].endswith("/sessions/native/codex/abc%2F123%3Adef")
    assert get_call["headers"]["x-relay-cli-command"] == "sessions.native.get"


def test_invalid_provider_or_session_ids_fail_closed_without_network():
    lane, transport = make_lane(lambda **_call: pytest.fail("must not dial"), actor_token="t")
    with pytest.raises(RelayConfigurationError):
        lane.harness_sessions("../../etc")
    with pytest.raises(RelayConfigurationError):
        lane.harness_session("claude", "")
    with pytest.raises(RelayConfigurationError):
        lane.harness_session("claude", "x" * 600)
    assert transport.calls == []


def test_upstream_auth_failure_clears_an_issued_token_like_the_channel_lane():
    calls = []

    def handler(**call):
        calls.append(call["url"])
        if call["url"].endswith(ACTOR_ISSUE_PATH):
            return issue_ok()
        return RelayHttpResponse(403, None)

    lane, _ = make_lane(handler, grant="approved-grant-value")
    assert lane.authorize().status == "ready"
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()
    # The issued token is gone, so the next call cannot even reach the transport.
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()
    dialled = [url for url in calls if not url.endswith(ACTOR_ISSUE_PATH)]
    assert len(dialled) == 1


def test_configured_environment_token_is_not_silent_about_rejection():
    """A rejected env token surfaces auth errors instead of being cleared."""

    def handler(**_call):
        return RelayHttpResponse(401, None)

    lane, transport = make_lane(handler, actor_token="env-sac-value")
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()
    assert len(transport.calls) == 2


def test_environment_construction_reads_both_lanes_independently(monkeypatch):
    monkeypatch.setenv("RELAY_IDE_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("RELAY_IDE_ACTOR_TOKEN", "env-sac-value")
    monkeypatch.setenv("RELAY_IDE_ACTOR_GRANT", "env-grant-value")
    lane = ActorLaneClient.from_environment()
    assert lane._base_url == "http://127.0.0.1:9999"
    assert lane._grant == "env-grant-value"
