# Participant seam contract v1 (frozen for implementation)

Status: implementation contract for the first vertical slice. All four implementation
lanes (Hermes core Python, Hermes Desktop TS, plugin Python, plugin Desktop JS) build
against this file. Deviations require updating this file in the same change.

Scope note: "session_id" in every API below is the **gateway session id** — the same
identifier used in gateway event frames (`params.session_id`) and accepted by
`session.*` RPCs. Core resolves it to the durable DB session internally and raises a
typed error for unknown/non-live sessions.

## 1. Persistence representation (Hermes core)

No SQLite schema change. Two new `display_kind` values on existing `messages` columns:

### 1.1 External participant reply row
- `role`: `"assistant"`
- `display_kind`: `"participant_message"`
- `content`: full final text (empty string while streaming)
- `display_metadata` (JSON object):

```json
{
  "participant": {
    "id": "claude:default",
    "handle": "claude",
    "display_name": "Claude Code",
    "plugin_id": "hermes-plugin-relay",
    "adapter_id": "claude-code-stream-json"
  },
  "participant_turn_id": "pturn-<uuid>",
  "status": "streaming | completed | failed | interrupted",
  "error": "optional string, only when status=failed"
}
```

### 1.2 Participant-directed human row (human @claude message that must not wake Hermes)
- `role`: `"user"`
- `display_kind`: `"participant_directed"`
- `content`: the human text as typed (mentions included)
- `display_metadata`:

```json
{ "mentions": ["claude"], "plugin_id": "hermes-plugin-relay" }
```

## 2. Model-context rules (Hermes core, agent/conversation_loop.py) — v1.1

The conversation is canonical: Hermes MUST be able to understand prior participant
replies on later turns (e.g. "critique Claude's reply above"). Participant rows are
therefore **projected, not dropped**:

- At the pre-repair seam (same location as the existing hidden interrupt-scaffold
  filter, BEFORE `repair_message_sequence_with_cursor`), each row with
  `display_kind == "participant_message"` is REPLACED in the outgoing copy (never in
  persisted history) by a synthetic **`role: "user"`** envelope row:

```text
[external-participant-message id=<participant_turn_id> from="<display Name>" handle=@<handle> status=<status>]
<content, sanitized>
[end-external-participant-message id=<participant_turn_id>]
```

  - Envelope is byte-deterministic: derived ONLY from persisted row fields
    (participant display_name/handle, participant_turn_id, status, content). Same row
    → identical envelope bytes on every rebuild (prompt-cache prefix stability).
  - Bounded: content capped (16,000 chars, deterministic truncation with a marker).
  - Sanitized: any literal `[end-external-participant-message` sequence inside
    content is neutralized (escaped) so untrusted text cannot forge the frame; the
    id-bearing markers make forgery detectable.
  - Rows with `status == "streaming"` are dropped from projection (incomplete);
    `completed`/`interrupted` project their text; `failed` projects text plus an
    `error=` note in the header. A row appearing after completion causes a one-time
    cache re-ingest from that index — accepted.
  - The envelope must NEVER be role system/developer/tool/assistant. Untrusted peer
    content thus never gains system authority, never merges into Hermes assistant
    turns (repair Pass 0 cannot touch user rows), and never silently impersonates
    the human (the frame labels provenance inside the merged user turn).
- Rows with `display_kind == "participant_directed"` stay in model context as the
  user rows they are (genuine human content); consecutive-user merge by repair Pass 2
  (which also merges adjacent envelope rows) is the accepted behavior.
- No change to system prompt, role alternation, caching, compaction. `display_kind`
  rows are already excluded from `is_user_originated_turn` and undo/rewind targeting;
  tests must assert this holds for both new kinds.
