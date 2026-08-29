"""Actor-lane client and native-session projection coverage."""

from __future__ import annotations

import json

import pytest
from conftest import RecordingTransport
from hermes_plugin_relay.relay_proxy import (
    ACTOR_AUDIENCE,
    ACTOR_CREDENTIAL_TTL_MS,
    ACTOR_PROBE_COMMAND,
    ACTOR_PROBE_PATH,
    ACTOR_READ_TASK_REF,
    ActorLaneClient,
    RelayAuthRequiredError,
    RelayConfigurationError,
    RelayHttpResponse,
    RelayMalformedResponseError,
    project_harness_rows,
    project_lane_probe,
    project_native_session_snapshot,
    project_native_session_summaries,
)

ACTOR_ISSUE_PATH = "/cli-gateway/actor-credentials"
NATIVE_LIST_PATH = "/sessions/native"


def probe_ok(**_call):
    return RelayHttpResponse(200, {"nodes": []})


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
    kwargs.setdefault("token_file", None)
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
    monkeypatch.setenv("RELAY_IDE_PUBLIC_URL", "https://relay.example.test")
    monkeypatch.setenv("RELAY_IDE_ACTOR_TOKEN", "env-sac-value")
    monkeypatch.setenv("RELAY_IDE_ACTOR_GRANT", "env-grant-value")
    lane = ActorLaneClient.from_environment()
    assert lane._base_url == "http://127.0.0.1:9999"
    assert lane._approval_base_url == "https://relay.example.test"
    assert lane._grant == "env-grant-value"


def test_actor_status_is_independent_and_does_not_dial_without_a_token():
    missing, missing_transport = make_lane(lambda **_call: pytest.fail("must not dial"))
    assert missing.status().to_wire() == {
        "status": "auth_required",
        "message": "Harness authorization is required",
    }
    assert missing_transport.calls == []

    ready, ready_transport = make_lane(probe_ok, actor_token="configured-sac")
    assert ready.status().to_wire() == {"status": "ready"}
    assert ready_transport.calls[0]["url"].endswith(ACTOR_PROBE_PATH)


def test_status_probes_the_lane_without_listing_native_sessions():
    """The status check must not make the hub walk every provider state root.

    Listing native sessions is unbounded in the operator's harness history —
    it is a filesystem walk over every provider's state root — and this check
    fires on mount and on every explicit refresh. The probe reads the same
    credential lane through the same scoped-actor middleware and the same
    `session:read` capability, but answers from the hub's in-memory registry.
    """

    def handler(**call):
        if call["url"].endswith(NATIVE_LIST_PATH):
            return pytest.fail("a status check must never list native sessions")
        assert call["url"].endswith(ACTOR_PROBE_PATH)
        assert call["method"] == "GET"
        assert call["headers"]["x-relay-cli-command"] == ACTOR_PROBE_COMMAND
        assert call["body"] is None
        return probe_ok()

    lane, transport = make_lane(handler, actor_token="configured-sac")

    assert lane.status().to_wire() == {"status": "ready"}
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("probe_status", "probe_payload", "expected"),
    [
        (401, {}, {"status": "auth_required", "message": "Harness authorization is required"}),
        (403, {}, {"status": "auth_required", "message": "Harness authorization is required"}),
        (503, {}, {"status": "offline", "message": "Relay is unavailable"}),
        (400, {}, {"status": "error", "message": "Relay returned an invalid response"}),
        # A 200 that is not the node-list contract is a hub speaking something
        # else, not a healthy lane.
        (200, {"sessions": [], "providers": []}, {"status": "error", "message": "Relay returned an invalid response"}),
        (200, {"nodes": "not-a-list"}, {"status": "error", "message": "Relay returned an invalid response"}),
    ],
)
def test_status_maps_every_probe_outcome_to_its_own_lane_state(
    probe_status, probe_payload, expected
):
    lane, _transport = make_lane(
        lambda **_call: RelayHttpResponse(probe_status, probe_payload),
        actor_token="configured-sac",
    )

    assert lane.status().to_wire() == expected


