# hermes-plugin-relay

A thin, backend-only proxy between Hermes Desktop and a local Relay hub. This
is a breaking pivot from the former participant/provider runtime: there are no
Hermes tools, provider adapters, subprocesses, participant skills, persistent
session records, or compatibility endpoints.

The Python dashboard API is mounted at
`/api/plugins/hermes-plugin-relay/`; `desktop/plugin.js` is intentionally a
separate frontend-owned surface. The backend has no CORS middleware and does
not expose `/events`.

## Configure the local Relay connection

The only accepted Relay URL is a loopback HTTP root. The default is:

```text
RELAY_IDE_URL=http://127.0.0.1:3456
```

Accepted hosts are `localhost`, `127.0.0.1`, and `[::1]`. HTTPS, remote hosts,
userinfo, paths, queries, and fragments are rejected. Requests have bounded
five-second transport timeouts and 1 MiB Relay response limits.

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
15-minute maximum TTL. The raw issued credential remains in process memory;
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
/home/donovanyohan/.hermes/hermes-agent/venv/bin/python -m pytest -q --ignore=tests/desktop
hermes plugins doctor /path/to/hermes-plugin-relay --ci
```

The focused Python suite uses fake in-memory transports. It makes no Relay
network call and never puts a credential or grant in an assertion, fixture
name, URL, log, config artifact, or test response.