- Required tests (behavioral, at the api_messages-build seam):
  1. later-turn continuity — a subsequent Hermes build contains the participant reply
     text inside a user-role message and in NO assistant/system/tool message;
  2. attribution label — envelope header carries handle + display name + turn id;
  3. prompt-injection boundary — content containing a forged end-marker and
     "SYSTEM:"-style text stays fully inside one user-role envelope, produces no
     system/developer row, and does not terminate the frame early;
  4. role repair/caching — [participant_directed user, participant_message assistant,
     human user] projects+repairs to strict alternation with the envelope present
     exactly once, and two consecutive builds over the same history yield
     byte-identical api_messages;
  5. normal transcript behavior — sessions without participant rows project
     byte-identically to before the change.

## 3. Core publisher module (Hermes core, new `tui_gateway/participants.py`)

Public, plugin-facing, in-process API. Thread-safe. All functions raise
`ParticipantSeamError` subtypes on validation failure; they never crash the session.

```python
class ParticipantSeamError(Exception): ...
class UnknownSessionError(ParticipantSeamError): ...
class OwnershipError(ParticipantSeamError): ...
class UnknownTurnError(ParticipantSeamError): ...

def register_participants(session_id: str, plugin_id: str, participants: list[dict]) -> None
    # participant dict: {"id","handle","display_name","adapter_id","status","capabilities":{...}}
    # idempotent upsert; ownership recorded as plugin_id per participant id.

def list_participants(session_id: str) -> list[dict]

def append_participant_user_message(
    session_id: str, plugin_id: str, text: str, mentions: list[str]
) -> int  # returns row_id; persists 1.2 row; appends to live history; emits event 4.1

def begin_participant_message(
    session_id: str, plugin_id: str, participant_id: str, participant_turn_id: str
) -> int  # returns row_id; persists 1.1 row (status=streaming, empty content);
          # appends to live history; emits event 4.2. Ownership-checked.

def append_participant_delta(
    session_id: str, plugin_id: str, participant_turn_id: str, delta: str
) -> None  # buffers text in the active-turn record; emits event 4.3. No DB write.

def complete_participant_message(
    session_id: str, plugin_id: str, participant_turn_id: str, *,
    status: str = "completed", text: str | None = None, error: str | None = None,
) -> None  # final text = explicit text or the delta buffer; updates the row's
           # content + display_metadata.status/error in DB and live history;
           # emits event 4.4; clears the active-turn record.
```

Ownership: `begin/append/complete` require `participant_id`/turn owned by the calling
`plugin_id` for that session. Active turns keyed `(session_id, participant_turn_id)`.

Session-id resolution (v1.2): the publisher key is ALWAYS the live gateway/UI session
id. Core additionally exposes an explicit resolver for non-UI callers:

```python
def resolve_publish_session_id(explicit: str | None = None) -> str
    # explicit id (validated against live sessions) wins; otherwise resolve the
    # calling context's live UI session via the gateway session env
    # (HERMES_UI_SESSION_ID); raises UnknownSessionError when unresolvable.
    # Never falls back to HERMES_SESSION_ID (durable DB id) or any process-global
    # "current session".
```

Tests must prove two concurrent live sessions cannot cross-route: publishing keyed by
session A's id lands rows/events only in A while B is active.

Crash semantics: a `streaming` row whose process dies stays `streaming` with empty
content in DB; documented limitation. Delta buffer lives in memory only.

Concurrency (v1.4):
- `begin_participant_message` RESERVES the `(session_id, participant_turn_id)` slot
  atomically under the registry lock BEFORE any persistence/side effect; a duplicate
  begin for a reserved/active turn raises (no second row, no overwrite). If
  persistence fails after reservation, the reservation is rolled back.
- Turn records carry a terminal/finalizing state checked under the per-turn lock:
  `complete_participant_message` snapshots the buffer and transitions to terminal
  atomically; a delta arriving at/after that transition raises `UnknownTurnError`
  (never silently appended, never emitted after finalization).

Identity validation (v1.4): participant identity fields are untrusted input to the
model-envelope HEADER, not just content. `register_participants` / `begin` enforce:
`handle` matches `[a-z0-9][a-z0-9_-]{0,31}`; `id`/`adapter_id`/`plugin_id` ≤ 128
chars; `display_name` ≤ 64 chars with control characters and newlines stripped and
`"`/`[`/`]` neutralized in the envelope header rendering. Violations raise
`ParticipantSeamError`. Tests must cover header/newline/marker forgery via
display_name and handle.

