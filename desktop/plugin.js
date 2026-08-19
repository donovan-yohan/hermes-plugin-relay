/**
 * hermes-plugin-relay — desktop half (participant seam contract v1.3, §9).
 *
 * Two contributions, no UI:
 *
 *   composer.atCompletions — `@claude`, `@codex`, … from the cached
 *     `GET /participants` roster. `provide()` is called per keystroke and MUST
 *     answer synchronously, so it only ever reads the cache; the fetch happens
 *     out of band (at register, on a 60s TTL, and whenever the backend says a
 *     participant turn started/finished).
 *
 *   composer.middleware — routes a submitted draft. `@claude do X` goes to the
 *     participant instead of Hermes; `@claude and @hermes do X` goes to BOTH
 *     (the participant via /dispatch, Hermes via the normal turn). Anything
 *     without a known external handle passes through untouched.
 *
 * Invariants this file is responsible for:
 *   - A message is NEVER lost, and never sent twice. Only a PRE-ACCEPTANCE
 *     refusal (4xx, or an `ok:false` body with no side-effect markers) passes
 *     the draft through to Hermes, because only there is it certain nothing was
 *     dispatched. A committed result — including a 200 `ok:false` partial —
 *     consumes the draft; an AMBIGUOUS failure (transport, timeout, 5xx)
 *     returns null so the composer restores it. Passing either of those through
 *     would duplicate the user row AND wake Hermes unaddressed.
 *   - One submit dispatches AT MOST once. Every attempt carries the same
 *     `dispatch_id`, which is what makes delivery exactly-once (the server's
 *     idempotency map, contract §6); the draft-identity guard on concurrent
 *     handler invocations is an optimization on top of it.
 *   - Participant output never re-enters this file. Only composer drafts reach
 *     the middleware; the gateway-event subscription below is roster-status
 *     invalidation only and cannot dispatch. Onward routing of participant
 *     text (chains) is a backend concern — see contract §10.
 *   - Nothing here throws. The SDK treats a throwing contribution as
 *     pass-through, but relying on that would make failures invisible; every
 *     entry point is explicitly guarded instead.
 *
 * Loaded UNCOMPILED as ESM in the renderer: no JSX, no TypeScript, and
 * `@hermes/plugin-sdk` is the only specifier that resolves (react is not
 * imported because this plugin renders nothing).
 */

import { COMPOSER_AREAS, host } from '@hermes/plugin-sdk'

const PLUGIN_ID = 'hermes-plugin-relay'

/** Hermes' own address. Never an external participant, even if a backend
 *  claims the handle — that would make "external mentions only" mis-route. */
const HERMES_HANDLE = 'hermes'

/** Dispatch outcomes. The distinction that matters is REJECTED (the server
 *  answered and declined, so NOTHING was dispatched) versus UNKNOWN (transport
 *  failure — the dispatch may or may not have landed). */
const DISPATCH_ACCEPTED = 'accepted'
const DISPATCH_REJECTED = 'rejected'
const DISPATCH_UNKNOWN = 'unknown'

const ROSTER_TTL_MS = 60_000
/** After a failed roster fetch, retry sooner than the success TTL — a boot
 *  race with the backend shouldn't cost a full minute of dead completions. */
const ROSTER_RETRY_MS = 5_000
const ROSTER_TIMEOUT_MS = 5_000
const DISPATCH_TIMEOUT_MS = 30_000
const MAX_COMPLETIONS = 8

/** Gateway events that can change a participant's `status` (ready ⇄ busy).
 *  The listener ONLY marks the roster stale — it must never dispatch. */
const ROSTER_INVALIDATING_EVENTS = ['participant.message.start', 'participant.message.complete']

// ── module state ─────────────────────────────────────────────────────────────

let pluginCtx = null
/** Normalized participants, in backend order. */
let roster = []
/** Lowercase handles of `roster`, for O(1) mention matching. */
let rosterHandles = new Set()
/** Freshness stamp of the last settled fetch (0 = stale/never). */
let rosterCheckedAt = 0
/** Whether that last settled fetch succeeded — picks the TTL to honour. */
let rosterOk = false
/** Whether ANY fetch has settled. Distinct from the freshness stamp, which an
 *  event invalidation resets: only the very first submit may wait on REST. */
