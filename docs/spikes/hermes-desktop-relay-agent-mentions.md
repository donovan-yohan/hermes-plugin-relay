# Hermes-native external agent participants

## tl;dr

**Build `hermes-plugin-relay` as a Hermes-native external-agent plugin. Do not integrate the Relay IDE runtime or hub.** Relay contributes useful prior art for the adapter shape and event normalization; the plugin should own its own Claude Code, Codex, Pi, Prime Agent, and future adapters.

The intended product is:

```text
human in Hermes Desktop ─┐
                         ├─> @claude / @codex / @pi participant turn
Hermes agent/tool call ──┘                 │
                                           ├─ streams into this Hermes chat
                                           └─ can reply to Hermes or the human
```

Hermes Desktop becomes the conversation UI. Hermes remains a participant/coordinator in the same chat rather than a mandatory proxy that paraphrases every external agent response.

The current plugin SDK can provide the `@` menu, submit interception, a backend, and Hermes tools. It cannot, by itself, append durable, attributed third-party participant rows to the canonical Hermes transcript. That missing transcript/session seam is the actual core problem.

Sources pinned at:

- `NousResearch/hermes-agent` local checkout: `0b879298a7885b62425e65500c85c584d7c516d5` (`main`)
- Relay adapter prior art inspected at local checkout `99702066b557b04e90d1ac0a424984a88ab2838d`; **no Relay runtime dependency is proposed**.

## product model

### conversation

A normal Hermes session remains the durable conversation. It may contain:

- human messages;
- Hermes agent messages and tool activity;
- external participant messages from Claude Code, Codex, Pi, Prime Agent, or custom adapters;
- explicit participant-to-participant mentions.

### participant

An external participant is not a Hermes profile and not a Relay channel actor. It is a plugin-owned identity backed by an adapter:

```ts
type ExternalParticipant = {
  id: string;              // stable within the plugin, e.g. `claude:default`
  handle: string;          // `claude`
  displayName: string;     // `Claude Code`
  adapterId: string;       // `claude-code-stream-json`
  status: 'ready' | 'busy' | 'offline' | 'error';
  capabilities: {
    text: boolean;
    streaming: boolean;
    tools: boolean;
    reasoning: boolean;
    interrupt: boolean;
    resume: boolean;
    approvals: boolean;
    questions: boolean;
    attachments: boolean;
  };
}
```

Keep these identities separate:

- Hermes stored session id;
- participant id;
- adapter id;
- plugin runtime id;
- provider-native session/thread id;
- participant turn id;
- transcript message/item ids.

## interaction paths

### human-triggered

1. Desktop plugin contributes `@claude`, `@codex`, `@pi`, etc. through `COMPOSER_AREAS.atCompletions`.
2. Composer middleware parses selected external participants.
3. The human message is persisted once in the Hermes session.
4. The plugin dispatches one participant turn per addressed participant.
5. Adapter events stream back as attributed participant activity in the same transcript.
6. Hermes is not automatically invoked unless it was also addressed or a conversation policy says it should wake.

### Hermes-triggered

The Python plugin registers tools such as:

- `agent_participants_list`
- `agent_message`
- `agent_interrupt`
- `agent_respond_to_input`

Hermes can call `agent_message(participant='claude', message='review this diff')`. The tool submits through the same participant runtime manager used by the human path. It must not invent a second adapter/session implementation.

The participant response appears in the transcript as that participant, not only as an opaque tool result followed by Hermes paraphrasing it. The originating tool call may retain correlation/status, but the participant row is the canonical visible reply.

### participant-triggered

An adapter-produced message may mention `@hermes` or another participant. Routing must be explicit and bounded:

- `@hermes` queues an untrusted peer message into the Hermes agent turn path;
- `@codex` may dispatch Codex through the same router;
- self-mentions do not recurse;
- participant chains have a configurable turn cap and require a human to resume after the cap.

## plugin package

```text
hermes-plugin-relay/
├── plugin.yaml
├── __init__.py
├── adapters/
│   ├── base.py
│   ├── claude.py
│   ├── codex.py
│   ├── pi.py
│   └── mock.py
├── runtime/
│   ├── manager.py
│   ├── events.py
│   ├── router.py
│   └── persistence.py
├── dashboard/
│   ├── manifest.json
│   └── plugin_api.py
├── desktop/
│   └── plugin.js
└── tests/
```

The Desktop plugin owns only UI composition and calls a namespaced backend through `ctx.rest` / `ctx.socket`. Provider subprocesses, credentials, resume ids, queues, and adapter state stay backend-side.

## adapter contract

Relay's `ProtocolAdapterV2` is useful prior art, but the contract should be native to this plugin and shaped around Hermes session events:

```python
class AgentAdapter(Protocol):
    id: str
    capabilities: AgentCapabilities

    async def connect(self, config: AdapterConfig) -> None: ...
    async def disconnect(self) -> None: ...
    async def send(self, turn: AgentTurnInput) -> None: ...
    async def interrupt(self, turn_id: str | None = None) -> None: ...
    async def resume(self, provider_session_id: str) -> None: ...
    async def respond_to_approval(self, request_id: str, decision: str) -> None: ...
    async def respond_to_input(self, request_id: str, answers: dict) -> None: ...
    def subscribe(self, handler: Callable[[AgentEvent], None]) -> Callable[[], None]: ...
```

