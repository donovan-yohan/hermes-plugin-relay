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
          { author: { displayName: 'Ari', type: 'human' }, id: 'human-1', text: '@agent is ordinary message content' },
          { author_type: 'agent', id: 'agent-1', text: 'Automated reply' },
          { id: 'system-1', role: 'system', text: 'Channel opened' }
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

test('offline refresh preserves the stale transcript read-only and keeps the draft', async () => {
  let statusChecks = 0
  const relay = await loadRelay({
    histories: { general: { messages: [{ authorType: 'human', id: 'old', text: 'Cached message' }] } },
    rest: async path => {
      if (path === '/connection/status') {
        statusChecks += 1
        return statusChecks === 1 ? { status: 'ready' } : { status: 'offline' }
      }
      if (path === '/channels') {
        return { channels: [{ id: 'general', name: 'General' }] }
      }
      if (path.includes('/messages?limit=50')) {
        return { messages: [{ authorType: 'human', id: 'old', text: 'Cached message' }] }
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

test('socket frames and the visible-page 3s poll both refresh the selected latest window', async () => {
  const relay = await loadRelay()
  const app = relay.mount()

  await app.settle()
  assert.strictEqual(relay.socketHandlers.length, 1)
  assert.strictEqual(relay.socketHandlers[0].path, '/events')
  assert.deepStrictEqual([...relay.timers.values()].map(timer => timer.delay), [3_000])
  assert.strictEqual(relay.histories().length, 1)

  relay.dispatchSocket({ type: 'message.created' })
  await app.settle()
  assert.strictEqual(relay.histories().length, 2, 'a scoped socket event forwards to the current transcript refresh')

  relay.tickPoll()
  await app.settle()
  assert.strictEqual(relay.histories().length, 3, 'the mounted page polls the latest 50-message window every three seconds')

  app.dispose()
  assert.strictEqual(relay.socketHandlers.length, 0)
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
