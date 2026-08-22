import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { findAll, loadRelay } from './_harness.mjs'

const SOURCE = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')

function sourceImports(source) {
  return [...source.matchAll(/from '([^']+)'/g)].map(match => match[1])
}

function callForm(app) {
  const form = findAll(app.tree, node => node.type === 'form')[0]

  assert.ok(form, 'Relay composer form is rendered')
  return form
}

test('is an uncompiled route-only plugin with only the permitted imports', () => {
  assert.deepStrictEqual(sourceImports(SOURCE), ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
  assert.match(SOURCE, /ROUTES_AREA/)
  assert.match(SOURCE, /SIDEBAR_NAV_AREA/)
  assert.doesNotMatch(SOURCE, /COMPOSER_AREAS|composer\.middleware|atCompletions|participant/i)
  assert.doesNotMatch(SOURCE, /host\.(?:request|openSession|newChat)|session[_./]|provider|claude|codex/i)
  assert.doesNotMatch(SOURCE, /\bfetch\(|XMLHttpRequest|https?:\/\/|<iframe|iframe/i)
  assert.doesNotMatch(SOURCE, /storage\.(?:set|get)\([^)]*(?:token|credential|secret|auth)/i)
  assert.match(SOURCE, /relay\.selection\.channelId/)
  assert.doesNotMatch(SOURCE, /ctx\.storage\.(?:set|get)\([^)]*(?!relay\.selection\.channelId)/)
})

test('registers exactly one full /relay route and one sidebar entry', async () => {
  const relay = await loadRelay()

  assert.strictEqual(relay.plugin.id, 'hermes-plugin-relay')
  assert.strictEqual(relay.plugin.name, 'Relay')
  assert.strictEqual(relay.plugin.defaultEnabled, false)
  assert.strictEqual(relay.registrations.length, 2)
  assert.deepStrictEqual(relay.registrations, [
    {
      area: 'routes',
      data: { path: '/relay' },
      id: 'page',
      render: relay.registrations[0].render
    },
    {
      area: 'sidebar.nav',
      data: { codicon: 'comment-discussion', label: 'Relay', path: '/relay' },
      id: 'nav',
      order: 55
    }
  ])
})

test('lists once, selects one channel, loads its 50-message window, and posts once', async () => {
  const relay = await loadRelay({
    channels: [
      { id: 'general', name: 'General' },
      { id: 'ops', name: 'Operations' }
    ],
    histories: {
      general: { messages: [] },
      ops: { messages: [{ body: { format: 'markdown', text: 'hello' }, id: 'ops-1', sender: { kind: 'human' } }] }
    }
  })
  const app = relay.mount()

  await app.settle()
  assert.strictEqual(relay.channels().length, 1, 'the channel list is requested exactly once on page load')
  assert.strictEqual(relay.histories().filter(call => call.path.includes('/channels/general/')).length, 1)

  const ops = findAll(app.tree, node => node.type === 'button' && node.props?.['data-channel-id'] === 'ops')[0]
  assert.ok(ops, 'Operations is selectable')
  ops.props.onClick()
  await app.settle()

  assert.strictEqual(relay.histories().filter(call => call.path.includes('/channels/ops/')).length, 1, 'selection loads one history window')
  const textarea = findAll(app.tree, node => node.type === 'textarea')[0]

  assert.ok(textarea, 'the composer is rendered')
  textarea.props.onChange({ target: { value: 'ship it' } })
  callForm(app).props.onSubmit({ preventDefault() {} })
  await app.settle()

  assert.strictEqual(relay.posts().length, 1, 'one submit emits exactly one POST')
  assert.deepStrictEqual(relay.posts()[0], {
    options: {
      body: {
        clientMessageId: relay.posts()[0].options.body.clientMessageId,
        format: 'markdown',
        text: 'ship it'
      },
      method: 'POST'
    },
    path: '/channels/ops/messages'
  })
  assert.ok(relay.posts()[0].options.body.clientMessageId, 'the client provides its own idempotency key')
  assert.strictEqual(relay.storage.set.at(-1)?.value, 'ops', 'only the non-secret selected channel is persisted')
  assert.strictEqual(relay.histories().filter(call => call.path.includes('/channels/ops/')).length, 2, 'a confirmed post immediately refreshes the latest window')
})
