/**
 * Shared loader for the desktop-half tests.
 *
 * The plugin is loaded the way the renderer loads it: as ESM, uncompiled, with
 * `@hermes/plugin-sdk` swapped for a per-test stub. The swap is a string
 * rewrite of the single import line into a prelude that reads the stub off
 * globalThis, and the rewritten source is imported as a `data:` URL — so every
 * load is a FRESH module instance (module-level roster and dispatch state
 * included) and no node flags are needed.
 *
 * Underscore-prefixed so neither `node --test 'tests/desktop/**\/*.test.mjs'`
 * nor bare `node --test` mistakes it for a suite.
 */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
const SDK_IMPORT = /^import \{[^}]*\} from '@hermes\/plugin-sdk'\r?\n/m

let seq = 0

/** Let every pending microtask AND the stubbed REST promises settle. */
export const flush = () => new Promise(resolve => setImmediate(resolve))

/**
 * Register the plugin against stubbed SDK + ctx surfaces.
 *
 * @param participants roster returned by `GET /participants` (default: none)
 * @param dispatch     handler for `POST /dispatch`, receives the request body
 * @param rest         full `ctx.rest` override; wins over the two above
 * @param sessionId    `host.state.focusedSessionId`, a value or a getter
 */
export async function loadPlugin({ dispatch, participants = [], rest, sessionId = 'sess-1' } = {}) {
  const calls = []
  const registrations = []
  const notifications = []
  const notices = []
  const listeners = new Map()

  const stubHost = {
    notify: input => {
      notices.push(input)

      return 'notice-id'
    },
    notifyError: (error, fallback) => {
      notifications.push({ error, fallback })

      return fallback
    },
    onEvent: (type, listener) => {
      const set = listeners.get(type) ?? new Set()

      set.add(listener)
      listeners.set(type, set)

      return () => set.delete(listener)
    },
    state: {
      focusedSessionId: {
        get: () => (typeof sessionId === 'function' ? sessionId() : sessionId)
      }
    }
  }

  const nonce = `relay-${seq++}`

  globalThis.__RELAY_SDK__ = globalThis.__RELAY_SDK__ ?? {}
  globalThis.__RELAY_SDK__[nonce] = {
    COMPOSER_AREAS: { atCompletions: 'composer.atCompletions', middleware: 'composer.middleware' },
    host: stubHost
  }

  assert.match(SOURCE, SDK_IMPORT, 'plugin.js imports the SDK on one line')

  const code = SOURCE.replace(
    SDK_IMPORT,
    `const { COMPOSER_AREAS, host } = globalThis.__RELAY_SDK__[${JSON.stringify(nonce)}]\n`
  )
  const module = await import(`data:text/javascript;base64,${Buffer.from(code, 'utf8').toString('base64')}`)
  const plugin = module.default

  const ctx = {
    onDispose: () => undefined,
    register: contribution => {
      registrations.push(contribution)

      return () => undefined
    },
    rest: async (path, options = {}) => {
      calls.push({ options, path })

      if (rest) {
        return rest(path, options)
      }

      if (path === '/participants') {
        return { participants }
      }

      if (path === '/dispatch') {
        return dispatch
          ? dispatch(options.body)
          : { ok: true, turns: [{ participant_id: 'claude:default', participant_turn_id: 'pturn-1' }] }
      }

      throw new Error(`unexpected REST path ${path}`)
    }
  }

  plugin.register(ctx)
  await flush()

  const find = area => registrations.find(contribution => contribution.area === area)

  return {
    calls,
    /** Every `POST /dispatch` recorded so far. */
    dispatches: () => calls.filter(call => call.path === '/dispatch'),
    /** Push a gateway event at whatever the plugin subscribed with. */
    emit: (type, event) => {
      for (const listener of listeners.get(type) ?? []) {
        listener(event)
      }
    },
    find,
    handler: find('composer.middleware')?.data.handler,
    listeners,
    notices,
    notifications,
    plugin,
    registrations
  }
}
