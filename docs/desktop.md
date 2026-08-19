# The desktop half (`desktop/plugin.js`)

Implements §9 of [`docs/contracts/participant-seam-v1.md`](contracts/participant-seam-v1.md).

The desktop half is the *mouth* of the seam: it offers participant handles in
the composer's `@` menu and decides where a submitted draft goes. It owns no
subprocesses, no credentials, and no transcript state — everything real happens
behind `ctx.rest` in the plugin's Python half.

```text
composer draft ──> participant-router (middleware)
                     │
                     ├─ no known @handle ─────────────> Hermes (draft unchanged)
                     ├─ @claude + @hermes ────────────> POST /dispatch  AND  Hermes
                     └─ @claude only ─────────────────> POST /dispatch, draft consumed
```

## Install shape

One folder, both halves ([one package, both SDKs][one-package]):

```text
~/.hermes/plugins/hermes-plugin-relay/
├── plugin.yaml            # agent half: tools, skills
├── dashboard/
│   ├── manifest.json      # { "name": "hermes-plugin-relay", "api": "plugin_api.py" }
│   └── plugin_api.py      # GET /participants, POST /dispatch, POST /interrupt
└── desktop/
    └── plugin.js          # this file — loads uncompiled as ESM in the renderer
```

`ctx.rest('/participants')` resolves to `/api/plugins/hermes-plugin-relay/participants`
— the namespace is enforced by the loader, so the plugin cannot address another
plugin's API or a core route.

### Two enable switches, both default off

| Switch | Where | Gates |
|---|---|---|
| `plugins.enabled` allow-list | `~/.hermes/config.yaml` | the **Python** half: tools and `plugin_api.py`. This is a security boundary (GHSA-mcfc-hp25-cjv7), not a preference. |
| Plugin toggle | Hermes Desktop → **Settings ▸ Plugins** | the **desktop** half: this file's contributions. |

Dropping the folder into `~/.hermes/plugins` is inert on every surface until the
user flips both. The plugin also declares `defaultEnabled: false` so it stays
dark even if the folder is installed under `~/.hermes/desktop-plugins/` instead,
where the loader would otherwise default it on.

Only the desktop half enabled → the roster is empty, `@` offers nothing, every
draft passes through to Hermes untouched. That is the designed degradation: a
missing backend must never break the composer.

## The `@` menu

`composer.atCompletions` is called **synchronously on every keystroke** after
the debounce, so `provide(query)` only ever reads a cache:

- the roster is fetched at register, then lazily on a **60 s TTL** (5 s after a
  failed attempt) — `provide()` kicks the refresh and returns the current cache;
- `participant.message.start` / `participant.message.complete` on the gateway
  event stream mark the cache stale, so `status` changes (ready ⇄ busy) show up
  without waiting out the TTL;
- a failed fetch keeps the **last** roster rather than clearing it;
- rows render as `@claude` with meta `External · Claude Code`, plus
  ` · <status>` when the participant is not `ready` (`External · Mock Agent ·
  offline`). Offline participants stay listed on purpose — hiding them turns a
  fixable error into a mystery;
- at most 8 rows, prefix-matched against the handle, case-insensitive.

A submit waits on the roster fetch at most **once per plugin load** (the first
one, if the register-time fetch has not settled yet). After that it routes from
the cache and refreshes in the background, so a slow or dead backend can never
stall the composer.

A backend row is rejected if its handle is missing, malformed, duplicated, or
equal to `hermes` — Hermes' own address can never be claimed by a participant.

## Mention grammar

A mention is `@` + a handle of `[A-Za-z0-9][A-Za-z0-9_-]*`, matched
**case-insensitively anywhere in the draft**, and kept only if the handle is in
the live roster (or is `hermes`).

| Input | Read as | Why |
|---|---|---|
| `@claude review this` | mention | start of text |
| `ping (@Claude) please` | mention | preceded by a non-word character |
| `cc @claude, @codex` | two mentions | order preserved, deduped |
| `mail me at user@codex.com` | not a mention | preceded by a word character |
| `` `@claude` `` / ```` ```@claude``` ```` | not a mention | inline and fenced code is stripped first |
| `@claude-code` (roster has `claude`) | not a mention | handles match whole, never as a prefix |
| `@nobody` | not a mention | not in the roster — left alone as prose |

## Routing matrix

`session_id` is `host.state.focusedSessionId` — the **runtime/gateway** id of
the chat the user is looking at, read at dispatch time (focus moves between
tiles without telling the plugin, so it is never cached).

| Draft | `POST /dispatch` | `append_user_message` | Middleware returns | Effect |
|---|---|---|---|---|
| no known external handle | — | — | the draft | normal Hermes turn |
| `@claude …` | yes | `true` | `{handled:true}` | composer clears, **no** Hermes turn; the backend persists the human row |
| `@claude … @hermes …` | yes | `false` | the draft | participant turn **and** Hermes turn; Hermes' own submit persists the human row (`false` prevents a double row) |
| any of the above, no live session | — | — | the draft | nothing to dispatch into yet |
| dispatch refused pre-acceptance (4xx / marker-free `{ok:false}`) | attempted | — | the draft | nothing was dispatched, so Hermes takes it |
| dispatch committed but partial (`200 {ok:false}` with markers) | attempted | as sent | `{handled:true}` (or the draft on the `@hermes` path) | the send happened; failed participants are named in a notice |
| dispatch ambiguous (transport / timeout / 5xx) | attempted twice | — | `null` | send cancelled, composer restores the draft |

## Failure behavior

