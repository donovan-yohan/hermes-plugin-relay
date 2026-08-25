/** Shared uncompiled-ESM loader and tiny hook renderer for Relay Desktop tests. */

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(new URL('../../desktop/plugin.js', import.meta.url), 'utf8')
const SDK_IMPORT = /import \{[\s\S]*?\} from '@hermes\/plugin-sdk'\r?\n/
const REACT_IMPORT = /import \{[\s\S]*?\} from 'react'\r?\n/
const JSX_IMPORT = /import \{[\s\S]*?\} from 'react\/jsx-runtime'\r?\n/

let sequence = 0

export const flush = () => new Promise(resolve => setImmediate(resolve))

export function textContent(node) {
  if (node == null || node === false) {
    return ''
  }

  if (typeof node === 'string' || typeof node === 'number') {
    return String(node)
  }

  if (Array.isArray(node)) {
    return node.map(textContent).join('')
  }

  return textContent(node.props?.children)
}

export function findAll(node, predicate, matches = []) {
  if (node == null || node === false) {
    return matches
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      findAll(child, predicate, matches)
    }

    return matches
  }

  if (typeof node === 'object') {
    if (predicate(node)) {
      matches.push(node)
    }

    findAll(node.props?.children, predicate, matches)
  }

  return matches
}

export function findButton(node, label) {
  const button = findAll(node, item => item.type === 'button' && textContent(item) === label)[0]

  assert.ok(button, `button ${JSON.stringify(label)} is present`)

  return button
}

function jsx(type, props = {}, key) {
  return { key, props, type }
}

function component(tag, extra = {}) {
  return props => jsx(tag, { ...extra, ...props })
}

function createSdk(host) {
  return {
    Button: component('button'),
    EmptyState: props => jsx('empty-state', props),
    ErrorState: props => jsx('error-state', props),
    host,
    Loader: component('loader', { role: 'progressbar' }),
    ROUTES_AREA: 'routes',
    StatusDot: props => jsx('status-dot', props),
    Textarea: component('textarea'),
    cn: (...classes) => classes.flat().filter(Boolean).join(' ')
  }
}

function createRenderer(rootElement, hooks) {
  const root = {
    effects: [],
    hookIndex: 0,
    hooks: [],
    render: null,
    rootElement,
    tree: null
  }
  let active = null

  const sameDeps = (left, right) =>
    Array.isArray(left) && Array.isArray(right) && left.length === right.length && left.every((value, index) => Object.is(value, right[index]))

  const useState = initial => {
    const index = active.hookIndex++
    const record = active.hooks[index] ?? { value: typeof initial === 'function' ? initial() : initial }

    active.hooks[index] = record
    return [record.value, next => {
      const value = typeof next === 'function' ? next(record.value) : next

      if (!Object.is(value, record.value)) {
        record.value = value
        root.render()
      }
    }]
  }

  const useRef = initial => {
    const index = active.hookIndex++
    const record = active.hooks[index] ?? { current: initial }

    active.hooks[index] = record
    return record
  }

  const useMemo = (factory, deps) => {
    const index = active.hookIndex++
    const record = active.hooks[index]

    if (!record || !sameDeps(record.deps, deps)) {
      const next = { deps, value: factory() }

      active.hooks[index] = next
      return next.value
    }

    return record.value
  }

  const useCallback = (callback, deps) => useMemo(() => callback, deps)

  const useEffect = (effect, deps) => {
    const index = active.hookIndex++
    const record = active.hooks[index] ?? { cleanup: undefined, deps: undefined }

    active.hooks[index] = record
    if (!sameDeps(record.deps, deps)) {
      record.deps = deps
      root.effects.push({ effect, record })
    }
  }

  const resolve = element => {
    if (element == null || element === false || typeof element === 'string' || typeof element === 'number') {
      return element
    }

    if (Array.isArray(element)) {
      return element.map(resolve)
    }

    if (typeof element.type === 'function') {
      return resolve(element.type(element.props || {}))
    }

    return { ...element, props: { ...element.props, children: resolve(element.props?.children) } }
  }

  root.render = () => {
    active = root
    root.hookIndex = 0
    const output = root.rootElement.type(root.rootElement.props || {})

    active = null
    root.tree = resolve(output)
  }

  const runEffects = () => {
    while (root.effects.length) {
      const { effect, record } = root.effects.shift()

      record.cleanup?.()
      record.cleanup = effect() || undefined
    }
  }

  return {
    dispose: () => {
      for (const record of root.hooks) {
        record?.cleanup?.()
      }
    },
    flushEffects: runEffects,
    get tree() {
      return root.tree
    },
    hooks: { useCallback, useEffect, useMemo, useRef, useState },
    render: root.render
  }
}

