import assert from 'node:assert/strict'
import test from 'node:test'

import { findAll, findButton, loadRelay, textContent } from './_harness.mjs'

function responseError(status, message) {
  const error = new Error(`${status}: ${message}`)

  error.statusCode = status
  return error
}

function textarea(app) {
  const control = findAll(app.tree, node => node.type === 'textarea')[0]

  assert.ok(control, 'Relay composer is available')
  return control
}

test('renders safe human, agent, and system attribution without parsing message text', async () => {
  const relay = await loadRelay({
    histories: {
      general: {
        messages: [
          { body: { format: 'markdown', text: '@agent is ordinary message content' }, id: 'human-1', sender: { displayName: 'Ari', kind: 'human' } },
          { body: { format: 'markdown', text: 'Automated reply' }, id: 'agent-1', sender: { displayName: 'Codex', kind: 'agent' } },
          { body: { format: 'markdown', text: 'Channel opened' }, id: 'system-1', sender: { kind: 'system' } }
        ]
      }
    }
  })
  const app = relay.mount()

  await app.settle()
  assert.deepStrictEqual(
    findAll(app.tree, node => node.type === 'article').map(node => node.props['data-attribution']),
    ['human', 'agent', 'system']
  )
  assert.match(textContent(app.tree), /@agent is ordinary message content/, 'message text is displayed, never parsed as routing syntax')
})

test('renders distinct channel-empty and transcript-empty states', async () => {
  const noChannels = await loadRelay({ channels: [] })
  const emptyChannels = noChannels.mount()

  await emptyChannels.settle()
  const channelState = findAll(emptyChannels.tree, node => node.type === 'empty-state')[0]
  assert.strictEqual(channelState.props.title, 'No Relay channels yet')

  const noMessages = await loadRelay({ histories: { general: { messages: [] } } })
  const emptyTranscript = noMessages.mount()

  await emptyTranscript.settle()
  const transcriptState = findAll(emptyTranscript.tree, node => node.type === 'empty-state').find(node => node.props.title === 'No messages in this channel yet')
  assert.ok(transcriptState, 'an existing channel has its own transcript-empty state')
})

test('401 and 403 move the page into the authorization state and post only to the scoped authorize endpoint', async () => {
  for (const status of [401, 403]) {
    const relay = await loadRelay({
      rest: async path => {
        if (path === '/connection/status') {
          throw responseError(status, 'Relay login expired')
        }
        if (path === '/connection/authorize') {
          return { ok: true }
        }
        throw new Error(`unexpected ${path}`)
      }
    })
    const app = relay.mount()

    await app.settle()
    assert.strictEqual(findAll(app.tree, node => node.props?.['data-connection'] === 'auth_required').length, 1, `${status} requires authorization`)

    findButton(app.tree, 'Authorize Relay').props.onClick()
    await app.settle()

    assert.deepStrictEqual(
      relay.calls.filter(call => call.path === '/connection/authorize'),
      [{ options: { method: 'POST' }, path: '/connection/authorize' }]
    )
  }
})

test('a missing grant opens Relay and explains how authorization completes', async () => {
  const relay = await loadRelay({
    rest: async path => {
      if (path === '/connection/status') return { status: 'auth_required' }
      if (path === '/connection/authorize') throw responseError(401, 'grant required')
      if (path === '/connection/onboarding') return { url: 'http://127.0.0.1:3456/' }
      throw new Error(`unexpected ${path}`)
    }
  })
  const app = relay.mount()

  await app.settle()
  findButton(app.tree, 'Authorize Relay').props.onClick()
  await app.settle()

  assert.deepStrictEqual(relay.externalUrls, ['http://127.0.0.1:3456/'])
  assert.match(textContent(app.tree), /approved scoped grant/)
  assert.match(textContent(app.tree), /restart Hermes/)
})

test('failed Relay launch keeps the grant and restart recovery instructions', async () => {
  const relay = await loadRelay({
    openExternal: async () => {
      throw new Error('shell denied launch')
    },
    rest: async path => {
      if (path === '/connection/status') return { status: 'auth_required' }
      if (path === '/connection/authorize') throw responseError(401, 'grant required')
      if (path === '/connection/onboarding') return { url: 'http://127.0.0.1:3456/' }
      throw new Error(`unexpected ${path}`)
    }
  })
  const app = relay.mount()

  await app.settle()
  findButton(app.tree, 'Authorize Relay').props.onClick()
  await app.settle()

  assert.strictEqual(findAll(app.tree, node => node.props?.['data-connection'] === 'auth_required').length, 1)
  assert.match(textContent(app.tree), /approved scoped grant/)
  assert.match(textContent(app.tree), /restart Hermes/)
  assert.match(textContent(app.tree), /could not be opened/)
})

