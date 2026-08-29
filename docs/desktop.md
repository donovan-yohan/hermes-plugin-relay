# Hermes Desktop Relay workspace

The Desktop renderer talks only to the plugin-local REST namespace:
`/api/plugins/hermes-plugin-relay/`. Hermes mounts `dashboard/plugin_api.py`
from this standalone plugin; the renderer cannot address Relay directly or read
plugin process environment variables.

`desktop/plugin.js` contributes a top-level **Relay** pane center-docked into
the Sessions zone, producing the same `SESSIONS | BOTS | RELAY` tab strip used
by Bot Mode. Selecting it opens a plugin-owned main-area workspace; switching
away tears that workspace down instead of leaving Relay stapled into ordinary
session navigation. The full `/relay` route remains as a deep-link and
older-Desktop fallback, but there is no `sidebar.nav` list row.

The workspace renders connection state, channel inventory, the selected
transcript, and a message composer without touching Hermes sessions or composer
middleware.

A second sidebar surface, **Harnesses**, is a read-only inspector over the
native coding-agent sessions Relay already tracks (Claude Code, Codex, Pi,
Prime Agent, DeepSeek Harness, Antigravity, plus Hermes/OpenCode rows when Relay reports them). Harnesses
render as collapsible groups with an install-status dot, session count, and
optional version; expanding an installed group loads that provider's native
session summaries (newest first), and selecting one shows a bounded, redacted
snapshot. This surface never renders a composer: the hub contract for native
sessions is strictly read-only observation.

This backend deliberately exposes no `/events` endpoint. Desktop refreshes its
visible page every three seconds instead of pretending that a local poll is a
stream.

## Frozen endpoints