/**
 * Load desktop/plugin.js as a fresh uncompiled ESM module, then expose the page
 * against a scoped REST/socket/storage stub. `rest` receives every request after
 * it has been recorded and can return or throw an endpoint-specific result.
 */
export async function loadRelay({
  channels = [{ id: 'general', name: 'General', summary: 'The main Relay channel' }],
  histories = { general: { messages: [] } },
  post,
  rest,
  status = { status: 'ready' },
  storedSelection = '',
  workspaceSupported = true
} = {}) {
  const calls = []
  const registrations = []
  const socketHandlers = []
  const storage = { get: [], set: [] }
  const timers = new Map()
  const navigations = []
  const paneListeners = new Set()
  const pluginDisposers = []
  const visibilityRequests = []
  const workspaceCloses = []
  const workspaceOpenings = []
  let paneVisible = false
  let timerId = 0
  const nonce = `relay-${sequence++}`
  const setIntervalStub = (callback, delay) => {
    const id = ++timerId

    timers.set(id, { callback, delay })
    return id
  }
  const clearIntervalStub = id => timers.delete(id)
  const visibility = {
    get: () => paneVisible,
    listen: callback => {
      paneListeners.add(callback)
      return () => paneListeners.delete(callback)
    }
  }
  const host = {
    navigate: path => navigations.push(path),
    openWorkspace: (id, options) => {
      const opening = { closed: false, id, options }

      workspaceOpenings.push(opening)
      return () => {
        if (opening.closed) {
          return
        }

        opening.closed = true
        workspaceCloses.push(id)
        options.onClose?.()
      }
    },
    paneVisibility: id => {
      visibilityRequests.push(id)
      return visibility
    }
  }

  if (!workspaceSupported) {
    delete host.openWorkspace
  }

  const renderer = createRenderer
  globalThis.__RELAY_DESKTOP_TEST__ = globalThis.__RELAY_DESKTOP_TEST__ ?? {}
  globalThis.__RELAY_DESKTOP_TEST__[nonce] = { sdk: createSdk(host), timers: { clearIntervalStub, setIntervalStub } }

  assert.match(SOURCE, SDK_IMPORT, 'plugin imports the SDK directly')
  assert.match(SOURCE, REACT_IMPORT, 'plugin imports React hooks directly')
  assert.match(SOURCE, JSX_IMPORT, 'plugin imports the JSX runtime directly')

  const prelude = `const __relay = globalThis.__RELAY_DESKTOP_TEST__[${JSON.stringify(nonce)}]\n`
  let code = SOURCE
    .replace(SDK_IMPORT, `${prelude}const { Button, EmptyState, ErrorState, host, Loader, ROUTES_AREA, StatusDot, Textarea, cn } = __relay.sdk\n`)
    .replace(REACT_IMPORT, `const { useCallback, useEffect, useRef, useState } = globalThis.__RELAY_DESKTOP_TEST__[${JSON.stringify(nonce)}].hooks\n`)
    .replace(JSX_IMPORT, `const { jsx, jsxs } = globalThis.__RELAY_DESKTOP_TEST__[${JSON.stringify(nonce)}].jsxRuntime\n`)
    .replace(/\bsetInterval\(/g, `globalThis.__RELAY_DESKTOP_TEST__[${JSON.stringify(nonce)}].timers.setIntervalStub(`)
    .replace(/\bclearInterval\(/g, `globalThis.__RELAY_DESKTOP_TEST__[${JSON.stringify(nonce)}].timers.clearIntervalStub(`)

  // Hooks must be attached after the page element exists; the loader shim reads
  // the same object at module evaluation time, so a tiny lazy proxy supplies it.
  const hookProxy = {}
  for (const name of ['useCallback', 'useEffect', 'useRef', 'useState']) {
    hookProxy[name] = (...args) => hookProxy.renderer.hooks[name](...args)
  }
  globalThis.__RELAY_DESKTOP_TEST__[nonce].hooks = hookProxy
  globalThis.__RELAY_DESKTOP_TEST__[nonce].jsxRuntime = { jsx, jsxs: jsx }

  const module = await import(`data:text/javascript;base64,${Buffer.from(code, 'utf8').toString('base64')}`)
  const plugin = module.default

  const ctx = {
    onDispose: dispose => pluginDisposers.push(dispose),
    registerMany: contributions => {
      registrations.push(...contributions)
      return () => undefined
    },
    rest: async (path, options = {}) => {
      calls.push({ options, path })
      if (rest) {
        return rest(path, options, calls)
      }

      if (path === '/connection/status') {
        return typeof status === 'function' ? status() : status
      }
      if (path === '/connection/authorize') {
        return { ok: true }
      }
      if (path === '/channels') {
        return { channels }
      }
      if (path.startsWith('/channels/') && path.endsWith('/messages?limit=50')) {
        const id = decodeURIComponent(path.slice('/channels/'.length, path.indexOf('/messages?')))

        return typeof histories[id] === 'function' ? histories[id]() : histories[id] ?? { messages: [] }
      }
      if (path.startsWith('/channels/') && path.endsWith('/messages') && options.method === 'POST') {
        return post ? post(options.body, path, calls) : { ok: true }
      }
      if (path === '/harnesses') {
        return { harnesses: [
          { provider: 'claude', sessionCount: 1, status: 'installed', version: '2.1.0' },
          { provider: 'codex', sessionCount: 1, status: 'installed' },
          { provider: 'hermes', sessionCount: 0, status: 'unsupported' },
          { provider: 'opencode', sessionCount: 0, status: 'unavailable' },
          { provider: 'pi', sessionCount: 0, status: 'unsupported' },
          { provider: 'prime-agent', sessionCount: 0, status: 'unsupported' },
          { provider: 'dsh', sessionCount: 0, status: 'unsupported' },
          { provider: 'antigravity', sessionCount: 0, status: 'unsupported' }
        ] }
      }
      const harnessSession = path.match(/^\/harnesses\/([a-z-]+)\/sessions\/([^/]+)$/)
      if (harnessSession) {
        return {
          snapshot: {
            capturedAt: '2026-08-25T01:00:00.000Z',
            eventTypes: ['user-message', 'assistant-message'],
            id: decodeURIComponent(harnessSession[2]),
            lineCount: 12,
            byteCount: 2048,
            preview: `snapshot for ${decodeURIComponent(harnessSession[2])}`,
            provider: harnessSession[1],
            redacted: true
          }
        }
      }
      const harnessList = path.match(/^\/harnesses\/([a-z-]+)\/sessions$/)
      if (harnessList) {
        return { sessions: [{
          canWatch: false,
          cwd: '/repo',
          id: `${harnessList[1]}-1`,
          preview: 'latest activity',
          provider: harnessList[1],
          redacted: true,
          timestamp: '2026-08-24T09:00:00.000Z',
          title: `Recent ${harnessList[1]} work`
        }] }
      }

      throw new Error(`unexpected scoped REST path ${path}`)
    },
    socket: (path, onMessage) => {
      socketHandlers.push({ onMessage, path })
      return () => {
        const index = socketHandlers.findIndex(item => item.onMessage === onMessage)
        if (index !== -1) {
          socketHandlers.splice(index, 1)
        }
      }
    },
    storage: {
      get: key => {
        storage.get.push(key)
        return key === 'relay.selection.channelId' ? storedSelection : undefined
      },
      set: (key, value) => storage.set.push({ key, value })
    }
  }

  plugin.register(ctx)

  const route = registrations.find(item => item.area === 'routes')
  const mount = () => {
    assert.ok(route?.render, 'route has a page renderer')
    const page = route.render()
    const app = renderer(page, null)

    hookProxy.renderer = app
    // The first render runs before the hooks proxy knows its renderer. Re-render
    // once after wiring it so every hook resolves through the active renderer.
    app.render()

    return {
      dispose: app.dispose,
      flushEffects: app.flushEffects,
      get tree() {
        return app.tree
      },
      async settle(turns = 8) {
        for (let index = 0; index < turns; index += 1) {
          app.flushEffects()
          await flush()
        }
      }
    }
  }

  return {
    calls,
    channels: () => calls.filter(call => call.path === '/channels'),
    dispatchSocket: payload => {
      for (const entry of [...socketHandlers]) {
        entry.onMessage(payload)
      }
    },
    disposePlugin: () => {
      for (const dispose of pluginDisposers.splice(0)) {
        dispose()
      }
    },
    histories: () => calls.filter(call => call.path.includes('/messages?limit=50')),
    mount,
    navigations,
    plugin,
    posts: () => calls.filter(call => call.options.method === 'POST' && call.path.includes('/messages')),
    registrations,
    setPaneVisible: visible => {
      paneVisible = visible
      for (const listener of [...paneListeners]) {
        listener(visible)
      }
    },
    socketHandlers,
    storage,
    tickPoll: () => {
      for (const timer of [...timers.values()]) {
        timer.callback()
      }
    },
    timers,
    visibilityRequests,
    workspaceCloses,
    workspaceOpenings
  }
}