def test_harness_rows_still_come_from_the_native_listing():
    """The probe replaces the status dial only; the harness surface is unchanged."""

    def handler(**call):
        if call["url"].endswith(NATIVE_LIST_PATH):
            return RelayHttpResponse(200, harness_report())
        return pytest.fail(f"unexpected Relay call: {call['url']}")

    lane, transport = make_lane(handler, actor_token="configured-sac")

    rows = lane.harnesses()
    assert [row["provider"] for row in rows] == ["claude", "codex", "hermes", "opencode"]
    assert transport.calls[0]["headers"]["x-relay-cli-command"] == "sessions.native.list"


def test_lane_probe_projection_keeps_nothing_and_rejects_other_shapes():
    assert project_lane_probe({"nodes": []}) is None
    assert project_lane_probe({"nodes": [{"nodeId": "n1"}]}) is None
    for payload in ({}, {"nodes": None}, {"nodes": {}}, [], "nodes", None):
        with pytest.raises(RelayMalformedResponseError):
            project_lane_probe(payload)


def test_browser_login_projects_public_fields_and_captures_token_exactly_once():
    secret = "relay-sac-v1.device-flow-secret"
    flow_id = "11111111-2222-3333-4444-555555555555"
    poll_count = 0

    def handler(**call):
        nonlocal poll_count
        if call["url"].endswith("/cli-gateway/login/start"):
            return RelayHttpResponse(
                201,
                {
                    "flowId": flow_id,
                    "code": "ABCD-1234",
                    "expiresAt": "2099-01-01T00:00:00.000Z",
                    "verificationUrl": (
                        f"http://127.0.0.1:3456/cli-gateway/login/{flow_id}/approve"
                    ),
                },
            )
        if call["url"].endswith(f"/cli-gateway/login/{flow_id}"):
            poll_count += 1
            if poll_count == 1:
                return RelayHttpResponse(200, {"status": "pending"})
            return RelayHttpResponse(
                200,
                {
                    "status": "approved",
                    "token": secret,
                    "credential": {
                        "id": "sac:device",
                        "audience": "relay:cli-gateway:v1",
                        "capabilities": ["session:read"],
                        "expiresAt": "2099-01-01T00:00:00.000Z",
                    },
                },
            )
        if call["url"].endswith(NATIVE_LIST_PATH):
            return RelayHttpResponse(200, harness_report())
        return pytest.fail(f"unexpected Relay call: {call['url']}")

    lane, transport = make_lane(
        handler,
        public_url="https://dev.example.test",
    )
    started = lane.start_login()
    assert started == {
        "status": "pending",
        "code": "ABCD-1234",
        "expiresAt": "2099-01-01T00:00:00.000Z",
        "verificationUrl": (
            f"https://dev.example.test/cli-gateway/login/{flow_id}/approve"
        ),
    }
    # The display-only public root must never carry a request: assert the exact
    # origin, because a suffix match would pass for either base URL.
    assert transport.calls[0]["url"] == "http://127.0.0.1:3456/cli-gateway/login/start"
    assert secret not in json.dumps(started)
    assert json.loads(transport.calls[0]["body"]) == {
        "actorId": "desktop-plugin-harness-view",
        "displayName": "Relay desktop plugin",
        "capabilities": ["session:read"],
    }

    assert lane.poll_login() == started
    approved = lane.poll_login()
    assert approved == {"status": "ready"}
    assert secret not in json.dumps(approved)
    assert lane.poll_login() == {"status": "ready"}
    assert poll_count == 2, "the one-shot approved response is never polled again"

    lane.harnesses()
    assert transport.calls[-1]["headers"]["Authorization"] == f"Bearer {secret}"
    assert all(
        call["url"].startswith("http://127.0.0.1:3456/") for call in transport.calls
    ), "every credential-bearing request stays on the loopback base URL"


@pytest.mark.parametrize(
    ("upstream_status", "message"),
    [
        ("denied", "Relay login was denied."),
        ("expired", "The Relay login code expired."),
        ("consumed", "This Relay login was already used. Start again."),
    ],
)
def test_browser_login_projects_terminal_states_and_forgets_the_flow(
    upstream_status, message
):
    flow_id = "11111111-2222-3333-4444-555555555555"

    def handler(**call):
        if call["url"].endswith("/cli-gateway/login/start"):
            return RelayHttpResponse(
                201,
                {
                    "flowId": flow_id,
                    "code": "ABCD-1234",
                    "expiresAt": "2099-01-01T00:00:00.000Z",
                    "verificationUrl": "http://127.0.0.1:3456/ignored",
                },
            )
        return RelayHttpResponse(200, {"status": upstream_status})

    lane, _ = make_lane(handler)
    lane.start_login()
    assert lane.poll_login() == {"status": upstream_status, "message": message}
    assert lane.poll_login() == {"status": "idle"}


