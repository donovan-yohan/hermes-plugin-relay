# hermes-plugin-relay

A native Hermes Desktop workspace for channels and harness sessions on a local
Relay hub. The
uncompiled Desktop plugin contributes a top-level **Relay** tab beside
**Sessions** and **Bots**, then opens its dedicated main-area workspace. A thin
Python dashboard API keeps Relay credentials and network access out of the
renderer.

This is a breaking pivot from the former participant/provider runtime: there
are no Hermes tools, provider adapters, subprocesses, participant skills,
persistent session records, or compatibility endpoints. The backend has no
CORS middleware and does not expose `/events`; the visible page uses a bounded
three-second refresh fallback.

## Install

[Install in Hermes](hermes://plugin/install?repo=donovan-yohan/hermes-plugin-relay&enable=1),
or use the CLI:

```bash
hermes plugins install donovan-yohan/hermes-plugin-relay --enable
```

The backend and Desktop halves are separately opt-in. After installation,
enable **Relay** in Hermes Desktop under **Settings → Plugins**, then select the
**Relay** tab in the sidebar's top-level tab strip. Relay itself must be running
locally with the operator-client credential endpoints described below.

## Desktop workspace

Selecting the **Relay** tab opens a dedicated main-area workspace. It lists
accessible Relay channels, renders the selected channel's latest 50 messages
with human/agent/system attribution, and posts Markdown messages with a stable
client-generated idempotency key. It keeps a stale transcript and unsent draft
visible while Relay is offline, and renders authorization, empty, archived,
malformed, missing-channel, and upstream-error states separately. The hidden
`/relay` route remains available for deep links and older Desktop builds.

The connection panel reports **Channels** and **Harnesses** independently.
Channel access uses a channel-scoped operator credential; when it is missing,
the channels lane offers **Open Relay**, which asks the backend for the
validated loopback Relay root and opens it in the system browser so the operator
can provision a grant. Harness access is approved with Relay Login: Desktop
shows the approval URL and one-time code, links out to the approval page, and
polls until Relay approves or rejects the flow. A successful harness login does
not imply channel access.

The sidebar's **Harnesses** switch shows every native coding-agent harness the
hub tracks — Claude Code, Codex, Pi, Prime Agent, DeepSeek Harness, Antigravity, plus
Hermes/OpenCode rows when supported — as collapsible groups with install
status and session counts.
Expanding an installed group lists that harness's native sessions newest-first;
selecting one displays a bounded, redacted transcript snapshot. This view is
strictly read-only observation and never sends anything to a harness.

## Configure the local Relay connection

The only accepted Relay URL is a loopback HTTP root. The default is:

```text
RELAY_IDE_URL=http://127.0.0.1:3456
```

Accepted hosts are `localhost`, `127.0.0.1`, and `[::1]`. HTTPS, remote hosts,
userinfo, paths, queries, and fragments are rejected. `localhost` is
canonicalized to `127.0.0.1`; ambient proxies and redirects are disabled so a
credential-bearing request cannot leave loopback. Requests have bounded
five-second transport timeouts and 8 MiB Relay response limits.

Browser approval normally uses that same loopback origin. If Desktop is opened
from another tailnet machine, set a separately validated, display-only origin:

```text
RELAY_IDE_PUBLIC_URL=http://dev.example.ts.net:3456
```

This value may be an HTTP or HTTPS root URL, but cannot contain credentials,
path, query, or fragment. It is used only to build the browser approval link;
credential-bearing backend requests still use the loopback-only
`RELAY_IDE_URL`.

Provide an existing Relay operator-client token only through the process
environment:

```text
RELAY_IDE_OPERATOR_CLIENT_TOKEN=relay-occ-v1.…
```

Or provide a fresh, approved one-time handshake grant:

```text
RELAY_IDE_OPERATOR_GRANT=relay-ohg-v1.…
```

The legacy `POST /connection/authorize` route redeems that grant with client id
`desktop-plugin-backend`, the read/write context capability pair, and a fixed
15-minute maximum TTL. The approved grant must carry an exact `channelIds`
scope; Relay inherits that scope when minting the credential, so the plugin
cannot widen access during onboarding. Relay Login does **not** mint this
channel-scoped credential, so Desktop gives channel-specific setup guidance
instead of presenting a dead generic authorization button. The raw issued
credential remains in process memory; it is never sent to Desktop, persisted,
logged, added to URLs, or put in plugin configuration. Every supplied or issued
credential is locally bounded to 15 minutes and is cleared on expiry or when
Relay responds 401 or 403.

Harnesses use a separate scoped actor credential. Existing deployments may set
`RELAY_IDE_ACTOR_TOKEN`, supply a one-time `RELAY_IDE_ACTOR_GRANT`, or rely on
the credential written by `relay-ide login` at
`~/.config/relay-ide/actor-token.json`. When none is live, **Connect Harnesses**
starts Relay's browser/PIN device flow. The Python backend captures the approved
`relay-sac-v1…` token exactly once, stores it only in memory, and returns only
public flow state, approval URL, code, and expiry to Desktop. Actor credentials
are read-only (`session:read`) and auto-renew shortly before expiry.

The channel lane's process-local, 15-minute credential is vertical-slice debt,
not durable onboarding. Restarting Relay or Hermes, a revoke, expiry, or a
consumed grant requires fresh operator authorization. This plugin intentionally
does not invent channel credential persistence, secret-store integration,
automatic reissue, or streaming.

## Plugin API

See [docs/desktop.md](docs/desktop.md) for the frozen API. In short:

- `GET /connection/status`
- `GET /connection/onboarding` — returns only the validated loopback Relay root as `{ "url": "..." }` so Desktop can open setup without exposing grants or credentials
- `POST /connection/authorize` (legacy operator/grant redemption)
- `POST /harnesses/login/start`
- `GET /harnesses/login`
- `DELETE /harnesses/login`
- `GET /channels`
- `GET /channels/:id/messages?limit=50`
- `POST /channels/:id/messages` with exactly `text`, `format`, and
  `clientMessageId`

The proxy calls only Relay's stable `channels.list`, `channels.history`, and
`channels.post` operations. Every channel request carries Relay's
operator-client bearer and versioned command/capability headers. Caller
`clientMessageId` is forwarded exactly once and unchanged. Relay 404 and 409
remain meaningful; Relay 5xx and unreachable connections are recoverable.

Only safe display data crosses the boundary. Provider runtime/turn/item/source
correlations, async runs, attachments, arbitrary metadata, and caller-supplied
sender/source are not accepted or returned.

## Verify

```bash
python -m pytest -q
npm test
hermes plugins doctor /path/to/hermes-plugin-relay --ci
```

The focused Python suite uses fake in-memory transports and makes no Relay
network call. Boundary tests deliberately inject synthetic credentials upstream
and verify that none can appear in renderer-facing responses or UI state.