## 4. Gateway events (emitted via `_emit`, session-scoped; type names frozen)

Payloads are under `params.payload`; `params.session_id` carries the session.

- 4.1 `participant.user_message` — `{"row_id", "text", "mentions", "timestamp"}`
- 4.2 `participant.message.start` — `{"row_id", "participant_turn_id", "participant": {…as 1.1}, "timestamp"}`
- 4.3 `participant.message.delta` — `{"participant_turn_id", "row_id", "text"}` (text = delta chunk)
- 4.4 `participant.message.complete` — `{"participant_turn_id", "row_id", "status", "text", "error?", "timestamp"}` (text = full final text)

All are session-scoped (never added to `UNSCOPED_STREAM_EVENT_TYPES`).

## 5. Hermes Desktop behavior (apps/desktop)

- New branches in the gateway-event dispatch chain for the four event types:
  - `participant.user_message` → append a user ChatMessage (role user) with the text.
  - `participant.message.start` → append pending assistant-role ChatMessage carrying
    `participant` attribution, keyed by `participant_turn_id`.
  - `participant.message.delta` → append text to that pending row.
  - `participant.message.complete` → finalize row (status/error), clear pending state.
- Rehydration parity in `chat-messages.ts`: `display_kind === "participant_message"`
  keeps role `assistant`, parses `display_metadata` (string-or-object guard, same as
  reactions) into a `participant` field on ChatMessage; `participant_directed` renders
  as a normal user bubble. `toRuntimeMessage` carries `participant` through
  `metadata.custom`. **No new role** — the runtime role stays `assistant`.
- Rendering: participant rows render as assistant-style rows with an attribution
  header (display name + @handle, deterministic avatar OK). All new user-facing
  strings go through i18n with all five locales (en, zh, zh-hant, ja, ar).
- Composer middleware extension (generic, not participant-specific):
  `ComposerMiddleware.handler` may additionally resolve to `{ handled: true }`.
  Result contract: draft consumed — composer clears, nothing is submitted, no turn
  starts. `null` keeps its existing meaning (cancel; draft restored).

## 6. Plugin REST surface (dashboard/plugin_api.py, mounted at `/api/plugins/hermes-plugin-relay/`)

- `GET /participants` → `{"participants": [{id, handle, display_name, adapter_id, status, capabilities}]}`
  (status honest: `ready | busy | offline | error`, from real binary/auth probes)
- `POST /dispatch` body (v1.3):

