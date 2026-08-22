# hermes-plugin-relay

A native Hermes Desktop workspace for channels on a local Relay hub. The
uncompiled Desktop plugin contributes a `/relay` page and sidebar entry; a thin
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
enable **Relay** in Hermes Desktop under **Settings → Plugins**, then open
`/relay` from the sidebar. Relay itself must be running locally with the
operator-client credential endpoints described below.

## Desktop workspace

The `/relay` page lists accessible Relay channels, renders the selected
channel's latest 50 messages with human/agent/system attribution, and posts
Markdown messages with a stable client-generated idempotency key. It keeps a
stale transcript and unsent draft visible while Relay is offline, and renders
authorization, empty, archived, malformed, missing-channel, and upstream-error
states separately.

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

Provide an existing Relay operator-client token only through the process
environment:

```text
RELAY_IDE_OPERATOR_CLIENT_TOKEN=relay-occ-v1.…
```

Or provide a fresh, approved one-time handshake grant:

```text
RELAY_IDE_OPERATOR_GRANT=relay-ohg-v1.…
```

`POST /connection/authorize` redeems that grant with client id
`desktop-plugin-backend`, the read/write context capability pair, and a fixed
15-minute maximum TTL. The approved grant must carry an exact `channelIds`
scope; Relay inherits that scope when minting the credential, so the plugin
cannot widen access during onboarding. The raw issued credential remains in process memory;
it is never sent to Desktop, persisted, logged, added to URLs, or put in plugin
configuration. Every supplied or issued credential is locally bounded to 15
minutes and is cleared on expiry or when Relay responds 401 or 403.

A process-local, 15-minute credential is vertical-slice debt, not durable
onboarding. Restarting Relay or Hermes, a revoke, expiry, or a consumed grant
requires fresh operator authorization. This plugin intentionally does not
invent credential persistence, secret-store integration, automatic reissue, or
streaming.

## Plugin API

See [docs/desktop.md](docs/desktop.md) for the frozen API. In short:

- `GET /connection/status`
- `POST /connection/authorize`
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

The focused Python suite uses fake in-memory transports. It makes no Relay
network call and never puts a credential or grant in an assertion, fixture
name, URL, log, config artifact, or test response.