let rosterAttempted = false
/** Single-flight guard for the roster fetch. */
let rosterInFlight = null
/** draft object → its one dispatch outcome. The no-double-dispatch guard;
 *  see dispatchOnce() for why identity, not text, is the key. */
let attemptOutcomes = new WeakMap()
/** Event-subscription disposers, released on ctx.onDispose. */
let disposers = []

// ── roster ───────────────────────────────────────────────────────────────────

/**
 * Coerce one `GET /participants` row into the shape the rest of this file
 * uses. Returns null for anything unusable so a malformed backend row can't
 * poison mention matching.
 */
function normalizeParticipant(raw) {
  if (!raw || typeof raw !== 'object') {
    return null
  }

  const handle = String(raw.handle == null ? '' : raw.handle)
    .trim()
    .replace(/^@+/, '')
    .toLowerCase()

  if (!/^[a-z0-9][a-z0-9_-]*$/.test(handle) || handle === HERMES_HANDLE) {
    return null
  }

  const displayName = String(raw.display_name == null ? '' : raw.display_name).trim()

  // Only what the two consumers need: the `@` menu row and mention matching.
  // Dispatch addresses participants by handle (contract §6), so the backend's
  // `id` / `adapter_id` / `capabilities` stay backend-side.
  return {
    displayName: displayName || handle,
    handle,
    status: String(raw.status == null ? 'offline' : raw.status)
  }
}

function applyRoster(participants) {
  roster = participants
  rosterHandles = new Set(participants.map(participant => participant.handle))
}

async function fetchRoster() {
  const ctx = pluginCtx

  if (!ctx || typeof ctx.rest !== 'function') {
    throw new Error('plugin REST bridge unavailable')
  }

  const response = await ctx.rest('/participants', { timeoutMs: ROSTER_TIMEOUT_MS })
  const rows = response && Array.isArray(response.participants) ? response.participants : []
  const next = []
  const seen = new Set()

  for (const row of rows) {
    const participant = normalizeParticipant(row)

    if (participant && !seen.has(participant.handle)) {
      seen.add(participant.handle)
      next.push(participant)
    }
  }

  applyRoster(next)
}

function rosterIsFresh() {
  if (!rosterCheckedAt) {
    return false
  }

  return Date.now() - rosterCheckedAt < (rosterOk ? ROSTER_TTL_MS : ROSTER_RETRY_MS)
}

/**
 * Resolve once the roster is fresh enough to answer with. Single-flight, and
 * it NEVER rejects: a failed fetch keeps the previous roster (an empty one on
 * a cold start), because a dead backend must degrade to "no external
 * participants", not to a broken composer.
 */
function ensureRoster() {
  if (rosterInFlight) {
    return rosterInFlight
  }

  if (rosterIsFresh()) {
    return Promise.resolve()
  }

  rosterInFlight = Promise.resolve()
    .then(fetchRoster)
    .then(
      () => true,
      () => false
    )
    .then(ok => {
      rosterOk = ok
      rosterAttempted = true
      rosterCheckedAt = Date.now()
      rosterInFlight = null
    })

  return rosterInFlight
}

/** Drop the freshness stamps so the next completion/submit refetches. */
function invalidateRoster() {
  rosterCheckedAt = 0
}

function participantMeta(participant) {
  const suffix = participant.status && participant.status !== 'ready' ? ` · ${participant.status}` : ''

  return `External · ${participant.displayName}${suffix}`
}

// ── mentions ─────────────────────────────────────────────────────────────────