```json
{
  "session_id": "<gateway session id>",
  "dispatch_id": "<opaque client-generated id, unique per submit attempt>",
  "text": "full human text as typed",
  "mentions": ["claude"],
  "append_user_message": true
}
```

  → dispatches one participant turn per mentioned+known participant through the
  runtime manager. `append_user_message=false` when Hermes was also addressed (the
  normal submit persists the human row; prevents double rows). Response:
  `{"ok": true, "turns": [{"participant_id", "participant_turn_id"}]}` or
  `{"ok": false, "error": "..."}` with a 4xx.

  Exactly-once (v1.3): `dispatch_id` is generated ONCE per submit attempt by the
  client and REUSED verbatim across ambiguous transport retries. The runtime manager
  keeps a bounded TTL idempotency map keyed `(session_id, dispatch_id)`; a duplicate
  arrival (concurrent or retry-after-response-loss) returns the SAME turn refs and
  performs NO second user-row append and NO second fanout. Missing `dispatch_id` →
  explicit 4xx validation error (pre-acceptance). Required runtime tests:
  concurrent duplicate POSTs; retry after response loss returns identical turn refs;
  exactly one user row persisted; multi-participant group fanout happens once; map
  is bounded (TTL/size eviction).

  Idempotency-map semantics (v1.4):
  - PRE-ACCEPTANCE failures (validation, unknown session/participants — no side
    effect yet) do not memoize; a retry may re-validate fresh.
  - Once the FIRST side effect occurs (user row appended or any participant turn
    queued), the entry is committed: the deterministic result — including a
    partial-failure result (e.g. user row appended, participant 2 failed to queue)
    — is memoized and returned verbatim to duplicates/retries. An exception after
    a side effect must NOT discard the entry (retry would duplicate the row/fanout).
  - In-flight or committed entries are NEVER evicted by size pressure; when
    max_entries is reached with all entries in-flight/unexpired, new dispatches
    fail closed with a visible capacity error. TTL eviction applies only to
    settled+expired entries.
  - Required tests: failure after user-row append → retry returns memoized partial
    result, no second row; partial group failure memoized; max-in-flight pressure →
    capacity error, no eviction of live entries.

  Durable-finalize failure (v1.4): if the seam's `complete_participant_message`
  fails (row would stay streaming/empty), the turn result reported to tool/REST
  callers must be a visible FAILURE (not success), and chain routing for that turn
  is suppressed. Test it. The seam itself must RAISE when the durable row update
  matches no row (v1.5) — a silent False return defeats this chain.

  Client interpretation of results (v1.5): an HTTP 200 with `ok:false` is a
  COMMITTED (possibly partial) result, not a rejection — side effects may exist
  (`user_row_appended:true` and/or non-empty `turns`). The Desktop client must NOT
  pass the draft through to Hermes in that case (that duplicates the user row and
  wakes Hermes unaddressed); only explicit pre-acceptance 4xx classes allow
  pass-through.
- `POST /interrupt` body `{"session_id", "participant_id"}` → best-effort interrupt.

## 7. Hermes-facing tools (plugin Python)