def test_malformed_one_shot_approval_is_not_polled_again():
    flow_id = "11111111-2222-3333-4444-555555555555"
    polls = 0

    def handler(**call):
        nonlocal polls
        if call["url"].endswith("/cli-gateway/login/start"):
            return RelayHttpResponse(
                201,
                {
                    "flowId": flow_id,
                    "code": "ABCD-1234",
                    "expiresAt": "2099-01-01T00:00:00.000Z",
                    "verificationUrl": "http://127.0.0.1:3456/ignored",
                },
            )
        polls += 1
        return RelayHttpResponse(
            200,
            {
                "status": "approved",
                "token": "not-an-actor-token",
                "credential": {"expiresAt": "2099-01-01T00:00:00.000Z"},
            },
        )

    lane, _ = make_lane(handler)
    lane.start_login()
    with pytest.raises(RelayMalformedResponseError):
        lane.poll_login()
    assert lane.poll_login() == {"status": "idle"}
    assert polls == 1


def test_invalid_public_url_disables_browser_login_without_dialing():
    lane, transport = make_lane(
        lambda **_call: pytest.fail("invalid login configuration must not dial"),
        public_url="https://relay.example.test/approve",
    )

    assert lane.login_available() is False
    assert lane.status().to_wire() == {
        "status": "error",
        "message": "Relay public approval URL is invalid",
    }
    with pytest.raises(RelayConfigurationError):
        lane.start_login()
    assert transport.calls == []


# --- login file (#1435) + renewal --------------------------------------------


def test_login_file_token_is_loaded_and_caches(tmp_path):
    token = "relay-sac-v1.from-file"
    expires_at = "2099-01-01T00:00:00.000Z"
    file_path = tmp_path / "actor-token.json"
    file_path.write_text(
        json.dumps(
            {
                "version": 1,
                "token": token,
                "credentialId": "sac:file",
                "hubUrl": "http://127.0.0.1:3456",
                "issuedAt": "2026-08-25T00:00:00.000Z",
                "expiresAt": expires_at,
                "actorId": "relay-cli@test",
                "capabilities": ["session:read"],
            }
        ),
        encoding="utf-8",
    )

    def handler(**_call):
        return RelayHttpResponse(200, harness_report())

    lane, transport = make_lane(handler, token_file=str(file_path))
    lane.harnesses()
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {token}"


