import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { findAll, loadRelay, textContent } from './_harness.mjs'

const SOURCE = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')

function sourceImports(source) {
  return [...source.matchAll(/from '([^']+)'/g)].map(match => match[1])
}

function callForm(app) {
  const form = findAll(app.tree, node => node.type === 'form')[0]

  assert.ok(form, 'Relay composer form is rendered')
  return form
}

test('is an uncompiled workspace plugin with only the permitted imports', () => {
  assert.deepStrictEqual(sourceImports(SOURCE), ['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
  assert.match(SOURCE, /ROUTES_AREA/)
  assert.match(SOURCE, /area: 'panes'/)
  assert.doesNotMatch(SOURCE, /SIDEBAR_NAV_AREA|sidebar\.nav/)
  // The harness inspector is read-only: it must never reach for a gateway
  // socket, session lifecycle, composer middleware, or the plugin backend's
  // credential surface. Native provider names may only appear as display
  // metadata, never in fetch/socket calls (the no-fetch guard below).
  assert.doesNotMatch(SOURCE, /COMPOSER_AREAS|composer\.middleware|atCompletions|participant/i)
  assert.doesNotMatch(SOURCE, /host\.(?:request|openSession|newChat)|session[_./](?:create|kill|attach|input)|ctx\.socket|onEvent/i)
  assert.doesNotMatch(SOURCE, /\bfetch\(|XMLHttpRequest|https?:\/\/|<iframe|iframe/i)
  assert.doesNotMatch(SOURCE, /storage\.(?:set|get)\([^)]*(?:token|credential|secret|auth)/i)
  assert.match(SOURCE, /relay\.selection\.channelId/)
  assert.doesNotMatch(SOURCE, /ctx\.storage\.(?:set|get)\([^)]*(?!relay\.selection\.channelId)/)
})

test('registers one full /relay route and one top-level Relay pane tab', async () => {
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
      area: 'panes',
      data: {
        collapsible: true,
        dock: { enforce: true, pane: 'sessions', pos: 'center' },
        hideOnly: true,
        placement: 'left',
        width: '260px'
      },
      id: 'pane',
      render: relay.registrations[1].render,
      title: 'Relay'
    }
  ])
})

test('opens the dedicated workspace only while the Relay tab is selected', async () => {
  const relay = await loadRelay()

  assert.deepStrictEqual(relay.visibilityRequests, ['hermes-plugin-relay:pane'])
  assert.strictEqual(relay.workspaceOpenings.length, 0, 'the hidden tab does not steal the main workspace')

  relay.setPaneVisible(true)
  assert.strictEqual(relay.workspaceOpenings.length, 1)
  assert.strictEqual(relay.workspaceOpenings[0].id, 'hermes-plugin-relay:home')
  assert.strictEqual(relay.workspaceOpenings[0].options.title, 'Relay')
  assert.strictEqual(relay.workspaceOpenings[0].options.minWidth, '32rem')
  assert.strictEqual(typeof relay.workspaceOpenings[0].options.render, 'function')
  assert.deepStrictEqual(relay.navigations, [], 'modern Desktop uses the main-workspace door, not route navigation')

  relay.setPaneVisible(false)
  assert.deepStrictEqual(relay.workspaceCloses, ['hermes-plugin-relay:home'])

  relay.setPaneVisible(true)
  relay.disposePlugin()
  assert.deepStrictEqual(relay.workspaceCloses, ['hermes-plugin-relay:home', 'hermes-plugin-relay:home'])
})

test('falls back to the hidden /relay route without a main-workspace door', async () => {
  const relay = await loadRelay({ workspaceSupported: false })

  relay.setPaneVisible(true)
  assert.deepStrictEqual(relay.navigations, ['/relay'])
  assert.strictEqual(relay.workspaceOpenings.length, 0)
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

test('harness surface groups by provider and expands into native sessions', async () => {
  const relay = await loadRelay()
  const app = relay.mount()

  await app.settle()

  const harnessTab = findAll(app.tree, node => node.type === 'button' && node.props?.['data-surface'] === 'harnesses')[0]
  assert.ok(harnessTab, 'the Harnesses surface tab is rendered')
  assert.strictEqual(relay.calls.filter(call => call.path === '/harnesses').length, 0, 'harness rows stay unloaded until the tab is selected')

  harnessTab.props.onClick()
  await app.settle()

  const listCalls = relay.calls.filter(call => call.path === '/harnesses')
  assert.strictEqual(listCalls.length, 1, 'switching surfaces loads the harness report once')

  const group = findAll(app.tree, node => node.type === 'button' && node.props?.['data-harness'] === 'claude')[0]
  assert.ok(group, 'the Claude Code group header renders')
  assert.strictEqual(group.props['aria-expanded'], false, 'groups start collapsed')
  assert.ok(
    findAll(app.tree, node => node.type === 'button' && node.props?.['data-harness'] === 'hermes')[0],
    'the unsupported Hermes group still renders'
  )
  assert.strictEqual(relay.calls.filter(call => call.path.startsWith('/harnesses/')).filter(call => call.path !== '/harnesses').length, 0, 'no session lists load while collapsed')

  group.props.onClick()
  await app.settle()

  const expandedGroup = findAll(app.tree, node => node.type === 'button' && node.props?.['data-harness'] === 'claude')[0]
  assert.strictEqual(expandedGroup.props['aria-expanded'], true, 'the expanded group reports its state')
  const scopedList = relay.calls.filter(call => call.path === '/harnesses/claude/sessions')
  assert.strictEqual(scopedList.length, 1, 'expansion loads exactly one provider-scoped session list')

  const sessionRow = findAll(app.tree, node => node.type === 'button' && node.props?.['data-session-id'] === 'claude-1')[0]
  assert.ok(sessionRow, 'the harness session row renders under its group')
  assert.match(textContent(sessionRow), /Recent claude work/)
})

test('selecting a harness session shows a redacted snapshot without leaking paths', async () => {
  const relay = await loadRelay()
  const app = relay.mount()

  await app.settle()
  findAll(app.tree, node => node.type === 'button' && node.props?.['data-surface'] === 'harnesses')[0].props.onClick()
  await app.settle()
  findAll(app.tree, node => node.type === 'button' && node.props?.['data-harness'] === 'codex')[0].props.onClick()
  await app.settle()
  findAll(app.tree, node => node.type === 'button' && node.props?.['data-session-id'] === 'codex-1')[0].props.onClick()
  await app.settle()

  const detailCall = relay.calls.find(call => call.path === '/harnesses/codex/sessions/codex-1')
  assert.ok(detailCall, 'selection requests exactly the chosen session snapshot')
  const detailText = textContent(app.tree)
  assert.match(detailText, /snapshot for codex-1/, 'the bounded preview is rendered')
  assert.match(detailText, /redacted preview/, 'the redaction marker is surfaced honestly')

  // The channels composer must not be present on the harness surface.
  assert.strictEqual(findAll(app.tree, node => node.type === 'form').length, 0, 'the read-only surface never renders a composer')
})
