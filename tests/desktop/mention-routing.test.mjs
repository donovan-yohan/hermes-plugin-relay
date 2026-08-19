/**
 * Desktop half — `composer.middleware` routing (participant seam contract §9).
 *
 * See _harness.mjs for how the plugin is loaded (uncompiled ESM, stubbed SDK,
 * a fresh module instance per test).
 *
 * The gates asserted here are the ones that keep the seam honest:
 *   - a draft addressed only to participants is consumed ({handled:true});
 *   - a draft that also addresses @hermes dispatches AND passes through;
 *   - a draft with no known handle never reaches the backend;
 *   - one submit dispatches at most once, even re-entered concurrently;
 *   - participant activity on the gateway event stream cannot dispatch;
 *   - only a pre-acceptance refusal passes through; a committed result is
 *     consumed and an ambiguous one cancels — never lost, never sent twice.
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { flush, loadPlugin as load } from './_harness.mjs'

const ROSTER = [
  {
    adapter_id: 'claude-code-stream-json',
    capabilities: { streaming: true, text: true },
    display_name: 'Claude Code',
    handle: 'claude',
    id: 'claude:default',
    status: 'ready'
  },
  {
    adapter_id: 'codex-app-server',
    capabilities: { streaming: true, text: true },
    display_name: 'Codex',
    handle: 'codex',
    id: 'codex:default',
    status: 'ready'
  }
]

/** Every test here routes against the same two-participant roster. */
const loadPlugin = options => load({ participants: ROSTER, ...options })

// ── targeting ────────────────────────────────────────────────────────────────

test('a participant-only draft dispatches with append_user_message and is consumed', async () => {
  const { dispatches, handler } = await loadPlugin()
  const draft = { text: '@claude review this' }
  const result = await handler(draft)

  assert.strictEqual(dispatches().length, 1)

  const { dispatch_id: dispatchId, ...body } = dispatches()[0].options.body

  assert.deepStrictEqual(body, {
    append_user_message: true,
    mentions: ['claude'],
    session_id: 'sess-1',
    text: '@claude review this'
  })
  assert.ok(typeof dispatchId === 'string' && dispatchId.length >= 8, `dispatch_id present: ${dispatchId}`)
  assert.strictEqual(dispatches()[0].options.method, 'POST')
  assert.deepStrictEqual(result, { handled: true }, 'draft consumed — no Hermes turn')
})

test('two participants in one draft ride a single dispatch', async () => {
  const { dispatches, handler } = await loadPlugin()
  const result = await handler({ text: '@claude and @codex go' })

  assert.strictEqual(dispatches().length, 1, 'one submit, one dispatch')
  assert.deepStrictEqual(dispatches()[0].options.body.mentions, ['claude', 'codex'])
  assert.strictEqual(dispatches()[0].options.body.append_user_message, true)
  assert.deepStrictEqual(result, { handled: true })
})

test('mentioning @hermes alongside a participant dispatches AND passes the draft through', async () => {
  const { dispatches, handler } = await loadPlugin()
  const draft = { text: '@claude plus @hermes' }
  const result = await handler(draft)

  assert.strictEqual(dispatches().length, 1)

  const { dispatch_id: dispatchId, ...body } = dispatches()[0].options.body

  assert.deepStrictEqual(body, {
    append_user_message: false,
    mentions: ['claude'],
    session_id: 'sess-1',
    text: '@claude plus @hermes'
  })
  assert.ok(typeof dispatchId === 'string' && dispatchId.length >= 8)
  assert.strictEqual(result, draft, 'Hermes turn proceeds normally and persists the human row')
})

test('a draft with no known mention never reaches the backend', async () => {
  const { calls, dispatches, handler } = await loadPlugin()

  for (const text of [
    'hello no mentions',
    '@nobody are you there',
    'mail me at user@codex.com',
    'ask @hermes about it',
    '```\n@claude ignored inside a fence\n```',
    'inline `@claude` is quoted, not addressed'
  ]) {
    const draft = { text }

    assert.strictEqual(await handler(draft), draft, `passthrough: ${text}`)
  }

  assert.strictEqual(dispatches().length, 0, 'no dispatch without a known external handle')
  assert.deepStrictEqual(
    calls.map(call => call.path),
    ['/participants'],
    'the roster read is the only REST traffic'
  )
})

