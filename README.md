# hermes-plugin-relay

External agent participants for Hermes. Claude Code, Codex and a built-in mock
adapter join a Hermes session as first-class, attributed participants:

```text
human in Hermes Desktop ─┐
                         ├─> @claude / @codex participant turn
Hermes agent tool call ──┘                 │
                                           ├─ streams into this Hermes chat
                                           └─ appears as an attributed row
```

A human can `@mention` a participant from the composer, Hermes itself can call
one through the `agent_message` tool, and either way the reply streams into the
same transcript as a durable `participant_message` row — not as opaque tool
output that Hermes has to paraphrase.

The Relay IDE runtime is **not** a dependency. This plugin borrows Relay's
adapter/event-normalization shape as prior art and implements its own adapters.

- Architecture spike: [`docs/spikes/hermes-desktop-relay-agent-mentions.md`](docs/spikes/hermes-desktop-relay-agent-mentions.md)
- Implementation contract: [`docs/contracts/participant-seam-v1.md`](docs/contracts/participant-seam-v1.md)

## Requirements

- Hermes with the participant seam (`tui_gateway/participants.py`).
  Without it the plugin still loads and the doctor still passes, but every
  participant reports status `error` with reason `hermes core seam missing` and
  the tools return a visible error. Nothing crashes.
- Python 3.11+ (stdlib only; FastAPI/pydantic are imported by the dashboard API
  module, which the Hermes dashboard already provides).
- `claude` and/or `codex` on `PATH` for the real adapters. Codex additionally
  needs a login (`~/.codex/auth.json`); the roster reports that honestly.

## Install and enable

There are **two independent switches**. Both must be on.

1. **Hermes plugin system** — install the folder and add it to `plugins.enabled`:

   ```bash
   hermes plugins install /path/to/hermes-plugin-relay
   ```

   or copy it to `~/.hermes/plugins/hermes-plugin-relay/` and enable it in
   `config.yaml`:

   ```yaml
   plugins:
     enabled:
       - hermes-plugin-relay
   ```

   This registers the tools and mounts the REST API at
   `/api/plugins/hermes-plugin-relay/`.

2. **Hermes Desktop plugin toggle** — enable the plugin in Desktop's plugin
   settings. That is what activates the `@` completion source and the composer
   middleware. See [`docs/desktop.md`](docs/desktop.md) for that half.

Enabling only (1) gives you the Hermes-side tools and the REST surface; the `@`
menu will not appear until (2) is on as well.

Verify the backend half:

```bash
hermes plugins doctor /path/to/hermes-plugin-relay --ci
```

> Run it as the `hermes` console script (from anywhere, including this
> directory). Do **not** drive the doctor through `python -c` with this
> directory as the working directory: that puts the plugin root on `sys.path`,
> where `tools.py` shadows Hermes's own top-level `tools` package and
> registration fails with a spurious relative-import error. The file name is
> fixed — declaring `provides_tools` is what makes Hermes import `tools.py` —
> so this applies to every Hermes plugin, not just this one.

## Configuration

Settings live under `plugins.entries.hermes-plugin-relay.settings.*` in
`config.yaml`. No `HERMES_*` environment variable configures behavior.

```yaml
plugins:
  entries:
    hermes-plugin-relay:
      settings:
        # Replaces the built-in claude/codex/mock trio entirely.
        participants:
          - id: claude:default
            handle: claude
            display_name: Claude Code
            adapter: claude          # claude | codex | mock
            model: null              # optional --model
            cwd: null                # optional working directory
            enabled: true
          - id: codex:default
            handle: codex
            display_name: Codex
            adapter: codex
        # Participant-to-participant routing. Off by default.
        chain:
          enabled: false
          turn_cap: 2
        # Bound on the blocking agent_message tool.
        tool_timeout_seconds: 300
        # Default cwd for participant subprocesses. Falls back to the Hermes
        # session cwd, then the user's home directory.
        cwd: null
```

Handles are the `@mention` tokens: lowercase, `[a-z0-9][a-z0-9_-]*`. `@hermes`
is reserved. Invalid entries are skipped with a warning rather than blocking the
plugin from loading.

## Use

**From the composer** (needs the Desktop half): type `@claude take a look at
this diff`. The human message is persisted once, the participant turn streams
back as an attributed row, and Hermes is *not* woken unless `@hermes` was also
addressed.

**From Hermes**, via three tools in the `relay_participants` toolset:

| Tool | Purpose |
| --- | --- |
| `agent_participants_list()` | Roster with honest status (`ready`/`busy`/`offline`/`error`). |
| `agent_message(participant, message)` | Send and block until the turn completes (bounded by `tool_timeout_seconds`). |
| `agent_interrupt(participant)` | Best-effort interrupt of an in-flight turn. |

The participant reply is published through the seam as the canonical visible
row; the tool result carries correlation, status and text for Hermes's own
context. No user row is appended on this path — the tool call is the record of
who asked.

External participant output is untrusted peer content of unbounded size, so the
`text` (and `error`) in the tool result is capped at **16,000 characters** with
the same deterministic truncation marker Hermes core uses for the model
envelope. The full text still lands in the transcript row; only the model-facing
side door is bounded.

Every participant turn is watchdog-bounded by `tool_timeout_seconds` on **all**
dispatch paths, not just the blocking tool. A child that goes silent but stays
alive is failed with `participant turn timed out …`, its adapter is recycled,
and the participant's queue keeps moving.

