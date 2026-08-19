/**
 * Desktop half — `composer.atCompletions` source (participant seam contract §9).
 *
 * See _harness.mjs for how the plugin is loaded (uncompiled ESM, stubbed SDK,
 * a fresh module instance per test).
 */

import assert from 'node:assert/strict'
import test from 'node:test'

import { loadPlugin as load } from './_harness.mjs'

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
  },
  {
    adapter_id: 'mock',
    capabilities: { streaming: true, text: true },
    display_name: 'Mock Agent',
    handle: 'mock',
    id: 'mock:default',
    status: 'offline'
  }
]

/** Every test here starts from the same three-participant roster. */
const loadPlugin = options => load({ participants: ROSTER, ...options })

test('register contributes an atCompletions source and a middleware handler', async () => {
  const { find, plugin, registrations } = await loadPlugin()

  assert.strictEqual(plugin.id, 'hermes-plugin-relay')
  assert.strictEqual(plugin.name, 'Relay Participants')
  assert.strictEqual(plugin.defaultEnabled, false, 'desktop half ships opt-in')

  const completions = find('composer.atCompletions')
  const middleware = find('composer.middleware')

  assert.ok(completions, 'atCompletions contribution registered')
  assert.strictEqual(completions.id, 'participant-completions')
  assert.strictEqual(typeof completions.data.provide, 'function')

  assert.ok(middleware, 'middleware contribution registered')
  assert.strictEqual(middleware.id, 'participant-router')
  assert.strictEqual(typeof middleware.data.handler, 'function')

  assert.strictEqual(registrations.length, 2, 'no other surfaces are claimed')
})

test('the roster is fetched once at register through the plugin namespace', async () => {
  const { calls } = await loadPlugin()

  assert.deepStrictEqual(
    calls.map(call => call.path),
    ['/participants']
  )
  assert.strictEqual(calls[0].options.method, undefined, 'roster read is a GET')
})

test('provide() filters the cached roster by handle prefix', async () => {
  const { find } = await loadPlugin()
  const { provide } = find('composer.atCompletions').data

  assert.deepStrictEqual(provide('cl'), [
    { display: '@claude', insert: '@claude', meta: 'External · Claude Code' }
  ])
  assert.deepStrictEqual(
    provide('c').map(item => item.insert),
    ['@claude', '@codex']
  )
  assert.deepStrictEqual(provide('zzz'), [])
})

test('provide() offers the whole roster for an empty query, status included', async () => {
  const { find } = await loadPlugin()
  const { provide } = find('composer.atCompletions').data

  assert.deepStrictEqual(provide(''), [
    { display: '@claude', insert: '@claude', meta: 'External · Claude Code' },
    { display: '@codex', insert: '@codex', meta: 'External · Codex' },
    { display: '@mock', insert: '@mock', meta: 'External · Mock Agent · offline' }
  ])
})

test('provide() tolerates a typed "@" prefix and odd input without throwing', async () => {
  const { find } = await loadPlugin()
  const { provide } = find('composer.atCompletions').data

  assert.deepStrictEqual(
    provide('@CO').map(item => item.insert),
    ['@codex']
  )
  assert.deepStrictEqual(provide(undefined).length, 3)
  assert.deepStrictEqual(provide(null).length, 3)
})

test('a failing roster fetch yields no rows instead of throwing', async () => {
  const { find } = await loadPlugin({
    rest: async () => {
      throw new Error('backend disabled')
    }
  })
  const { provide } = find('composer.atCompletions').data

  assert.deepStrictEqual(provide('cl'), [], 'no roster, no rows')
  assert.deepStrictEqual(provide(''), [])
})

test('a malformed roster row is dropped, and a participant may not claim @hermes', async () => {
  const { find } = await loadPlugin({
    rest: async () => ({
      participants: [
        ROSTER[0],
        { display_name: 'Impostor', handle: 'hermes', id: 'x' },
        { display_name: 'No handle', id: 'y' },
        null,
        { display_name: 'Dupe', handle: 'CLAUDE', id: 'claude:other' }
      ]
    })
  })
  const { provide } = find('composer.atCompletions').data

  assert.deepStrictEqual(
    provide('').map(item => item.insert),
    ['@claude'],
    'hermes, handle-less, null and duplicate rows are rejected'
  )
})

test('a roster fetch that never resolves cannot block the popover', async () => {
  const { find } = await loadPlugin({ rest: () => new Promise(() => undefined) })
  const { provide } = find('composer.atCompletions').data

  assert.deepStrictEqual(provide('cl'), [], 'provide stays synchronous')
})