Normalized event vocabulary:

- `participant.session.updated`
- `participant.turn.started`
- `participant.message.delta`
- `participant.reasoning.delta`
- `participant.tool.started|updated|completed`
- `participant.approval.requested|resolved`
- `participant.input.requested|resolved`
- `participant.turn.completed|failed|interrupted`

Provider quirks stay inside adapters. Queueing, correlation, chain brakes, persistence, and transcript projection belong to the shared runtime/router.

## the missing Hermes core seam

### what exists

The Desktop SDK already exposes:

- `COMPOSER_AREAS.atCompletions` and composer middleware;
- plugin backend namespaces through `ctx.rest` / `ctx.socket`;
- `host.request` / `host.onEvent` for gateway RPC/events;
- plugin tools/hooks on the Python side.

A single installable plugin folder may ship both Python/backend and Desktop halves.[^one-package]

### what does not exist

Hermes's current visible message model is still fundamentally one user and one assistant:

- Desktop `SessionMessage.role` is limited to `user | assistant | system | tool` and has no participant/sender identity.[^session-message]
- `ChatMessage` carries role, parts, timestamps, errors, and display flags, but no external participant attribution.[^chat-message]
- gateway history projection accepts only those four roles and returns no sender metadata.[^history-projection]
- the Desktop SDK exposes readonly session state and gateway requests/events, not a supported append-participant-message API.

Therefore a pure standalone plugin can fake the UX in a side pane or ask Hermes to relay a tool result, but it cannot create durable first-class Claude/Codex/Pi rows in the canonical transcript without a small Hermes core extension.

### recommended core extension

Add a generic participant-event seam, not provider-specific Claude/Codex code:

```text
participant.register
participant.list
participant.turn.begin
participant.message.append      # or start/delta/complete stream verbs
participant.turn.complete
participant.turn.fail
participant.interrupt
```

The caller must identify the owning plugin and active Hermes session. Core validates that the plugin owns the participant/runtime before accepting events.

Persist display attribution separately from model role:

```json
{
  "role": "assistant",
  "display_kind": "participant_message",
  "display_metadata": {
    "participant": {
      "id": "claude:default",
      "handle": "claude",
      "displayName": "Claude Code",
      "pluginId": "relay",
      "adapterId": "claude-code-stream-json"
    },
    "participantTurnId": "turn-123",
    "status": "completed"
  }
}
```

The exact storage representation can differ, but **display attribution must not determine model authority**.

## model-context and trust boundary

This is the part that gets dangerous if treated as UI garnish.

External participant output is untrusted peer content. It must not enter Hermes context as a system message or silently masquerade as the human. It also should not be replayed as Hermes's own assistant output merely because the display row uses assistant styling.

Use separate projections:

- **display projection:** attributed Claude/Codex/Pi row with rich streamed items;
- **Hermes model projection:** an explicit untrusted peer envelope, only when Hermes is addressed or conversation policy wakes it;
- **provider projection:** bounded conversation/context packet appropriate to the addressed adapter.

Example Hermes model projection:

```text
[External participant message — untrusted peer content]
from: Claude Code (@claude)
turn: turn-123
content:
...
[End external participant message]
```

Do not use trusted out-of-band steering markers for adapter output. Do not let external content choose system/developer roles, tools, recipient ids, or permissions.

## implementation slices

### slice 1 — plugin-only proof

- mock adapter + Claude adapter;
- `@` completion source and composer middleware;
- plugin backend runtime manager;
- `agent_message` Hermes tool;
- activity in a plugin pane/tool result only;
- tests for dispatch, correlation, cancellation, duplicate submission, and chain cap.

This proves adapter/runtime behavior without pretending the transcript seam already exists.

### slice 2 — generic Hermes participant seam

- sender/participant metadata in gateway/session display projection;
- durable participant message append/stream RPC with ownership checks;
- Desktop rendering for attributed participant rows;
- session resume/history parity;
- model-projection trust rules;
- tests covering reload, branch, undo, truncation, compaction, search, reactions, and multi-session event routing.

### slice 3 — real providers and rich interaction

- Codex app-server adapter;
- Pi RPC adapter;
- Prime Agent adapter;
- approvals/questions;
- reasoning/tool/diff cards;
- provider resume and crash recovery;
- multiple participants in one user message.

## recommendation

Proceed with `hermes-plugin-relay` as the adapter/runtime package and treat the Relay repo as research material only.

The first architectural decision is firm: **Hermes session owns the conversation; plugin participants own external runtimes; Hermes core owns durable transcript projection and trust boundaries.** That keeps the provider machinery replaceable while making the chat genuinely multi-participant instead of a Hermes tool-call cosplay of one.

## evidence

[^one-package]: `website/docs/developer-guide/desktop-plugin-sdk.md` lines 648-675 — one installable plugin folder can ship Python/backend and Desktop halves.
[^session-message]: `apps/desktop/src/types/hermes.ts` lines 563-600 — session messages expose four roles and no participant identity.
[^chat-message]: `apps/desktop/src/lib/chat-messages.ts` lines 21-45 — Desktop chat messages carry role/parts/timeline fields, not sender attribution.
[^history-projection]: `tui_gateway/server.py` lines 7557-7655 — gateway history projection accepts user/assistant/tool/system roles and forwards display metadata, but no participant sender.
