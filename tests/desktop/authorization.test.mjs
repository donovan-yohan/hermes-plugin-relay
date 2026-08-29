import assert from 'node:assert/strict'
import test from 'node:test'

import { findAll, findButton, loadRelay, textContent } from './_harness.mjs'

const pendingFlow = (code = 'Q7CV-EH8Y') => ({
  code,
  expiresAt: '2099-01-01T00:05:00.000Z',
  flowId: 'flow-public-id',
  status: 'pending',
  verificationUrl: 'http://relay.example.test:3456/cli-gateway/login/flow-public-id/approve'
})

const splitStatus = harnessStatus => ({
  channels: {
    guidance: 'Channels need an operator-client credential. Relay Login connects harnesses only.',
    status: 'auth_required'
  },
  harnesses: {
    loginAvailable: true,
    status: harnessStatus
  }
})

test('channel and harness authorization stay independent', async () => {
  const relay = await loadRelay({
    status: {
      channels: { guidance: '', status: 'ready' },
      harnesses: { loginAvailable: true, status: 'auth_required' }
    }
  })
  const app = relay.mount()

  await app.settle()
  const channels = findAll(app.tree, node => node.props?.['data-lane'] === 'channels')[0]
  const harnesses = findAll(app.tree, node => node.props?.['data-lane'] === 'harnesses')[0]

  assert.strictEqual(channels.props['data-connection'], 'ready')
  assert.strictEqual(harnesses.props['data-connection'], 'auth_required')
  assert.strictEqual(relay.channels().length, 1, 'channel data remains available without harness authorization')
  assert.ok(findButton(app.tree, 'Connect Harnesses'))
  assert.strictEqual(findAll(app.tree, node => node.type === 'textarea')[0].props.readOnly, false)
})

test('harness login renders only public approval data and becomes ready after polling', async () => {
  const secret = 'relay-sac-v1.must-never-reach-renderer'
  let flowStarted = false
  let approved = false
  const relay = await loadRelay({
    rest: async (path, options) => {
      if (path === '/connection/status') {
        return splitStatus(approved ? 'ready' : 'auth_required')
      }
      if (path === '/harnesses/login/start' && options.method === 'POST') {
        flowStarted = true
        return {
          ...pendingFlow(),
          credential: { token: secret },
          token: secret
        }
      }
      if (path === '/harnesses/login' && !options.method) {
        if (!flowStarted) {
          return { status: 'idle' }
        }
        approved = true
        return { credential: { token: secret }, status: 'ready', token: secret }
      }
      throw new Error(`unexpected ${options.method || 'GET'} ${path}`)
    }
  })
  const app = relay.mount()

  await app.settle()
  findButton(app.tree, 'Connect Harnesses').props.onClick()
  await app.settle()

  const code = findAll(app.tree, node => node.props?.['data-login-code'] === 'Q7CV-EH8Y')[0]
  const approvalLink = findAll(app.tree, node => node.type === 'a' && node.props?.['aria-label'] === 'Open approval page')[0]
  const copyButtons = findAll(app.tree, node => node.type === 'copy-button')

  assert.strictEqual(code.props.children, 'Q7CV-EH8Y')
  assert.strictEqual(approvalLink.props.href, pendingFlow().verificationUrl)
  assert.strictEqual(textContent(approvalLink), pendingFlow().verificationUrl)
  assert.strictEqual(approvalLink.props.target, '_blank')
  assert.deepStrictEqual(copyButtons.map(node => node.props.text), ['Q7CV-EH8Y', pendingFlow().verificationUrl])
  assert.doesNotMatch(JSON.stringify(app.tree), /relay-sac-v1/)
  assert.deepStrictEqual([...relay.timers.values()].map(timer => timer.delay), [3_000])
  assert.deepStrictEqual(
    relay.calls.filter(call => call.path === '/harnesses/login/start'),
    [{ options: { method: 'POST' }, path: '/harnesses/login/start' }]
  )

  relay.tickPoll()
  await app.settle()

  const harnesses = findAll(app.tree, node => node.props?.['data-lane'] === 'harnesses')[0]
  assert.strictEqual(harnesses.props['data-connection'], 'ready')
  assert.strictEqual(relay.timers.size, 0, 'terminal approval stops login polling')
  assert.doesNotMatch(JSON.stringify(app.tree), /relay-sac-v1/)
})