def test_login_file_expired_or_missing_yields_nothing(tmp_path, monkeypatch):
    # The real file on this machine must not leak into this test.
    monkeypatch.setenv("HOME", str(tmp_path))

    def handler(**_call):
        return RelayHttpResponse(200, harness_report())

    # No file at all.
    lane, _ = make_lane(handler, token_file=str(tmp_path / "nonexistent.json"))
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()

    # Expired file.
    expired = tmp_path / "expired.json"
    expired.write_text(
        json.dumps(
            {
                "version": 1,
                "token": "relay-sac-v1.expired",
                "expiresAt": "2020-01-01T00:00:00.000Z",
                "credentialId": "x",
                "hubUrl": "http://127.0.0.1:3456",
                "issuedAt": "2020-01-01T00:00:00.000Z",
                "actorId": "x",
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )
    lane, transport = make_lane(handler, token_file=str(expired))
    # Prove no live token leaked from the real machine.
    assert lane._configured_token is None
    assert lane._grant is None
    assert lane._token_file == str(expired)
    active = lane._active_token()
    assert active is None, f"expected no active token, got {active!r}"
    with pytest.raises(RelayAuthRequiredError):
        lane.harnesses()


def test_missing_login_file_is_rechecked_after_short_cache(tmp_path):
    clock = [10.0]
    token_file = tmp_path / "actor-token.json"
    lane, _ = make_lane(
        lambda **_call: RelayHttpResponse(200, harness_report()),
        token_file=str(token_file),
        clock=lambda: clock[0],
    )

    assert lane._active_token() is None
    token_file.write_text(
        json.dumps(
            {
                "token": "relay-sac-v1.created-later",
                "expiresAt": "2099-01-01T00:00:00.000Z",
            }
        ),
        encoding="utf-8",
    )
    assert lane._active_token() is None, "a missing file is cached briefly"
    clock[0] += 5
    assert lane._active_token() == "relay-sac-v1.created-later"


def test_renewal_fires_before_expiry_and_swaps_token():
    clock = [100.0]
    original_token = "relay-sac-v1.original"
    renewed_token = "relay-sac-v1.renewed"
    renewed_token_2 = "relay-sac-v1.renewed-2"
    renew_path = "/cli-gateway/actor-credentials/renew"
    renew_count = [0]

    def handler(**call):
        if call["url"].endswith(renew_path):
            renew_count[0] += 1
            assert call["headers"]["x-relay-cli-command"] == "actor-credentials.renew"
            token = renewed_token if renew_count[0] == 1 else renewed_token_2
            # Future deadline so the renewed token survives long enough
            # for the first request; the second renewal is forced by
            # manipulating _issued_deadline directly.
            expires = "2099-01-01T00:00:00.000Z"
            return RelayHttpResponse(
                201,
                {
                    "token": token,
                    "credential": {"id": f"sac:{renew_count[0]}", "expiresAt": expires},
                },
            )
        return RelayHttpResponse(200, harness_report())

    lane, transport = make_lane(handler, actor_token=original_token, clock=lambda: clock[0])
    # Set the configured deadline so _maybe_renew sees it within the margin.
    with lane._lock:
        lane._configured_deadline = clock[0] + 60  # < 120s margin

    lane.harnesses()
    # The renew call happened and the new token is now active.
    renew_calls = [c for c in transport.calls if c["url"].endswith(renew_path)]
    assert len(renew_calls) == 1
    assert renew_calls[0]["headers"]["Authorization"] == f"Bearer {original_token}"
    # The native-list call used the renewed token.
    list_calls = [c for c in transport.calls if c["url"].endswith("/sessions/native")]
    assert list_calls[0]["headers"]["Authorization"] == f"Bearer {renewed_token}"

    # Advance past the renewed token's deadline so a second renewal fires.
    with lane._lock:
        lane._issued_deadline = clock[0] + 30  # < 120s margin
    lane.harnesses()
    renew_calls = [c for c in transport.calls if c["url"].endswith(renew_path)]
    assert len(renew_calls) == 2, "second renewal must fire after first renewed token nears expiry"
    assert renew_calls[1]["headers"]["Authorization"] == f"Bearer {renewed_token}"
    list_calls = [c for c in transport.calls if c["url"].endswith("/sessions/native")]
    assert list_calls[1]["headers"]["Authorization"] == f"Bearer {renewed_token_2}"


def test_renewal_failure_keeps_old_token_working():
    clock = [100.0]

    def handler(**call):
        if call["url"].endswith("/cli-gateway/actor-credentials/renew"):
            return RelayHttpResponse(503, None)
        return RelayHttpResponse(200, harness_report())

    lane, transport = make_lane(handler, actor_token="relay-sac-v1.original", clock=lambda: clock[0])
    with lane._lock:
        lane._configured_deadline = clock[0] + 60

    lane.harnesses()
    list_calls = [c for c in transport.calls if c["url"].endswith("/sessions/native")]
    assert list_calls[0]["headers"]["Authorization"] == "Bearer relay-sac-v1.original"


def test_renewal_rejects_naive_expiry_and_uses_bounded_fallback():
    clock = [100.0]

    def handler(**call):
        if call["url"].endswith("/cli-gateway/actor-credentials/renew"):
            return RelayHttpResponse(
                201,
                {
                    "token": "relay-sac-v1.renewed",
                    "credential": {"expiresAt": "2099-01-01T00:00:00"},
                },
            )
        return RelayHttpResponse(200, harness_report())

    lane, _ = make_lane(
        handler,
        actor_token="relay-sac-v1.original",
        clock=lambda: clock[0],
    )
    with lane._lock:
        lane._configured_deadline = clock[0] + 60

    lane.harnesses()

    assert lane._issued_deadline == clock[0] + ACTOR_CREDENTIAL_TTL_MS / 1000


LOGIN_START_PATH = "/cli-gateway/login/start"


def login_start_response(flow_id: str) -> RelayHttpResponse:
    return RelayHttpResponse(
        201,
        {
            "flowId": flow_id,
            "code": "ABCD-1234",
            "expiresAt": "2099-01-01T00:00:00.000Z",
            "verificationUrl": (
                f"http://127.0.0.1:3456/cli-gateway/login/{flow_id}/approve"
            ),
        },
    )


def test_a_hub_rejected_env_token_does_not_wedge_relay_login():
    """A stale but unexpired env/file token must not block a fresh login.

    `_actor_request` deliberately keeps operator-supplied tokens on 401/403, so
    without lane-rejection tracking `start_login` would short-circuit to
    "ready" forever while the lane stayed `auth_required`.
    """

    starts = 0

    def handler(**call):
        nonlocal starts
        if call["url"].endswith(ACTOR_PROBE_PATH):
            return RelayHttpResponse(401, {})
        if call["url"].endswith(LOGIN_START_PATH):
            starts += 1
            return login_start_response("11111111-2222-3333-4444-555555555555")
        return pytest.fail(f"unexpected Relay call: {call['url']}")

    lane, _transport = make_lane(handler, actor_token="relay-sac-v1.stale")

    assert lane.status().to_wire() == {
        "status": "auth_required",
        "message": "Harness authorization is required",
    }
    started = lane.start_login()

    assert started["status"] == "pending"
    assert starts == 1, "the rejected lane must still be able to dial a login"


def test_start_login_reuses_a_live_flow_instead_of_orphaning_hub_slots():
    starts = 0

    def handler(**call):
        nonlocal starts
        if call["url"].endswith(LOGIN_START_PATH):
            starts += 1
            return login_start_response("11111111-2222-3333-4444-555555555555")
        return pytest.fail(f"unexpected Relay call: {call['url']}")

    lane, _transport = make_lane(handler)
    first = lane.start_login()

    assert lane.start_login() == first
    assert lane.start_login() == first
    assert starts == 1, "Relay's pending-flow cap is not burned by repeat clicks"


def test_a_widened_or_mis_audienced_login_credential_is_refused():
    flow_id = "11111111-2222-3333-4444-555555555555"

    def make_handler(credential):
        def handler(**call):
            if call["url"].endswith(LOGIN_START_PATH):
                return login_start_response(flow_id)
            if call["url"].endswith(f"/cli-gateway/login/{flow_id}"):
                return RelayHttpResponse(
                    200,
                    {
                        "status": "approved",
                        "token": "relay-sac-v1.widened",
                        "credential": credential,
                    },
                )
            return pytest.fail(f"unexpected Relay call: {call['url']}")

        return handler

    good = {
        "id": "sac:device",
        "audience": ACTOR_AUDIENCE,
        "capabilities": ["session:read"],
        "expiresAt": "2099-01-01T00:00:00.000Z",
    }
    for credential in (
        {**good, "capabilities": ["session:read", "session:write"]},
        {**good, "capabilities": ["session:write"]},
        {**good, "capabilities": []},
        {**good, "audience": "relay:operator-client:v1"},
        {key: value for key, value in good.items() if key != "capabilities"},
    ):
        lane, _transport = make_lane(make_handler(credential))
        lane.start_login()
        with pytest.raises(RelayMalformedResponseError):
            lane.poll_login()
        assert lane._active_token() is None, "a widened credential is never installed"

    lane, _transport = make_lane(make_handler(good))
    lane.start_login()
    assert lane.poll_login() == {"status": "ready"}


@pytest.mark.parametrize(
    "flow_id",
    ["..", ".", "a/b", "a b", "a?b", "a#b", "a%2fb", "", "x" * 129],
)
def test_a_hostile_flow_id_is_rejected_before_it_reaches_a_url(flow_id):
    def handler(**call):
        if call["url"].endswith(LOGIN_START_PATH):
            return login_start_response(flow_id)
        return pytest.fail(f"unexpected Relay call: {call['url']}")

    lane, _transport = make_lane(handler)
    with pytest.raises(RelayMalformedResponseError):
        lane.start_login()