/** Fenced and inline code is quoted text, not an address. */
function stripCode(text) {
  return text.replace(/```[\s\S]*?```/g, ' ').replace(/`[^`\n]*`/g, ' ')
}

/**
 * Every `@handle` in the draft that names a KNOWN participant, plus whether
 * Hermes itself was addressed. Case-insensitive, order-preserving, deduped.
 *
 * The leading `[^A-Za-z0-9_@]` guard is what keeps `user@example.com` from
 * reading as a mention of `@example`; matching against the live roster does
 * the rest, so an unknown `@handle` is left alone as ordinary prose.
 */
function parseMentions(text) {
  const mentions = []
  let hermes = false

  for (const match of stripCode(text).matchAll(/(^|[^A-Za-z0-9_@])@([A-Za-z0-9][A-Za-z0-9_-]*)/g)) {
    const handle = match[2].toLowerCase()

    if (handle === HERMES_HANDLE) {
      hermes = true
      continue
    }

    if (rosterHandles.has(handle) && !mentions.includes(handle)) {
      mentions.push(handle)
    }
  }

  return { hermes, mentions }
}

// ── dispatch ─────────────────────────────────────────────────────────────────

/**
 * Runtime (gateway) session id of the chat the user is looking at, read at
 * call time — never cached, because focus moves between tiles without the
 * plugin hearing about it.
 */
function focusedSessionId() {
  try {
    const state = host && host.state
    const atom = state && state.focusedSessionId
    const value = atom && typeof atom.get === 'function' ? atom.get() : null
    const id = value == null ? '' : String(value).trim()

    return id || null
  } catch {
    return null
  }
}

function reportDispatchFailure(error, mentions, outcome) {
  try {
    const who = mentions.map(handle => `@${handle}`).join(', ') || 'the external participants'

    host.notifyError(
      error,
      outcome === DISPATCH_REJECTED
        ? `Relay could not reach ${who} — the message went to Hermes instead.`
        : `Relay could not confirm the send to ${who} — your message was restored and nothing was sent to Hermes.`
    )
  } catch {
    // Toasts are best-effort; losing one must not change routing.
  }
}

/** Name one entry of a `failed[]` array — `{participant_id, error}` rows, per
 *  runtime/manager.py. '' for a row without an id, which the caller drops. */
function failedLabel(entry) {
  return String(entry?.participant_id ?? '')
}

/**
 * A committed partial: the send happened, some participants did not start.
 * A notice, not an error — the message was NOT lost and Hermes was not woken.
 */
function reportPartialDispatch(response) {
  try {
    const failed = Array.isArray(response.failed) ? response.failed : []
    const who = failed.map(failedLabel).filter(Boolean).join(', ')

    host.notify({
      kind: 'warning',
      message: who
        ? `${who} could not start. The rest of the message was sent.`
        : String(response.error || 'Some participants could not start.'),
      title: 'Relay: partial dispatch'
    })
  } catch {
    // Toasts are best-effort; losing one must not change routing.
  }
}

/**
 * HTTP status behind a `ctx.rest` rejection, or 0 when there isn't one.
 *
 * The desktop bridge rejects with `Error(`${statusCode}: ${body}`)` carrying a
 * `statusCode` property, but that property does not survive every IPC
 * boundary — the message does. Read both, and treat "no status" as a transport
 * failure rather than guessing.
 */
function httpStatus(error) {
  if (!error || typeof error !== 'object') {
    return 0
  }

  const explicit = error.statusCode == null ? error.status : error.statusCode

  if (Number.isInteger(explicit)) {
    return explicit
  }

  const match = /(?:^|\D)([45]\d{2}):\s/.exec(String(error.message == null ? '' : error.message))

  return match ? Number(match[1]) : 0
}

/**
 * Opaque id for ONE submit attempt. The server's `(session_id, dispatch_id)`
 * idempotency map is what makes delivery exactly-once (contract §6); this side
 * only has to never mint a fresh id for a retry of the same attempt.
 */
function newDispatchId() {
  try {
    const webcrypto = globalThis.crypto

    if (webcrypto && typeof webcrypto.randomUUID === 'function') {
      return webcrypto.randomUUID()
    }

    if (webcrypto && typeof webcrypto.getRandomValues === 'function') {
      return Array.from(webcrypto.getRandomValues(new Uint8Array(16)), byte =>
        byte.toString(16).padStart(2, '0')
      ).join('')
    }
  } catch {
    // Fall through: uniqueness is what this id needs, not unpredictability.
  }

  return `d-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

/**
 * Did this `ok:false` body leave side effects behind? (contract §6, v1.5)
 *
 * The server answers 200 `ok:false` for a COMMITTED partial failure — the human
 * row is already persisted and/or some participants are already streaming, and
 * only the rest failed to queue. `user_row_appended` and a non-empty `turns`
 * are the markers. Treating that as a rejection and passing the draft through
 * would duplicate the user row AND wake Hermes unaddressed.
 */
function isCommittedResult(response) {
  return response.user_row_appended === true || (Array.isArray(response.turns) && response.turns.length > 0)
}

/**
 * One POST, classified:
 *   accepted — the server took it, wholly or partly. A partial result carries
 *              `partial` so the caller can say which participants failed;
 *   rejected — the server answered and declined BEFORE any side effect (4xx, or
 *              an `ok:false` body with no side-effect markers). Nothing was
 *              dispatched, so the draft may still go to Hermes;
 *   unknown  — transport failure, timeout, or 5xx. The dispatch may or may not
 *              have been accepted; the caller must not assume either way.
 * Never rejects.
 */
async function attemptDispatch(ctx, body) {
  try {
    const response = await ctx.rest('/dispatch', { body, method: 'POST', timeoutMs: DISPATCH_TIMEOUT_MS })

    if (response && response.ok === false) {
      return isCommittedResult(response)
        ? { outcome: DISPATCH_ACCEPTED, partial: response }
        : { error: new Error(String(response.error || 'dispatch refused')), outcome: DISPATCH_REJECTED }
    }

    return { outcome: DISPATCH_ACCEPTED }
  } catch (error) {
    const status = httpStatus(error)

    return { error, outcome: status >= 400 && status < 500 ? DISPATCH_REJECTED : DISPATCH_UNKNOWN }
  }
}

/**
 * POST /dispatch for ONE submit attempt. An ambiguous attempt is retried once
 * with the same `dispatch_id`, so a duplicate arrival collapses server-side
 * into the same turns (contract §6). Resolves to a DISPATCH_* outcome and
 * never rejects.
 */
function dispatch(sessionId, text, mentions, appendUserMessage) {
  const ctx = pluginCtx
  // Minted once, here. Every attempt below reuses it verbatim.
  const body = {
    append_user_message: appendUserMessage,
    dispatch_id: newDispatchId(),
    mentions,
    session_id: sessionId,
    text
  }

  return (async () => {
    if (!ctx || typeof ctx.rest !== 'function') {
      // No bridge at all: no request left this machine, so nothing can be
      // half-dispatched. Definite, and safe to pass through to Hermes.
      reportDispatchFailure(new Error('plugin REST bridge unavailable'), mentions, DISPATCH_REJECTED)

      return DISPATCH_REJECTED
    }

    let attempt = await attemptDispatch(ctx, body)

    if (attempt.outcome === DISPATCH_UNKNOWN) {
      attempt = await attemptDispatch(ctx, body)
    }

    // Exactly one notice per submit, never one per attempt.
    if (attempt.outcome !== DISPATCH_ACCEPTED) {
      reportDispatchFailure(attempt.error, mentions, attempt.outcome)
    } else if (attempt.partial) {
      reportPartialDispatch(attempt.partial)
    }

    return attempt.outcome
  })().catch(() => DISPATCH_UNKNOWN)
}

/**
 * One dispatch per draft attempt, keyed by the draft OBJECT.
 *
 * The composer builds a fresh draft per submit, so object identity is attempt
 * identity: concurrent invocations of the same attempt join one POST, while a
 * deliberate re-send — a new draft object, even with byte-identical text — is a
 * new attempt with a fresh `dispatch_id`. Keying by text instead would silently
 * swallow a genuine re-send.
 *
 * The entry is kept after settling (a draft object dispatches at most once,
 * ever) and the WeakMap lets it vanish with the draft. This is an optimization
 * on top of the server's idempotency map, never the correctness mechanism.
 *
 * Only reachable with an object draft: routeDraft returns early unless
 * `draft.text` is a non-empty string.
 */
function dispatchOnce(draft, sessionId, text, mentions, appendUserMessage) {
  const pending = attemptOutcomes.get(draft)

  if (pending) {
    return pending
  }

  const settled = dispatch(sessionId, text, mentions, appendUserMessage)

  attemptOutcomes.set(draft, settled)

  return settled
}

/**
 * The routing decision, contract §9:
 *
 *   no known external mention  → pass through (Hermes handles it)
 *   external + @hermes         → dispatch (append_user_message:false), then
 *                                pass through (the Hermes turn runs and
 *                                persists the human row itself)
 *   external only              → dispatch (append_user_message:true), consume
 *                                the draft ({handled:true})
 *   committed partial failure  → same as accepted: the send happened, some
 *                                participants did not start (one notice)
 *   pre-acceptance refusal     → pass through: nothing was dispatched, so the
 *                                text still lands with Hermes
 *   ambiguous failure          → null: the composer restores the draft rather
 *                                than risk a double send
 */
async function routeDraft(draft) {
  const text = draft && typeof draft.text === 'string' ? draft.text : ''

  if (!text || text.indexOf('@') === -1) {
    return draft
  }

  if (rosterAttempted) {
    // Warm: answer from cache and refresh for next time. A submit must never
    // wait on REST once we've talked to the backend at least once.
    void ensureRoster()
  } else {
    // Never resolved a roster: this submit is the right moment to pay for it,
    // bounded by the REST timeout, and exactly once per plugin load.
    await ensureRoster()
  }

  const { hermes, mentions } = parseMentions(text)

  if (!mentions.length) {
    return draft
  }

  const sessionId = focusedSessionId()

  if (!sessionId) {
    // A draft with no live session has nowhere to dispatch to; the normal
    // submit path is what creates the session.
    return draft
  }

  const appendUserMessage = !hermes
  const outcome = await dispatchOnce(draft, sessionId, text, mentions, appendUserMessage)

  if (outcome === DISPATCH_UNKNOWN) {
    // The dispatch may already have landed. Passing through would risk waking
    // Hermes AND duplicating the participant send, so cancel instead: the
    // composer restores the draft and the human decides whether to re-send.
    return null
  }

  return appendUserMessage && outcome === DISPATCH_ACCEPTED ? { handled: true } : draft
}

// ── plugin ───────────────────────────────────────────────────────────────────

export default {
  /** Opt-in on both halves — the unified-package loader already caps this
   *  root at false; stating it keeps the promise if the folder is ever
   *  dropped into `desktop-plugins/` directly. */
  defaultEnabled: false,
  description:
    'Route @claude / @codex mentions in the composer to external agent participants, and offer their handles in the @ menu.',
  id: PLUGIN_ID,
  name: 'Relay Participants',

  register(ctx) {
    // Hot reload re-runs register with a fresh ctx; start from a clean slate.
    pluginCtx = ctx
    roster = []
    rosterHandles = new Set()
    rosterAttempted = false
    rosterCheckedAt = 0
    rosterOk = false
    rosterInFlight = null
    attemptOutcomes = new WeakMap()
    disposers = []

    // Warm the cache so the first `@` keystroke already has handles. The
    // backend may be disabled (`plugins.enabled` in config.yaml) — that is a
    // supported state, not an error, so this can only ever no-op.
    try {
      void ensureRoster()
    } catch {
      // ensureRoster never throws, but register must survive even if it did.
    }

    // Participant activity changes `status`; mark the roster stale so the next
    // completion or submit re-reads it. This listener CANNOT dispatch — that
    // is the recursion suppression on this side of the seam.
    try {
      if (host && typeof host.onEvent === 'function') {
        for (const type of ROSTER_INVALIDATING_EVENTS) {
          const off = host.onEvent(type, invalidateRoster)

          if (typeof off === 'function') {
            disposers.push(off)
          }
        }
      }
    } catch {
      // No event tap → the TTL alone keeps the roster honest.
    }

    ctx.register({
      area: COMPOSER_AREAS.atCompletions,
      data: {
        /** Called per keystroke — cache reads only, no awaits, no throws. */
        provide: query => {
          try {
            // Fire-and-forget: refreshes a stale cache for the NEXT keystroke.
            void ensureRoster()

            const q = String(query == null ? '' : query)
              .trim()
              .replace(/^@+/, '')
              .toLowerCase()
            const items = []

            for (const participant of roster) {
              if (q && !participant.handle.startsWith(q)) {
                continue
              }

              items.push({
                display: `@${participant.handle}`,
                insert: `@${participant.handle}`,
                meta: participantMeta(participant)
              })

              if (items.length >= MAX_COMPLETIONS) {
                break
              }
            }

            return items
          } catch {
            return []
          }
        }
      },
      id: 'participant-completions'
    })

    ctx.register({
      area: COMPOSER_AREAS.middleware,
      data: {
        handler: async draft => {
          try {
            return await routeDraft(draft)
          } catch {
            // Last line of defence for the never-lose-a-message invariant.
            return draft
          }
        }
      },
      id: 'participant-router'
    })

    if (typeof ctx.onDispose === 'function') {
      ctx.onDispose(() => {
        for (const off of disposers) {
          try {
            off()
          } catch {
            // Already gone.
          }
        }

        disposers = []
        attemptOutcomes = new WeakMap()
        pluginCtx = null
      })
    }
  }
}