test('offline refresh preserves the stale transcript read-only and keeps the draft', async () => {
  let statusChecks = 0
  const relay = await loadRelay({
    histories: { general: { messages: [{ body: { format: 'markdown', text: 'Cached message' }, id: 'old', sender: { kind: 'human' } }] } },
    rest: async path => {
      if (path === '/connection/status') {
        statusChecks += 1
        return statusChecks === 1 ? { status: 'ready' } : { status: 'offline' }
      }
      if (path === '/channels') {
        return { channels: [{ id: 'general', name: 'General' }] }
      }
      if (path.includes('/messages?limit=50')) {
        return { messages: [{ body: { format: 'markdown', text: 'Cached message' }, id: 'old', sender: { kind: 'human' } }] }
      }
      throw new Error(`unexpected ${path}`)
    }
  })
  const app = relay.mount()

  await app.settle()
  textarea(app).props.onChange({ target: { value: 'Keep this draft' } })
  findButton(app.tree, 'Refresh').props.onClick()
  await app.settle()

  assert.strictEqual(findAll(app.tree, node => node.props?.['data-connection'] === 'offline').length, 1)
  assert.match(textContent(app.tree), /Cached message/)
  assert.strictEqual(textarea(app).props.value, 'Keep this draft')
  assert.strictEqual(textarea(app).props.readOnly, true)
})

test('an ambiguous post keeps a retry affordance and reuses its exact clientMessageId', async () => {
  let attempts = 0
  const relay = await loadRelay({
    post: async () => {
      attempts += 1
      if (attempts === 1) {
        throw new Error('socket hang up')
      }
      return { ok: true }
    }
  })
  const app = relay.mount()

  await app.settle()
  textarea(app).props.onChange({ target: { value: 'No duplicates please' } })
  findAll(app.tree, node => node.type === 'form')[0].props.onSubmit({ preventDefault() {} })
  await app.settle()

  const retry = findButton(app.tree, 'Retry send')
  assert.strictEqual(relay.posts().length, 1)
  const firstId = relay.posts()[0].options.body.clientMessageId

  retry.props.onClick()
  await app.settle()

  assert.strictEqual(relay.posts().length, 2)
  assert.strictEqual(relay.posts()[1].options.body.clientMessageId, firstId, 'manual retry keeps the original client message id')
  assert.strictEqual(textarea(app).props.value, '', 'a confirmed retry clears the draft')
  assert.ok(relay.histories().length >= 3, 'both the ambiguous post and its manual retry immediately reconcile history')
})

test('a deterministic rejected post preserves the draft without offering a doomed retry', async () => {
  const relay = await loadRelay({
    post: async () => {
      throw responseError(413, '{"error":{"code":"message_too_large","retryable":false}}')
    }
  })
  const app = relay.mount()

  await app.settle()
  textarea(app).props.onChange({ target: { value: 'Too large' } })
  findAll(app.tree, node => node.type === 'form')[0].props.onSubmit({ preventDefault() {} })
  await app.settle()

  assert.strictEqual(
    findAll(app.tree, node => node.type === 'button' && textContent(node) === 'Retry send').length,
    0
  )
  assert.strictEqual(textarea(app).props.value, 'Too large')
  assert.match(findAll(app.tree, node => node.props?.role === 'alert')[0].props.children[0].props.children, /message_too_large/)
})

test('the visible-page 3s poll refreshes the selected latest window and stops on unmount', async () => {
  const relay = await loadRelay()
  const app = relay.mount()

  await app.settle()
  assert.strictEqual(relay.socketHandlers.length, 0, 'the polling-only slice does not dial an unsupported socket')
  assert.deepStrictEqual([...relay.timers.values()].map(timer => timer.delay), [3_000])
  assert.strictEqual(relay.histories().length, 1)

  relay.tickPoll()
  await app.settle()
  assert.strictEqual(relay.histories().length, 2, 'the mounted page polls the latest 50-message window every three seconds')

  app.dispose()
  assert.strictEqual(relay.timers.size, 0, 'unmount stops the visible-page fallback')
})

test('malformed, 404, archived, and generic errors remain recoverable', async () => {
  const malformed = await loadRelay({ histories: { general: { nope: true } } })
  const malformedApp = malformed.mount()

  await malformedApp.settle()
  assert.strictEqual(findAll(malformedApp.tree, node => node.type === 'error-state')[0].props.title, 'Transcript could not be loaded')

  const missing = await loadRelay({
    rest: async path => {
      if (path === '/connection/status') return { status: 'ready' }
      if (path === '/channels') return { channels: [{ id: 'general', name: 'General' }] }
      if (path.includes('/messages?limit=50')) throw responseError(404, 'channel unavailable')
      throw new Error(`unexpected ${path}`)
    }
  })
  const missingApp = missing.mount()

  await missingApp.settle()
  const missingState = findAll(missingApp.tree, node => node.type === 'error-state')[0]
  assert.strictEqual(missingState.props.title, 'Transcript could not be loaded')
  assert.ok(missingState.props.action.props.onClick, '404 exposes a retry action rather than trapping the page')

  const archived = await loadRelay({ histories: { general: { archived: true } } })
  const archivedApp = archived.mount()

  await archivedApp.settle()
  assert.ok(findAll(archivedApp.tree, node => node.type === 'empty-state').some(node => node.props.title === 'Channel archived'))

  const generic = await loadRelay({
    rest: async path => {
      if (path === '/connection/status') return { status: 'ready' }
      if (path === '/channels') throw new Error('upstream down')
      throw new Error(`unexpected ${path}`)
    }
  })
  const genericApp = generic.mount()

  await genericApp.settle()
  assert.strictEqual(findAll(genericApp.tree, node => node.type === 'error-state')[0].props.title, 'Channels could not be loaded')
})