test('mentions are matched case-insensitively and anywhere in the text', async () => {
  const { dispatches, handler } = await loadPlugin()

  await handler({ text: 'please have (@Claude) take a look, cc @CODEX' })

  assert.deepStrictEqual(dispatches()[0].options.body.mentions, ['claude', 'codex'])
})

test('a draft with no live session passes through instead of dispatching', async () => {
  const { dispatches, handler } = await loadPlugin({ sessionId: null })
  const draft = { text: '@claude review this' }

  assert.strictEqual(await handler(draft), draft)
  assert.strictEqual(dispatches().length, 0)
})

test('the session id is read at dispatch time, never cached from register', async () => {
  let current = 'sess-a'
  const { dispatches, handler } = await loadPlugin({ sessionId: () => current })

  await handler({ text: '@claude first' })
  current = 'sess-b'
  await handler({ text: '@claude second' })

  assert.deepStrictEqual(
    dispatches().map(call => call.options.body.session_id),
    ['sess-a', 'sess-b']
  )
})

// ── idempotence ──────────────────────────────────────────────────────────────

test('concurrent handler calls for the same draft dispatch exactly once', async () => {
  let release
  const gate = new Promise(resolve => {
    release = resolve
  })
  const { dispatches, handler } = await loadPlugin({
    dispatch: async () => {
      await gate

      return { ok: true, turns: [] }
    }
  })
  const draft = { text: '@claude review this' }
  const both = Promise.all([handler(draft), handler(draft)])

  await flush()
  assert.strictEqual(dispatches().length, 1, 'second entry joined the in-flight dispatch')

  release()

  const [first, second] = await both

  assert.deepStrictEqual(first, { handled: true })
  assert.deepStrictEqual(second, { handled: true })
  assert.strictEqual(dispatches().length, 1)
  assert.ok(dispatches()[0].options.body.dispatch_id, 'the single POST carries one dispatch_id')
})

test('the pass-through (@hermes) path also dispatches only once when re-entered', async () => {
  const { dispatches, handler } = await loadPlugin()
  const draft = { text: '@claude plus @hermes' }
  const [first, second] = await Promise.all([handler(draft), handler(draft)])

  assert.strictEqual(dispatches().length, 1)
  assert.strictEqual(first, draft)
  assert.strictEqual(second, draft)
})

// The guard keys on the draft OBJECT, not its text: the composer builds a
// fresh draft per submit, so identical text from a deliberate re-send is a new
// attempt and must reach the backend.
test('a deliberate re-send of identical text dispatches again, with a fresh dispatch_id', async () => {
  const { dispatches, handler } = await loadPlugin()

  await handler({ text: '@claude review this' })
  await handler({ text: '@claude review this' })

  assert.strictEqual(dispatches().length, 2, 'a deliberate re-send is not swallowed')
  assert.notStrictEqual(
    dispatches()[0].options.body.dispatch_id,
    dispatches()[1].options.body.dispatch_id,
    'a new attempt is a new dispatch_id — the server must not dedupe it away'
  )
})

test('an identical re-send while the first is still in flight is still a separate attempt', async () => {
  let release
  const gate = new Promise(resolve => {
    release = resolve
  })
  const { dispatches, handler } = await loadPlugin({
    dispatch: async () => {
      await gate

      return { ok: true, turns: [] }
    }
  })

  // Two distinct draft objects: two submits that happen to overlap.
  const both = Promise.all([handler({ text: '@claude review this' }), handler({ text: '@claude review this' })])

  await flush()
  assert.strictEqual(dispatches().length, 2, 'text equality must not collapse two real submits')

  release()
  await both

  assert.strictEqual(new Set(dispatches().map(call => call.options.body.dispatch_id)).size, 2)
})

test('the same draft object never dispatches twice, even after the first settled', async () => {
  const { dispatches, handler } = await loadPlugin()
  const draft = { text: '@claude review this' }

  const first = await handler(draft)

  assert.deepStrictEqual(first, { handled: true })
  assert.strictEqual(dispatches().length, 1)

  const second = await handler(draft)

  assert.deepStrictEqual(second, { handled: true }, 'memoized outcome, not a second send')
  assert.strictEqual(dispatches().length, 1, 'one draft attempt, one dispatch — ever')
})

// ── recursion suppression ────────────────────────────────────────────────────