**REST** (behind the dashboard's auth, mounted at
`/api/plugins/hermes-plugin-relay/`):

- `GET /participants[?session_id=…]` → `{"participants": [...]}`
- `POST /dispatch` → `{"ok", "turns": [{participant_id, participant_turn_id}], "failed": [...], "user_row_appended"}`
- `POST /interrupt` → interrupt status

`POST /dispatch` requires a client-generated `dispatch_id`, unique per submit
attempt and **reused verbatim on transport retry**:

```json
{
  "session_id": "<gateway session id>",
  "dispatch_id": "<opaque id>",
  "text": "@claude take a look",
  "mentions": ["claude"],
  "append_user_message": true
}
```

A duplicate arrival — concurrent or a retry after a lost response — returns the
same turn refs and performs no second user-row append and no second fanout. A
missing `dispatch_id` is a `400` before any side effect. When the idempotency
map is saturated with live entries the endpoint fails closed with `429` rather
than evicting a live entry.

Set `append_user_message: false` when Hermes was also addressed, so the normal
submit persists the human row and you do not get two.

## Chain safety

External participant output is untrusted peer content. Participant-to-participant
routing is therefore off by default. With `chain.enabled: true`:

- a completed reply is scanned for roster mentions;
- self-mentions are ignored;
- onward dispatch is refused above `chain.turn_cap` and the refusal is logged;
- `@hermes` forwarding is **not wired** in slice 1 — the seam is
  `runtime/router.py::forward_to_hermes`, which refuses and logs. Waking Hermes
  with peer text requires the core's queued untrusted-peer path.

## Layout

```text
plugin.yaml              manifest v2: provides_tools, config_schema
__init__.py              register(ctx): tools + on_unload teardown
config.py                participant/adapter/chain settings
tools.py                 the three Hermes tools + UI-session resolution
adapters/
  base.py                adapter protocol + subprocess plumbing (argv, stderr ring, kill ladder)
  claude.py              claude -p stream-json over stdio
  codex.py               codex app-server NDJSON JSON-RPC 2.0
  mock.py                in-process deterministic adapter
runtime/
  events.py              normalized event vocabulary
  manager.py             the single dispatch path, queueing, exactly-once map
  router.py              chain-safety planning and brakes
  persistence.py         provider session ids in the Hermes plugin data dir
dashboard/
  manifest.json          mounts plugin_api.py at /api/plugins/hermes-plugin-relay/
  plugin_api.py          FastAPI router
scripts/                 opt-in real-provider smokes
tests/                   pytest suite
```

## Testing

The suite needs `pytest`, and `fastapi`/`httpx` for the REST tests. The Hermes
virtualenv already has all three:

```bash
cd /path/to/hermes-plugin-relay
/path/to/hermes/.venv/bin/python -m pytest tests/ -q --ignore=tests/desktop
```

`tests/test_integration_seam.py` is the acceptance gate — streamed participant
rows persist and rehydrate through the **real** core seam, a real `SessionDB`
and a temp Hermes home. It skips unless you point it at a Hermes checkout:

```bash
HERMES_AGENT_ROOT=/path/to/hermes-agent \
  /path/to/hermes/.venv/bin/python -m pytest tests/test_integration_seam.py -q
```

Every test redirects `HERMES_HOME` to a temp directory; nothing touches your
real `~/.hermes`, `~/.claude` or `~/.codex` state.

### Real provider smokes

Opt-in, because they spawn a real CLI on your machine. They run in a throwaway
temp directory, pass no permission-bypass flags, write nothing to your config or
credentials, and clean up the child and the directory on exit.

```bash
RELAY_SMOKE=1 python scripts/smoke_claude.py
RELAY_SMOKE=1 python scripts/smoke_codex.py
```

Each sends one harmless prompt, prints the streamed deltas and the terminal
status, and exits non-zero on anything but a completed turn.

## Known limitations (slice 1)

- A `streaming` row whose gateway process dies stays `streaming` with empty
  content in the database; the delta buffer lives in memory only.
- `@hermes` forwarding from a participant is not wired (see Chain safety).
- Reasoning, tool activity, approvals and questions are normalized inside the
  adapters but not surfaced through the seam.
- `agent_message` timing out interrupts the turn and returns
  `status: "timeout"`; the participant's row is finalized as `interrupted`.
- Seam calls (SQLite writes) run inline on the manager's loop thread, so a slow
  database write backpressures that participant's stream.
- **Claude interrupt on a dead child waits out its ack timeout.** The Codex
  adapter fails every in-flight request when the child's stdout closes; the
  Claude adapter does not, so if the CLI dies between the interrupt request and
  its ack, `interrupt()` blocks for the full 5s
  (`adapters/claude.py::INTERRUPT_ACK_SECONDS`) before falling through to the
  kill ladder. Correctness is unaffected — the turn still finalizes as
  `interrupted` — but the call is slower than it needs to be. Fixing it means a
  shared request-correlation helper in `adapters/base.py` that fails all pending
  futures on stdout EOF.
- **A failed chain hop is only visible in the log.** `ChainRouter` records
  policy *refusals* (cap reached) in `router.refusals`, but a hop that fails to
  queue is caught and logged without being recorded, so nothing surfaces it to a
  caller. Fixing it means the chain path returning the same
  `{turns, failed}` shape `/dispatch` already produces.