- `agent_participants_list()` → JSON roster (same shape as GET /participants).
- `agent_message(participant: str, message: str)` → dispatches through the SAME
  runtime-manager path as `/dispatch` (no user row appended; the tool call itself is
  the record of who asked). Blocks until the participant turn completes (bounded
  timeout, config default 300s) and returns JSON
  `{"ok", "participant_id", "participant_turn_id", "status", "text", "error?"}`.
  The participant reply is ALSO published through the seam (attributed row is the
  canonical visible reply; the tool result is correlation/status + content for
  Hermes's own context). The `text` field in the tool result is capped at the same
  16,000-char bound with the same deterministic truncation marker as the model
  envelope (v1.5) — untrusted peer output must not reach Hermes unbounded through
  the tool-result side door.
- `agent_interrupt(participant: str)` → best-effort interrupt, JSON status.

Tool-path session resolution (v1.2): tool handlers obtain the target session id via
`gateway.session_context.get_session_env("HERMES_UI_SESSION_ID")` — the live
gateway/UI session id. NEVER `HERMES_SESSION_ID` (durable DB id) and never a
process-global current session. When that env is unset (pure CLI context), call the
core resolver `tui_gateway.participants.resolve_publish_session_id()`; if still
unresolved, return a visible JSON error. Tests must prove model tool dispatch
publishes into the same Desktop session the tool call ran in, and that two
concurrent sessions dispatching simultaneously cannot cross-route.

## 8. Adapter contract (plugin Python, adapters/base.py)

Async, asyncio-native. Normalized event dataclasses (vocabulary):
`session_updated(provider_session_id)`, `turn_started`, `message_delta(text)`,
`turn_completed(status: completed|failed|interrupted, text, error?)`.
(Reasoning/tool events may exist internally but are not part of slice-1 seam output.)

```python
class AgentAdapter(Protocol):
    id: str
    capabilities: AdapterCapabilities  # text, streaming, interrupt, resume — honest
    async def start_turn(self, turn: TurnInput) -> None
    async def interrupt(self) -> None
    async def close(self) -> None
    def subscribe(self, handler) -> Callable[[], None]
```

- Claude adapter: `claude -p --input-format stream-json --output-format stream-json
  --verbose --include-partial-messages` (+ `--permission-mode default`, `--resume <id>`
  when resuming). NDJSON stdin user frames; consume `system/init` (session_id),
  `stream_event/content_block_delta/text_delta`, terminal `result`
  (failure = `subtype != "success"` or `is_error`). Interrupt via
  `control_request{subtype:"interrupt"}` with nested-request_id ack rule.
- Codex adapter: `codex app-server` NDJSON JSON-RPC 2.0 (no Content-Length);
  `initialize` → `initialized`; `thread/start {cwd}` / `thread/resume {threadId}`;
  `turn/start {threadId, input:[{type:"text",text}]}`; consume `thread/started`,
  `item/agentMessage/delta {itemId, delta}`, `item/completed`, `turn/completed
  {turn.status}`; interrupt via `turn/interrupt {threadId, turnId}` and wait for the
  interrupted `turn/completed`. Terminal grace (v1.4): `turn/completed` may arrive
  before the final `item/completed` — and the final `item/completed` may be the FIRST
  sight of the message text (no prior delta, no prior `item/started`). Apply the
  ~250ms terminal grace whenever final text is not yet known, not only when an open
  item was observed; a late `item/completed` inside the grace supplies the text.
- Spawn: argv lists only, never shell strings. Bounded stderr ring (50 lines),
  bounded line size, no credentials in events/logs. Provider session/thread ids
  persisted per (hermes session_id, participant_id) in the plugin data dir.
- Unit tests use fake subprocess/scripted protocol fixtures. Real smokes opt-in only.

## 9. Desktop plugin behavior (desktop/plugin.js)

- Registers `composer.atCompletions` source from the cached `GET /participants`
  roster (`@claude`, `@codex`, …; sync provide from cache, async refresh).
- Registers `composer.middleware`:
  - Parse mentions (leading-anchored or anywhere; word-boundary `@handle` tokens
    matching known roster handles; `@hermes` recognized as Hermes address).
  - No known external mentions → pass draft through unchanged.
  - External mentions AND Hermes addressed (`@hermes` present) → POST /dispatch with
    `append_user_message:false`, then pass draft through (Hermes turn proceeds).
  - External mentions only → POST /dispatch with `append_user_message:true`, return
    `{handled: true}` on success.
  - Failure handling (v1.3): a `dispatch_id` is generated once per submit attempt
    and reused verbatim on retry. An explicit pre-acceptance 4xx (server validated
    and rejected before doing anything) MAY pass the draft through to Hermes
    unchanged. An AMBIGUOUS failure (network error, timeout, 5xx — the dispatch may
    or may not have been accepted) must NOT pass through (that could both wake
    Hermes and duplicate the participant send): retry once with the same
    `dispatch_id`; if still ambiguous, return `null` so the composer restores the
    draft, and surface one notifyError.
  - Generated/participant text never re-enters this middleware (it only sees
    composer drafts); recursion suppression for router paths lives backend-side.
- Exactly-once is enforced server-side by the `(session_id, dispatch_id)` idempotency
  map (§6); the client contributes by never minting a fresh `dispatch_id` for a
  retry of the same attempt. Frontend draft-identity guards are an optimization,
  not the correctness mechanism.

## 10. Chain safety foundation (plugin runtime/router.py)

- Config `chain.enabled` (default false), `chain.turn_cap` (default 2).
- When enabled: completed participant text is scanned for roster mentions /
  `@hermes`; router may dispatch onward with `chain_depth+1`; dispatch refused above
  cap; self-mentions ignored. `@hermes` routing lands as a **queued untrusted-peer
  user message** only via explicit config, not in slice 1 defaults. Tests cover cap
  and self-mention suppression with chain enabled + mock adapters.

## 11. Compatibility

- Plugin detects the seam via `import tui_gateway.participants`; absence → participants
  report status `error` with reason `hermes core seam missing` in roster, tools return
  a visible error, doctor still passes (registration must not crash).
- Manifest: `manifest_version: 2`, `name: hermes-plugin-relay`.