test('participant activity on the gateway event stream can never dispatch', async () => {
  const { dispatches, emit, handler, listeners } = await loadPlugin()

  await handler({ text: '@claude review this' })
  assert.strictEqual(dispatches().length, 1)

  assert.ok(listeners.has('participant.message.complete'), 'plugin taps participant events')

  emit('participant.message.start', {
    params: { payload: { participant_turn_id: 'pturn-1', row_id: 7 }, session_id: 'sess-1' },
    type: 'participant.message.start'
  })
  emit('participant.message.complete', {
    params: {
      payload: {
        participant_turn_id: 'pturn-1',
        row_id: 7,
        status: 'completed',
        text: '@claude @codex @hermes — reply text that names everyone'
      },
      session_id: 'sess-1'
    },
    type: 'participant.message.complete'
  })
  await flush()

  assert.strictEqual(dispatches().length, 1, 'participant output never re-enters the router')
})

// ── failure fallback ─────────────────────────────────────────────────────────

test('an ambiguous failure retries once with the SAME dispatch_id, then cancels the send', async () => {
  const { dispatches, handler, notifications } = await loadPlugin({
    dispatch: async () => {
      throw new Error('socket hang up')
    }
  })
  const draft = { text: '@claude review this' }
  const result = await handler(draft)

  assert.strictEqual(dispatches().length, 2, 'exactly one retry')
  assert.strictEqual(
    dispatches()[0].options.body.dispatch_id,
    dispatches()[1].options.body.dispatch_id,
    'a retry NEVER mints a fresh dispatch_id — the server dedupes on it'
  )
  assert.strictEqual(result, null, 'composer restores the draft; Hermes is not woken')
  assert.strictEqual(notifications.length, 1, 'exactly one error surfaced')
  assert.match(notifications[0].fallback, /@claude/)
  assert.match(notifications[0].fallback, /restored/)
})

test('a 5xx is ambiguous too — retried, then cancelled', async () => {
  const { dispatches, handler } = await loadPlugin({
    dispatch: async () => {
      const error = new Error('503: upstream unavailable')

      error.statusCode = 503
      throw error
    }
  })

  assert.strictEqual(await handler({ text: '@claude review this' }), null)
  assert.strictEqual(dispatches().length, 2)
})

test('an ambiguous first attempt that succeeds on retry is accepted', async () => {
  let attempts = 0
  const { dispatches, handler } = await loadPlugin({
    dispatch: async () => {
      attempts += 1

      if (attempts === 1) {
        throw new Error('ETIMEDOUT')
      }

      return { ok: true, turns: [] }
    }
  })

  assert.deepStrictEqual(await handler({ text: '@claude review this' }), { handled: true })
  assert.strictEqual(dispatches().length, 2)
  assert.strictEqual(dispatches()[0].options.body.dispatch_id, dispatches()[1].options.body.dispatch_id)
})

test('an explicit 4xx passes the draft through — nothing was dispatched', async () => {
  const { dispatches, handler, notifications } = await loadPlugin({
    dispatch: async () => {
      const error = new Error('400: {"ok":false,"error":"missing dispatch_id"}')

      error.statusCode = 400
      throw error
    }
  })
  const draft = { text: '@claude review this' }

  assert.strictEqual(await handler(draft), draft, 'the message still reaches Hermes')
  assert.strictEqual(dispatches().length, 1, 'a stated rejection is never retried')
  assert.ok(notifications.length <= 1, 'at most one notice')
})

test('a 4xx is classified from the message when the status property is stripped by IPC', async () => {
  const { dispatches, handler } = await loadPlugin({
    dispatch: async () => {
      // What ipcRenderer.invoke surfaces: custom props gone, message intact.
      throw new Error(`Error invoking remote method 'hermes:api': Error: 404: {"detail":"no such session"}`)
    }
  })
  const draft = { text: '@claude review this' }

  assert.strictEqual(await handler(draft), draft)
  assert.strictEqual(dispatches().length, 1, 'recognized as definite, so not retried')
})

test('a 200 {ok:false} with NO side-effect markers is pre-acceptance: pass through, no retry', async () => {
  const { dispatches, handler, notifications } = await loadPlugin({
    dispatch: async () => ({ error: 'unknown session', ok: false })
  })
  const draft = { text: '@claude review this' }

  assert.strictEqual(await handler(draft), draft)
  assert.strictEqual(dispatches().length, 1)
  assert.strictEqual(notifications.length, 1)
  assert.match(notifications[0].fallback, /Hermes instead/)
})