Not all failures are equal, and treating them alike is how you get a message
delivered twice or not at all. The question is **what side effects already
exist**, and only a pre-acceptance refusal answers "none":

| Failure | Classification | Response |
|---|---|---|
| 4xx, with or without a `statusCode` property | pre-acceptance — validated and refused **before** any side effect | pass the draft through |
| `200` `{"ok": false}` with **no** `user_row_appended` and **no** `turns` | pre-acceptance | pass the draft through |
| `ctx.rest` unavailable (backend half off) | pre-acceptance — no request ever left the machine | pass the draft through |
| `200` `{"ok": false, "user_row_appended": true}` and/or non-empty `turns` | **committed** (contract §6, v1.5) — the user row exists and/or participants are streaming | treat as accepted: consume the draft, notify about `failed[]` |
| network error, timeout, 5xx, unparseable error | **ambiguous** — the dispatch may already have landed | retry once, then return `null` |

The two non-pre-acceptance rows are the ones that must never pass through.
Doing so would append the human row a second time **and** wake Hermes, who was
never addressed. A committed partial is consumed like a success; an ambiguous
failure cancels the submit so the composer restores the text and the human
decides whether to re-send.

Exactly one toast fires per submit — never one per attempt:

- pre-acceptance: `notifyError` *"Relay could not reach @claude — the message
  went to Hermes instead."*
- ambiguous: `notifyError` *"Relay could not confirm the send to @claude — your
  message was restored and nothing was sent to Hermes."*
- committed partial: `notify` (warning) *"@codex could not start. The rest of
  the message was sent."*

Everything else still degrades to pass-through: a throw anywhere in the
handler, a hostile draft shape, a throwing `host.state` read. `provide()` never
throws either; on any internal error it returns `[]`.

Status is read from `error.statusCode` when present and otherwise parsed out of
the message (`"404: …"`), because the desktop bridge's custom Error properties
do not survive every IPC boundary but its message does. **No parsable status
means ambiguous** — the conservative reading, never a guess.

### Dispatch exactly once

Every `POST /dispatch` carries a `dispatch_id` minted **once per submit
attempt** and reused verbatim on the retry. Exactly-once is enforced
server-side by the `(session_id, dispatch_id)` idempotency map (contract §6): a
duplicate arrival returns the same turn refs, appends no second user row, and
fans out no second time.

The client-side guard is an optimization on top of that, not the correctness
mechanism. It keys on the **draft object**, not its text: the composer builds a
fresh draft per submit, so object identity *is* attempt identity. Concurrent
invocations of one attempt join a single POST; a deliberate re-send — a new
draft object, even with byte-identical text — is a new attempt with a fresh
`dispatch_id`. Keying by text would silently swallow that re-send. The entry is
kept after settling (one draft object dispatches at most once, ever) in a
`WeakMap`, so it disappears with the draft.

### Recursion suppression

Only composer drafts reach the middleware. The gateway-event subscription is
roster-status invalidation and **cannot** dispatch, so participant output that
happens to contain `@claude` or `@hermes` never re-enters this router. Onward
routing of participant text is a backend concern with its own turn cap
(contract §10).

## Limitations (slice 1)

- Attachments on a consumed draft (`@claude only`) are dropped — `/dispatch`
  carries text and mentions only.
- The participant reply becomes a visible transcript row only once the Hermes
  core seam (contract §§1–5) is in place; without it the dispatch still runs and
  the reply is reachable through the plugin's tools.
- `ctx.socket` is unused: the roster refreshes on a TTL plus event
  invalidation, which is enough for a handful of participants and works on OAuth
  remotes, where plugin sockets are a no-op.
- The desktop half only loads for a plugin folder on the machine running the
  app; against a remote backend, the remote box's `~/.hermes/plugins` is not a
  reachable filesystem.

## Tests

`tests/desktop/*.test.mjs` run on the built-in `node:test` runner with no
dependencies and no flags. The repo-root `package.json` exists only to wire
`npm test` — the plugin itself ships no npm dependencies and the desktop half
is loaded from source, never built. They load `desktop/plugin.js` the way the renderer
does — as uncompiled ESM — after rewriting the single `@hermes/plugin-sdk`
import into a per-test stub, importing the result as a `data:` URL so every load
is a fresh module instance.

```bash
npm test                                   # the wired entry point
node --test 'tests/desktop/**/*.test.mjs'  # the same command, directly
node --test                                # bare discovery from the repo root
```

Quote the glob. `node --test tests/desktop/` does **not** work on Node ≥ 22:
positional arguments are glob patterns, and a bare directory matches only
itself.

Coverage: contribution registration, prefix filtering, roster failure and
malformed rows, the full routing matrix, session-id freshness, `dispatch_id`
minting and reuse, the pre-acceptance / committed / ambiguous split with its
retry, draft-identity idempotence, recursion suppression, and every failure
fallback.

The guards are mutation-checked: eighteen single-line breakages — dropping the
dispatch guard, its memoization, draft-identity keying, the committed-result
classification in either direction, the partial-failure notice, the `failed[]`
labelling, the code-fence stripping, the `@hermes` suppression, the mention
boundary, the `dispatch_id`, its reuse across the retry, the retry itself, the
ambiguous-failure cancel, the status classification, the status parse, or the
never-lose-a-message fallback — each turn the suite red.

`_harness.mjs` holds the shared loader. The underscore keeps it out of both
runner forms (verified: neither the glob nor bare discovery treats it as a
suite), so the two test files carry only fixtures and assertions.

[one-package]: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/desktop-plugin-sdk.md
