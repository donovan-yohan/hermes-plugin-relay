# Hermes Desktop Relay workspace

The Desktop renderer talks only to the plugin-local REST namespace:
`/api/plugins/hermes-plugin-relay/`. Hermes mounts `dashboard/plugin_api.py`
from this standalone plugin; the renderer cannot address Relay directly or read
plugin process environment variables.

`desktop/plugin.js` contributes the full `/relay` route and sidebar entry. It
renders connection state, channel inventory, the selected transcript, and a
message composer without touching Hermes sessions or composer middleware.

This backend deliberately exposes no `/events` endpoint. Desktop refreshes its
visible page every three seconds instead of pretending that a local poll is a
stream.

## Frozen endpoints

- `GET /connection/status` → `{ "status": "ready" | "offline" | "auth_required" | "error", "message"?: string }`
- `POST /connection/authorize`
- `GET /channels`
- `GET /channels/:id/messages?limit=50` (1–50 only)
- `POST /channels/:id/messages`

Message posts accept exactly:

```json
{
  "text": "Hello",
  "format": "markdown",
  "clientMessageId": "desktop-retry-id"
}
```

The backend forwards that `clientMessageId` byte-for-byte to Relay. It performs
no local retry or idempotency substitution; Relay's stable `channels.post`
contract owns exactly-once behavior.

Only safe channel/message display fields are returned. The backend drops Relay
provider/runtime/turn/item/source correlations, async runs, attachments, and
arbitrary metadata. A caller cannot supply `sender` or `source`.

## Credential boundary

The proxy reads `RELAY_IDE_URL` once at process start (default
`http://127.0.0.1:3456`). It accepts only literal loopback HTTP roots:
`localhost`, `127.0.0.1`, or `[::1]`, with no userinfo, path, query, or
fragment. There is no CORS policy because this is a same-origin Hermes plugin
backend. `localhost` is canonicalized to `127.0.0.1`, ambient proxies are
disabled, and redirects are rejected before another credential-bearing hop.

`RELAY_IDE_OPERATOR_CLIENT_TOKEN` supplies an existing Relay operator-client
credential. If that is absent, `POST /connection/authorize` may redeem the
one-time `RELAY_IDE_OPERATOR_GRANT` using fixed generic client metadata and
only `context:read` / `context:write`. Every supplied or returned credential is
held only in this Python process and is locally bounded to 15 minutes. It is
cleared on local expiry and any Relay 401/403.

Neither token nor grant is ever returned to Desktop JavaScript, included in a
URL, written to config/files, placed in test fixtures, or logged by this
backend. A missing, consumed, expired, or revoked credential/grant is reported
as `auth_required`.

Grant-backed onboarding requires an approved handshake grant with an exact
`channelIds` scope. Relay inherits that scope when the issue request omits one;
the plugin cannot broaden or replace the approved channel set.

## Vertical-slice debt

This is intentionally not set-and-forget authorization. Relay's current
operator-client registry and this plugin credential holder are process-local;
a Relay restart, plugin reload, credential expiry, or revocation requires a
fresh approved grant or a newly supplied environment credential. There is no
persistence, secret-store integration, automatic grant refresh, or event
streaming in this slice.