// Contract v1.5: a 200 ok:false is a COMMITTED result whenever it carries
// side-effect markers. Passing that draft through would append the user row a
// second time and wake Hermes, who was never addressed.
test('a committed partial failure consumes the draft and reports which participants failed', async () => {
  const { dispatches, handler, notices, notifications } = await loadPlugin({
    dispatch: async () => ({
      error: 'codex failed to start',
      failed: [{ error: 'binary not found', participant_id: 'codex:default' }],
      ok: false,
      turns: [{ participant_id: 'claude:default', participant_turn_id: 'pturn-1' }],
      user_row_appended: true
    })
  })
  const draft = { text: '@claude and @codex go' }
  const result = await handler(draft)

  assert.deepStrictEqual(result, { handled: true }, 'consumed — the user row already exists')
  assert.notStrictEqual(result, draft, 'must NOT pass through to Hermes')
  assert.strictEqual(dispatches().length, 1, 'a committed result is never retried')
  assert.strictEqual(notifications.length, 0, 'not an error — the send happened')
  assert.strictEqual(notices.length, 1, 'exactly one notice')
  assert.match(notices[0].message, /codex/, 'names the participant that failed')
})

test('either side-effect marker alone marks the result committed', async () => {
  const rowOnly = await loadPlugin({
    dispatch: async () => ({ error: 'all participants failed', ok: false, turns: [], user_row_appended: true })
  })

  assert.deepStrictEqual(await rowOnly.handler({ text: '@claude go' }), { handled: true })

  const turnsOnly = await loadPlugin({
    dispatch: async () => ({
      error: 'user row failed',
      ok: false,
      turns: [{ participant_id: 'claude:default', participant_turn_id: 'pturn-1' }]
    })
  })

  assert.deepStrictEqual(await turnsOnly.handler({ text: '@claude go' }), { handled: true })
})

test('a committed partial on the @hermes path passes through without a duplicate dispatch', async () => {
  const { dispatches, handler, notices } = await loadPlugin({
    dispatch: async () => ({
      failed: [{ error: 'binary not found', participant_id: 'codex:default' }],
      ok: false,
      turns: [{ participant_id: 'claude:default', participant_turn_id: 'pturn-1' }],
      user_row_appended: false
    })
  })
  const draft = { text: '@claude and @codex plus @hermes' }

  assert.strictEqual(await handler(draft), draft, 'Hermes was addressed, so its turn still runs')
  assert.strictEqual(dispatches().length, 1)
  assert.strictEqual(notices.length, 1)
  assert.match(notices[0].message, /codex:default/)
})

test('on the @hermes path a 4xx passes through but an ambiguous failure cancels', async () => {
  const rejected = await loadPlugin({
    dispatch: async () => {
      const error = new Error('422: unknown participant')

      error.statusCode = 422
      throw error
    }
  })
  const rejectedDraft = { text: '@claude plus @hermes' }

  assert.strictEqual(await rejected.handler(rejectedDraft), rejectedDraft, 'Hermes turn still runs')

  const ambiguous = await loadPlugin({
    dispatch: async () => {
      throw new Error('socket hang up')
    }
  })

  assert.strictEqual(
    await ambiguous.handler({ text: '@claude plus @hermes' }),
    null,
    'waking Hermes here could pair with a participant send that actually landed'
  )
  assert.strictEqual(ambiguous.dispatches().length, 2)
})

test('separate submits carry different dispatch_ids', async () => {
  const { dispatches, handler } = await loadPlugin()

  await handler({ text: '@claude first' })
  await handler({ text: '@claude second' })
  await handler({ text: '@claude first' })

  const ids = dispatches().map(call => call.options.body.dispatch_id)

  assert.strictEqual(ids.length, 3)
  assert.strictEqual(new Set(ids).size, 3, 'a new submit is a new attempt, even with identical text')
})

test('a hostile draft shape cannot break the composer', async () => {
  const { dispatches, handler } = await loadPlugin()

  for (const draft of [{}, { text: '' }, { text: 42 }, { attachments: [] }, { text: null }]) {
    assert.strictEqual(await handler(draft), draft)
  }

  assert.strictEqual(dispatches().length, 0)
})

test('a throwing host.state read degrades to pass-through', async () => {
  const { dispatches, handler } = await loadPlugin({
    sessionId: () => {
      throw new Error('no bridge')
    }
  })
  const draft = { text: '@claude review this' }

  assert.strictEqual(await handler(draft), draft)
  assert.strictEqual(dispatches().length, 0)
})