test('expired harness login can restart and cancel without touching channel auth', async () => {
  let flowState = 'idle'
  let starts = 0
  const relay = await loadRelay({
    rest: async (path, options) => {
      if (path === '/connection/status') {
        return splitStatus('auth_required')
      }
      if (path === '/harnesses/login/start' && options.method === 'POST') {
        starts += 1
        flowState = 'pending'
        return pendingFlow(starts === 1 ? 'FIRST-001' : 'SECOND-2')
      }
      if (path === '/harnesses/login' && options.method === 'DELETE') {
        flowState = 'idle'
        return { status: 'idle' }
      }
      if (path === '/harnesses/login' && !options.method) {
        if (flowState === 'pending') {
          flowState = 'expired'
          return { message: 'This Relay login expired. Start again.', status: 'expired' }
        }
        return { status: flowState }
      }
      throw new Error(`unexpected ${options.method || 'GET'} ${path}`)
    }
  })
  const app = relay.mount()

  await app.settle()
  findButton(app.tree, 'Connect Harnesses').props.onClick()
  await app.settle()
  relay.tickPoll()
  await app.settle()

  assert.ok(findAll(app.tree, node => node.props?.['data-login-status'] === 'expired')[0])
  findButton(app.tree, 'Start again').props.onClick()
  await app.settle()
  assert.ok(findAll(app.tree, node => node.props?.['data-login-code'] === 'SECOND-2')[0])

  findButton(app.tree, 'Cancel').props.onClick()
  await app.settle()

  assert.ok(findButton(app.tree, 'Connect Harnesses'))
  assert.strictEqual(findAll(app.tree, node => node.props?.['data-login-status'] === 'pending').length, 0)
  assert.deepStrictEqual(
    relay.calls.filter(call => call.path === '/harnesses/login' && call.options.method === 'DELETE'),
    [{ options: { method: 'DELETE' }, path: '/harnesses/login' }]
  )
  assert.strictEqual(findAll(app.tree, node => node.props?.['data-lane'] === 'channels')[0].props['data-connection'], 'auth_required')
})

test('invalid harness approval configuration is explained without a dead connect action', async () => {
  const relay = await loadRelay({
    status: {
      channels: { guidance: '', status: 'ready' },
      harnesses: { loginAvailable: false, message: '', status: 'auth_required' }
    }
  })
  const app = relay.mount()

  await app.settle()

  const harnesses = findAll(app.tree, node => node.props?.['data-lane'] === 'harnesses')[0]

  // `auth_required` + `loginAvailable: false` is the only input that reaches
  // the unavailable copy; an `error` status would pass on the shared branch.
  assert.match(textContent(harnesses), /Relay Login is unavailable because its connection URL is invalid\./)
  assert.strictEqual(
    findAll(harnesses, node => node.type === 'button' && textContent(node) === 'Connect Harnesses').length,
    0
  )
  assert.strictEqual(
    relay.calls.some(call => call.path === '/harnesses/login/start'),
    false
  )
})

test('a hostile approval URL is refused instead of being rendered as a link', async () => {
  for (const verificationUrl of ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'not a url', 'https://user:pw@relay.example.test/approve']) {
    const relay = await loadRelay({
      status: {
        channels: { guidance: '', status: 'ready' },
        harnesses: { loginAvailable: true, message: '', status: 'auth_required' }
      },
      rest: async (path, options) => {
        if (path === '/connection/status') {
          return {
            channels: { guidance: '', status: 'ready' },
            harnesses: { loginAvailable: true, status: 'auth_required' }
          }
        }
        if (path === '/channels') return { channels: [] }
        if (path === '/harnesses') return { harnesses: [] }
        if (path === '/harnesses/login') return { status: 'idle' }
        if (path === '/harnesses/login/start' && options?.method === 'POST') {
          return { code: 'ABCD-1234', expiresAt: '2099-01-01T00:00:00.000Z', status: 'pending', verificationUrl }
        }
        throw new Error(`unexpected ${path}`)
      }
    })
    const app = relay.mount()

    await app.settle()
    findButton(app.tree, 'Connect Harnesses').props.onClick()
    await app.settle()

    assert.strictEqual(
      findAll(app.tree, node => node.type === 'a' && String(node.props?.href || '').startsWith('javascript:')).length,
      0,
      `${verificationUrl} must never reach an anchor href`
    )
    assert.strictEqual(
      findAll(app.tree, node => node.props?.['data-login-status'] === 'pending').length,
      0,
      `${verificationUrl} must not open the approval prompt`
    )
    assert.match(textContent(app.tree), /invalid harness (approval URL|login details)/)
  }
})