- `GET /connection/status` reports both credential lanes independently:
  ```json
  {
    "channels": {
      "status": "ready | offline | auth_required | error",
      "message": "optional safe detail",
      "guidance": "channel operator setup guidance"
    },
    "harnesses": {
      "status": "ready | offline | auth_required | error",
      "message": "optional safe detail",
      "loginAvailable": true
    }
  }
  ```
  Each lane proves itself with one cheap read on its own credential: the
  channel lane lists channels, the harness lane reads `nodes.list`. The harness
  probe deliberately does not list native sessions — that call makes Relay walk
  every provider state root on disk (seconds on a real machine, unbounded in
  the operator's session history), and this endpoint fires on Desktop mount and
  on every Refresh. `nodes.list` sits on the same hub read-command allowlist,
  resolves to the same `session:read` capability, and passes the same
  scoped-actor middleware, so it proves the same thing about the credential.
- `POST /connection/authorize` — legacy redemption of pre-provisioned channel
  and actor grants; Desktop does not use it as a generic authorization action.
- `GET /connection/onboarding` → `{ "url": "http://<literal-loopback>[:port]/" }`
  (credential-free setup target Desktop opens from the channels lane)
- `GET /channels`
- `GET /channels/:id/messages?limit=50` (1–50 only)
- `POST /channels/:id/messages`

### Native harness-session endpoints

These routes ride a separate scoped actor-token lane and return only projected,
renderer-safe fields:

- `GET /harnesses` → per-provider `{ provider, status, sessionCount, version? }`
- `POST /harnesses/login/start` → starts Relay's browser/PIN actor flow and
  returns only `{ status: "pending", verificationUrl, code, expiresAt }`.
- `GET /harnesses/login` → `idle`, the same public `pending` projection,
  `ready`, `denied`, `expired`, or `consumed`. On approval, the backend consumes
  Relay's one-time token internally before returning `ready`.
- `DELETE /harnesses/login` → forgets the process-local flow. Relay expires the
  orphaned upstream flow on its normal short TTL.
- `GET /harnesses/:provider/sessions` → bounded summaries (`id`, `title`,
  `cwd`, `preview`, `updatedAt`, `canWatch`, `redacted`), newest first.
  `cwd` is the harness's own recorded working directory, surfaced for
  orientation only; it is not used by this backend for any access decision.
- `GET /harnesses/:provider/sessions/:nativeId` → one bounded snapshot
  (`capturedAt`, `preview`, `lineCount`, `byteCount`, `eventTypes`, `redacted`)

Unknown providers, oversized ids, and malformed upstream payloads fail closed.
Native transcript source paths, hashes, and provider-internal metadata are
stripped before anything reaches the renderer. There is no write path: the
plugin cannot create, resume, inject into, or delete any harness session.

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

`RELAY_IDE_PUBLIC_URL` optionally supplies a distinct browser-visible root for
the harness approval page, for example a tailnet hostname. It accepts only a
root URL with no credentials, path, query, or fragment, and rejects plaintext
`http` for any non-loopback host: the approval link embeds a one-time flow id
that is a bearer capability for the issued token until it is redeemed. This
value is display-only: Relay API calls and every credential-bearing request
continue to use the loopback-only `RELAY_IDE_URL`.

`RELAY_IDE_OPERATOR_CLIENT_TOKEN` supplies an existing Relay operator-client
credential. If that is absent, `POST /connection/authorize` may redeem the
one-time `RELAY_IDE_OPERATOR_GRANT` using fixed generic client metadata and
only `context:read` / `context:write`. Every supplied or returned credential is
held only in this Python process and is locally bounded to 15 minutes. It is
cleared on local expiry and any Relay 401/403.

The harness-session surface uses a second, independent credential family.
`RELAY_IDE_ACTOR_TOKEN` supplies an existing Relay scoped actor token
(`relay-sac-v1…`) with `session:read`; otherwise `RELAY_IDE_ACTOR_GRANT`
supplies a one-time handshake grant that is redeemed (once) for
audience `relay:cli-gateway:v1`, capability `session:read`, and the standard
read task-ref scope. If neither is set, the plugin reads the
`relay-ide login` credential file at `~/.config/relay-ide/actor-token.json`
(#1435) — so a machine that already ran `relay-ide login` works with zero
plugin configuration. Token precedence: env `RELAY_IDE_ACTOR_TOKEN` > grant >
login file. The two lanes never share tokens: channels stay on the
operator-client credential and native sessions on the actor credential, so a
compromise or failure of one cannot widen into the other. Actor tokens obey the
same in-process-only, cleared-on-401/403 rules, and are auto-renewed ~2 min
before expiry via `POST /cli-gateway/actor-credentials/renew` (the predecessor
is never revoked, so a lost renew response can't lock the plugin out).

When no actor token is live, Desktop's **Connect Harnesses** action starts
Relay's `/cli-gateway/login` browser/PIN flow. The backend owns the flow id,
polls Relay over loopback, and captures the approved `relay-sac-v1…` token
exactly once, and refuses to install it unless the returned credential record
carries exactly `session:read` on the `relay:cli-gateway:v1` audience. Renderer
JavaScript receives only the approval URL, human code, expiry, and public flow
state. This flow grants `session:read` with Relay's
standard read task-ref; it cannot authorize `channels.*` and is never presented
as channel authorization.

Neither token nor grant is ever returned to Desktop JavaScript, included in a
production URL or config artifact, or logged by this backend. Boundary tests use
synthetic markers only to prove they cannot cross renderer-facing responses. A
missing, consumed, expired, or revoked credential/grant is reported as
`auth_required`.

Grant-backed onboarding requires an approved handshake grant with an exact
`channelIds` scope. Relay inherits that scope when the issue request omits one;
the plugin cannot broaden or replace the approved channel set.

## Vertical-slice debt

Channel authorization is intentionally not set-and-forget. Relay's current
operator-client registry and this plugin credential holder are process-local;
a Relay restart, plugin reload, credential expiry, or revocation requires a
fresh approved channel grant or a newly supplied environment credential.
Harnesses have browser/PIN onboarding and actor-token renewal, but the approved
token still lives only in the backend process (or Relay CLI's existing token
file when that source is used). There is no plugin-owned secret persistence or
event streaming in this slice.
