/**
 * Relay's native Desktop client. This is an uncompiled ESM disk plugin: it owns
 * a top-level sidebar tab plus a full workspace, and talks only to its scoped
 * plugin API.
 */

import {
  Button,
  CopyButton,
  EmptyState,
  ErrorState,
  host,
  Loader,
  ROUTES_AREA,
  StatusDot,
  Textarea,
  cn
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'hermes-plugin-relay'
const RELAY_ROUTE = '/relay'
const POLL_INTERVAL_MS = 3_000
const HISTORY_LIMIT = 50
const CONNECTION_STATES = new Set(['ready', 'offline', 'auth_required', 'error'])
const LOGIN_STATES = new Set(['idle', 'pending', 'ready', 'denied', 'expired', 'consumed'])
const NON_RETRYABLE_STATUS_CODES = new Set([400, 404, 409, 413])
const RELAY_WORKSPACE_ID = `${PLUGIN_ID}:home`
const HARNESS_PROVIDERS = ['claude', 'codex', 'hermes', 'opencode', 'pi', 'prime-agent', 'dsh', 'antigravity']
const HARNESS_META = {
  claude: { label: 'Claude Code' },
  codex: { label: 'Codex' },
  hermes: { label: 'Hermes' },
  opencode: { label: 'OpenCode' },
  pi: { label: 'Pi' },
  'prime-agent': { label: 'Prime Agent' },
  dsh: { label: 'DeepSeek Harness' },
  antigravity: { label: 'Antigravity' }
}
const STATUS_TONES = {
  installed: 'good',
  unavailable: 'bad',
  unsupported: 'idle'
}

let pluginContext = null
let relayWorkspaceClose = null

function text(value, fallback = '') {
  return typeof value === 'string' ? value : fallback
}

function errorMessage(error, fallback) {
  const message = text(error?.message).trim()

  return message || fallback
}

function statusCode(error) {
  const explicit = error?.statusCode ?? error?.status

  if (Number.isInteger(explicit)) {
    return explicit
  }

  const match = /(?:^|\D)([45]\d{2})(?::|\s)/.exec(text(error?.message))

  return match ? Number(match[1]) : 0
}

function isAuthError(error) {
  const status = statusCode(error)

  return status === 401 || status === 403
}

function isRetryableError(error) {
  if (typeof error?.retryable === 'boolean') {
    return error.retryable
  }

  const message = text(error?.message)
  const jsonStart = message.indexOf('{')

  if (jsonStart >= 0) {
    try {
      const envelope = JSON.parse(message.slice(jsonStart))

      if (typeof envelope?.error?.retryable === 'boolean') {
        return envelope.error.retryable
      }
    } catch {
      // Fall through to the stable HTTP classification below.
    }
  }

  return !NON_RETRYABLE_STATUS_CODES.has(statusCode(error))
}

function normalizeLaneConnection(response) {
  const status = typeof response === 'string' ? response : response?.status

  if (!CONNECTION_STATES.has(status)) {
    throw new Error('Relay returned an invalid connection status.')
  }

  return { message: text(response?.message), status }
}

function normalizeConnections(response) {
  const channels = normalizeLaneConnection(response?.channels)
  const harnesses = normalizeLaneConnection(response?.harnesses)

  return {
    channels: {
      ...channels,
      guidance: text(response?.channels?.guidance)
    },
    harnesses: {
      ...harnesses,
      loginAvailable: response?.harnesses?.loginAvailable === true
    }
  }
}

function normalizeHarnessLogin(response) {
  const status = text(response?.status)

  if (!LOGIN_STATES.has(status)) {
    throw new Error('Relay returned an invalid harness login status.')
  }
  if (status !== 'pending') {
    return { message: text(response?.message), status }
  }

  const code = text(response?.code).trim()
  const expiresAt = text(response?.expiresAt).trim()
  const verificationUrl = text(response?.verificationUrl).trim()
  let parsed

  try {
    parsed = new URL(verificationUrl)
  } catch {
    throw new Error('Relay returned an invalid harness approval URL.')
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || !code || !expiresAt) {
    throw new Error('Relay returned invalid harness login details.')
  }

  return { code, expiresAt, message: text(response?.message), status, verificationUrl: parsed.href }
}

function normalizeChannels(response) {
  const rows = Array.isArray(response) ? response : response?.channels

  if (!Array.isArray(rows)) {
    throw new Error('Relay returned an invalid channel list.')
  }

  return rows.flatMap(row => {
    const id = text(row?.id).trim()

    if (!id) {
      return []
    }

    return [{
      archived: row?.archived === true || row?.status === 'archived',
      id,
      name: text(row?.name, text(row?.title, id)).trim() || id,
      summary: text(row?.summary, text(row?.description))
    }]
  })
}

function normalizedAttribution(row) {
  const candidate = text(row?.sender?.kind, text(row?.author?.type, text(row?.authorType, text(row?.author_type, text(row?.senderType, text(row?.sender_type, text(row?.role))))))).toLowerCase()
  const kind = candidate === 'human' || candidate === 'agent' || candidate === 'system' ? candidate : 'system'
  const name = text(row?.sender?.displayName, text(row?.author?.displayName, text(row?.author?.display_name, text(row?.authorName, text(row?.author_name, text(row?.senderName, text(row?.sender_name))))))).trim()

  return { kind, name }
}

function normalizeMessages(response) {
  if (response?.archived === true || response?.status === 'archived') {
    return { archived: true, messages: [] }
  }

  const rows = Array.isArray(response) ? response : response?.messages

  if (!Array.isArray(rows)) {
    throw new Error('Relay returned an invalid message history.')
  }

  return {
    archived: false,
    messages: rows.flatMap((row, index) => {
      const id = text(row?.id, text(row?.messageId, `relay-message-${index}`)).trim()
      const body = text(row?.body?.text, text(row?.text, text(row?.content)))

      if (!id || typeof body !== 'string') {
        return []
      }

      return [{
        attribution: normalizedAttribution(row),
        id,
        text: body,
        timestamp: text(row?.createdAt, text(row?.created_at, text(row?.timestamp)))
      }]
    })
  }
}

function newClientMessageId() {
  try {
    if (typeof globalThis.crypto?.randomUUID === 'function') {
      return globalThis.crypto.randomUUID()
    }
  } catch {
    // A deterministic fallback is not needed; uniqueness within this page is.
  }

  return `relay-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}

function emptyHistory() {
  return { archived: false, error: '', loading: false, messages: [] }
}

function normalizeHarnesses(response) {
  const rows = Array.isArray(response) ? response : response?.harnesses

  if (!Array.isArray(rows)) {
    throw new Error('Relay returned an invalid harness report.')
  }

  return rows.flatMap(row => {
    const provider = text(row?.provider).trim()
    const status = text(row?.status).trim()

    if (!provider || !HARNESS_PROVIDERS.includes(provider) || !STATUS_TONES[status]) {
      return []
    }

    return [{
      label: HARNESS_META[provider].label,
      provider,
      sessionCount: Number.isInteger(row?.sessionCount) && row.sessionCount >= 0 ? row.sessionCount : 0,
      status,
      version: text(row?.version).trim()
    }]
  })
}

function normalizeHarnessSessions(response, provider) {
  const rows = Array.isArray(response) ? response : response?.sessions

  if (!Array.isArray(rows)) {
    throw new Error(`Relay returned an invalid session list for ${HARNESS_META[provider]?.label || provider}.`)
  }

  return rows.flatMap((row, index) => {
    const id = text(row?.id).trim()

    if (!id) {
      return []
    }

    return [{
      canWatch: row?.canWatch === true,
      cwd: text(row?.cwd).trim(),
      id,
      preview: text(row?.preview),
      provider,
      redacted: row?.redacted === true,
      timestamp: text(row?.updatedAt),
      title: text(row?.title).trim() || `Session ${index + 1}`
    }]
  })
}

function normalizeHarnessSnapshot(response, fallbackProvider, fallbackId) {
  const snapshot = response?.snapshot

  if (!snapshot || typeof snapshot !== 'object') {
    throw new Error('Relay returned an invalid session snapshot.')
  }

  const eventTypes = Array.isArray(snapshot.eventTypes)
    ? snapshot.eventTypes.filter(kind => typeof kind === 'string')
    : []

  return {
    capturedAt: text(snapshot.capturedAt),
    eventTypes,
    id: text(snapshot.id).trim() || fallbackId,
    lineCount: Number.isInteger(snapshot.lineCount) ? snapshot.lineCount : null,
    byteCount: Number.isInteger(snapshot.byteCount) ? snapshot.byteCount : null,
    preview: text(snapshot.preview),
    provider: text(snapshot.provider).trim() || fallbackProvider,
    redacted: snapshot.redacted === true
  }
}

function formatStamp(stamp) {
  if (!stamp) {
    return ''
  }

  const parsed = new Date(stamp)

  if (Number.isNaN(parsed.getTime())) {
    return stamp
  }

  return parsed.toLocaleString()
}

function relativeTime(stamp) {
  if (!stamp) {
    return 'unknown time'
  }

  const parsed = new Date(stamp)

  if (Number.isNaN(parsed.getTime())) {
    return stamp
  }

  const seconds = Math.round((Date.now() - parsed.getTime()) / 1000)

  if (seconds < 45) {
    return 'just now'
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)}m ago`
  }
  if (seconds < 86400) {
    return `${Math.round(seconds / 3600)}h ago`
  }

  return `${Math.round(seconds / 86400)}d ago`
}

function safeStorageGet(ctx) {
  try {
    const value = ctx?.storage?.get?.('relay.selection.channelId', '')

    return text(value).trim()
  } catch {
    return ''
  }
}

function safeStorageSet(ctx, channelId) {
  try {
    ctx?.storage?.set?.('relay.selection.channelId', channelId)
  } catch {
    // Selection is a convenience, never a prerequisite for Relay access.
  }
}

function closeRelayWorkspace() {
  const close = relayWorkspaceClose

  relayWorkspaceClose = null
  if (typeof close === 'function') {
    try {
      close()
    } catch {
      // The workspace may already have been closed from its own tab.
    }
  }
}

function openRelayWorkspace() {
  if (relayWorkspaceClose) {
    return true
  }

  if (typeof host?.openWorkspace !== 'function') {
    return false
  }

  try {
    relayWorkspaceClose = host.openWorkspace(RELAY_WORKSPACE_ID, {
      minWidth: '32rem',
      onClose: () => {
        relayWorkspaceClose = null
      },
      render: () => jsx(RelayPage, {}),
      title: 'Relay'
    })

    return typeof relayWorkspaceClose === 'function'
  } catch {
    relayWorkspaceClose = null
    return false
  }
}

function openRelaySurface() {
  if (openRelayWorkspace()) {
    return
  }

  try {
    host.navigate(RELAY_ROUTE)
  } catch {
    // Older shells without the main-workspace door keep the in-pane button.
  }
}

function RelayPane() {
  return jsx('section', {
    'aria-label': 'Relay workspace launcher',
    className: 'flex h-full min-h-0 flex-col px-3 py-4 text-(--ui-text-primary)',
    children: jsx(EmptyState, {
      action: jsx(Button, { onClick: openRelaySurface, size: 'sm', type: 'button', children: 'Open Relay' }),
      description: 'Channels, transcripts, and messaging live in their own workspace.',
      title: 'Relay channels'
    })
  })
}

function laneTone(status) {
  return status === 'ready' ? 'good' : status === 'offline' ? 'bad' : 'warn'
}

function laneCopy(lane, connection) {
  const shared = {
    error: {
      body: connection.message || 'Relay returned a recoverable connection error.',
      title: `${lane} need attention`
    },
    loading: {
      body: 'Checking this Relay connection…',
      title: `Checking ${lane.toLowerCase()}`
    },
    offline: {
      body: 'Relay is unavailable. Existing data stays visible while reconnecting.',
      title: `${lane} are offline`
    }
  }

  if (shared[connection.status]) {
    return shared[connection.status]
  }
  if (lane === 'Channels') {
    return connection.status === 'ready'
      ? { body: 'Channel updates and sending are live.', title: 'Channels connected' }
      : {
          body: connection.guidance || 'Channels need an operator-client credential or an approved channel-scoped grant.',
          title: 'Operator access required'
        }
  }
  return connection.status === 'ready'
    ? { body: 'Native harness sessions are available read-only.', title: 'Harnesses connected' }
    : {
        body: connection.loginAvailable
          ? 'Use Relay Login to approve read-only harness access in your browser.'
          : 'Relay Login is unavailable because its connection URL is invalid.',
        title: 'Harnesses not connected'
      }
}

function HarnessLoginPrompt({ login, onCancel, onPoll, pending }) {
  if (login?.status !== 'pending') {
    return null
  }

  return jsxs('div', {
    className: 'mt-3 border-t border-(--ui-stroke-tertiary) pt-3',
    'data-login-status': 'pending',
    children: [
      jsx('p', {
        className: 'text-xs text-(--ui-text-secondary)',
        children: 'Open the approval page, enter your Relay PIN, and confirm this code. This panel updates automatically.'
      }),
      jsxs('div', {
        className: 'mt-2 flex flex-wrap items-center gap-2',
        children: [
          jsx('code', {
            className: 'select-all rounded-sm border border-(--ui-stroke-secondary) px-2 py-1 font-mono text-sm tracking-wider',
            'data-login-code': login.code,
            children: login.code
          }),
          jsx(CopyButton, { appearance: 'inline', label: 'Copy code', text: login.code })
        ]
      }),
      jsxs('div', {
        className: 'mt-2 flex min-w-0 flex-wrap items-center gap-2',
        children: [
          jsx('a', {
            'aria-label': 'Open approval page',
            className: 'min-w-0 truncate text-xs text-(--ui-accent) underline underline-offset-2',
            href: login.verificationUrl,
            rel: 'noopener noreferrer',
            target: '_blank',
            children: login.verificationUrl
          }),
          jsx(CopyButton, { appearance: 'inline', label: 'Copy URL', text: login.verificationUrl })
        ]
      }),
      jsxs('div', {
        className: 'mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', { children: login.message || `Expires ${formatStamp(login.expiresAt)}` }),
          jsxs('div', {
            className: 'flex items-center gap-1',
            children: [
              jsx(Button, { disabled: pending, onClick: () => void onPoll(), size: 'xs', type: 'button', variant: 'ghost', children: 'Check now' }),
              jsx(Button, { disabled: pending, onClick: () => void onCancel(), size: 'xs', type: 'button', variant: 'ghost', children: 'Cancel' })
            ]
          })
        ]
      })
    ]
  })
}

function ConnectionPanel({ connections, login, loginPending, onCancelLogin, onConnectHarnesses, onOpenChannelSetup, onPollLogin, onRetry, pending, setupNote }) {
  const channelCopy = laneCopy('Channels', connections.channels)
  const harnessCopy = laneCopy('Harnesses', connections.harnesses)
  const loginNeedsRestart = ['denied', 'expired', 'consumed', 'error'].includes(login?.status)
  const showConnect = connections.harnesses.status === 'auth_required' && connections.harnesses.loginAvailable && login?.status !== 'pending'

  return jsxs('section', {
    'aria-label': 'Relay connections',
    'aria-live': 'polite',
    className: 'grid shrink-0 gap-2 border-b border-(--ui-stroke-tertiary) p-3 text-sm md:grid-cols-2',
    role: 'status',
    children: [
      jsxs('div', {
        className: 'min-w-0 border border-(--ui-stroke-tertiary) p-3',
        'data-connection': connections.channels.status,
        'data-lane': 'channels',
        children: [
          jsxs('div', {
            className: 'flex items-start justify-between gap-3',
            children: [
              jsxs('div', {
                className: 'flex min-w-0 items-start gap-2',
                children: [
                  jsx(StatusDot, { tone: laneTone(connections.channels.status) }),
                  jsxs('div', {
                    className: 'min-w-0',
                    children: [
                      jsx('div', { className: 'font-medium', children: channelCopy.title }),
                      jsx('div', { className: 'mt-0.5 text-xs text-(--ui-text-tertiary)', children: channelCopy.body })
                    ]
                  })
                ]
              }),
              connections.channels.status !== 'ready'
                ? jsxs('div', {
                    className: 'flex items-center gap-1',
                    children: [
                      connections.channels.status === 'auth_required'
                        ? jsx(Button, { disabled: pending, onClick: () => void onOpenChannelSetup(), size: 'xs', type: 'button', variant: 'secondary', children: 'Open Relay' })
                        : null,
                      jsx(Button, { disabled: pending, onClick: () => void onRetry(), size: 'xs', type: 'button', variant: 'ghost', children: 'Refresh' })
                    ]
                  })
                : null
            ]
          }),
          setupNote && connections.channels.status !== 'ready'
            ? jsx('p', { className: 'mt-2 text-xs text-(--ui-text-secondary)', 'data-channel-setup': 'true', children: setupNote })
            : null
        ]
      }),
      jsxs('div', {
        className: 'min-w-0 border border-(--ui-stroke-tertiary) p-3',
        'data-connection': connections.harnesses.status,
        'data-lane': 'harnesses',
        children: [
          jsxs('div', {
            className: 'flex items-start justify-between gap-3',
            children: [
              jsxs('div', {
                className: 'flex min-w-0 items-start gap-2',
                children: [
                  jsx(StatusDot, { tone: laneTone(connections.harnesses.status) }),
                  jsxs('div', {
                    className: 'min-w-0',
                    children: [
                      jsx('div', { className: 'font-medium', children: harnessCopy.title }),
                      jsx('div', { className: 'mt-0.5 text-xs text-(--ui-text-tertiary)', children: harnessCopy.body })
                    ]
                  })
                ]
              }),
              showConnect
                ? jsx(Button, {
                    disabled: loginPending,
                    onClick: () => void onConnectHarnesses(),
                    size: 'xs',
                    type: 'button',
                    variant: 'secondary',
                    children: loginPending ? 'Starting…' : loginNeedsRestart ? 'Start again' : 'Connect Harnesses'
                  })
                : connections.harnesses.status === 'offline' || connections.harnesses.status === 'error'
                  ? jsx(Button, { disabled: pending, onClick: () => void onRetry(), size: 'xs', type: 'button', variant: 'ghost', children: 'Refresh' })
                  : null
            ]
          }),
          loginNeedsRestart
            ? jsx('p', { className: 'mt-2 text-xs text-(--ui-text-secondary)', 'data-login-status': login.status, children: login.message || 'Relay Login did not complete. Start a fresh flow.' })
            : null,
          jsx(HarnessLoginPrompt, { login, onCancel: onCancelLogin, onPoll: onPollLogin, pending: loginPending })
        ]
      })
    ]
  })
}

function ChannelList({ channels, error, loading, onRetry, onSelect, selectedChannelId }) {
  if (loading && channels.length === 0) {
    return jsx('div', { className: 'flex flex-1 items-center justify-center', children: jsx(Loader, {}) })
  }

  if (error && channels.length === 0) {
    return jsx(ErrorState, {
      action: jsx(Button, { onClick: () => void onRetry(), size: 'sm', type: 'button', variant: 'secondary', children: 'Retry channels' }),
      description: error,
      title: 'Channels could not be loaded'
    })
  }

  if (channels.length === 0) {
    return jsx(EmptyState, {
      description: 'Configure Relay operator access, then retry to load the channels available to you.',
      title: 'No Relay channels yet'
    })
  }

  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto px-2 py-2',
    children: [
      error
        ? jsxs('div', {
            className: 'mb-1 flex items-center justify-between gap-2 px-2 text-xs text-(--ui-text-tertiary)',
            children: [jsx('span', { children: error }), jsx(Button, { onClick: () => void onRetry(), size: 'xs', type: 'button', variant: 'text', children: 'Retry' })]
          })
        : null,
      ...channels.map(channel =>
        jsx(Button, {
          'aria-current': channel.id === selectedChannelId ? 'page' : undefined,
          className: cn('h-auto justify-start px-2 py-2 text-left', channel.id === selectedChannelId && 'bg-(--chrome-action-hover)'),
          'data-channel-id': channel.id,
          onClick: () => void onSelect(channel.id),
          type: 'button',
          variant: 'ghost',
          children: jsxs('span', {
            className: 'min-w-0',
            children: [
              jsxs('span', { className: 'block truncate text-sm', children: [channel.name, channel.archived ? ' · archived' : ''] }),
              channel.summary ? jsx('span', { className: 'block truncate text-xs text-(--ui-text-tertiary)', children: channel.summary }) : null
            ]
          })
        }, channel.id)
      )
    ]
  })
}

function MessageRow({ message }) {
  const label = message.attribution.name || message.attribution.kind

  return jsxs('article', {
    className: 'border-b border-(--ui-stroke-tertiary) px-4 py-3 last:border-b-0',
    'data-attribution': message.attribution.kind,
    'data-message-id': message.id,
    children: [
      jsxs('header', {
        className: 'mb-1 flex items-center gap-2 text-xs text-(--ui-text-tertiary)',
        children: [jsx('span', { className: 'font-medium uppercase tracking-wide', children: label }), message.timestamp ? jsx('time', { children: message.timestamp }) : null]
      }),
      jsx('div', { className: 'whitespace-pre-wrap break-words text-sm text-(--ui-text-primary)', children: message.text })
    ]
  })
}

function Transcript({ channel, entry, onRetry }) {
  if (!channel) {
    return jsx(EmptyState, { description: 'Choose a Relay channel to inspect its messages.', title: 'Select a channel' })
  }

  if (entry.loading && entry.messages.length === 0) {
    return jsx('div', { className: 'flex flex-1 items-center justify-center', children: jsx(Loader, {}) })
  }

  if (channel.archived || entry.archived) {
    return jsx(EmptyState, {
      description: 'This channel is archived. Its previous messages remain available when Relay returns them.',
      title: 'Channel archived'
    })
  }

  if (entry.error && entry.messages.length === 0) {
    return jsx(ErrorState, {
      action: jsx(Button, { onClick: () => void onRetry(), size: 'sm', type: 'button', variant: 'secondary', children: 'Retry history' }),
      description: entry.error,
      title: 'Transcript could not be loaded'
    })
  }

  if (entry.messages.length === 0) {
    return jsx(EmptyState, { description: 'Send the first message when Relay is ready.', title: 'No messages in this channel yet' })
  }

  return jsxs('div', {
    className: 'min-h-0 flex-1 overflow-y-auto',
    children: [
      entry.error
        ? jsxs('div', {
            className: 'flex items-center justify-between gap-2 border-b border-(--ui-stroke-tertiary) px-4 py-2 text-xs text-(--ui-text-tertiary)',
            children: [jsx('span', { children: entry.error }), jsx(Button, { onClick: () => void onRetry(), size: 'xs', type: 'button', variant: 'text', children: 'Retry' })]
          })
        : null,
      ...entry.messages.map(message => jsx(MessageRow, { message }, message.id))
    ]
  })
}

function HarnessGroupHeader({ expanded, harness, onToggle }) {
  const countLabel = harness.status === 'installed'
    ? `${harness.sessionCount} session${harness.sessionCount === 1 ? '' : 's'}`
    : 'not available'

  return jsxs('button', {
    'aria-expanded': expanded,
    className: cn(
      'flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-(--chrome-action-hover)',
      expanded && 'bg-(--chrome-action-hover)'
    ),
    'data-harness': harness.provider,
    'data-harness-status': harness.status,
    onClick: onToggle,
    type: 'button',
    children: [
      jsx('span', {
        'aria-hidden': true,
        className: cn(
          'text-xs text-(--ui-text-tertiary) transition-transform',
          expanded && 'rotate-90'
        ),
        children: '▶'
      }),
      jsx(StatusDot, { tone: STATUS_TONES[harness.status] }),
      jsxs('span', {
        className: 'min-w-0 flex-1',
        children: [
          jsxs('span', { className: 'flex items-baseline justify-between gap-2', children: [
            jsx('span', { className: 'truncate font-medium text-(--ui-text-primary)', children: harness.label }),
            jsx('span', { className: 'shrink-0 text-xs tabular-nums text-(--ui-text-tertiary)', children: countLabel })
          ] }),
          harness.version
            ? jsx('span', { className: 'block truncate text-xs text-(--ui-text-quaternary)', children: `v${harness.version}` })
            : null
        ]
      })
    ]
  })
}

function HarnessSessionRow({ onSelect, selected, session }) {
  return jsxs('button', {
    'aria-current': selected ? 'true' : undefined,
    className: cn(
      'ml-6 flex w-[calc(100%-1.5rem)] flex-col gap-0.5 border-l border-(--ui-stroke-tertiary) py-1.5 pl-3 pr-2 text-left transition-colors hover:bg-(--chrome-action-hover)',
      selected ? 'bg-(--chrome-action-hover)' : 'border-transparent'
    ),
    'data-session-id': session.id,
    onClick: onSelect,
    type: 'button',
    children: [
      jsxs('span', { className: 'flex items-baseline justify-between gap-2', children: [
        jsx('span', { className: 'truncate text-sm text-(--ui-text-primary)', children: session.title }),
        jsx('span', { className: 'shrink-0 text-xs text-(--ui-text-tertiary)', children: relativeTime(session.timestamp) })
      ] }),
      session.cwd
        ? jsx('span', { className: 'truncate text-xs text-(--ui-text-tertiary)', children: session.cwd })
        : null,
      session.preview
        ? jsx('span', { className: 'truncate text-xs text-(--ui-text-quaternary)', children: `${session.redacted ? '[redacted] ' : ''}${session.preview}` })
        : null
    ]
  }, session.id)
}

function HarnessList({
  expandedProviders,
  harnesses,
  onRetry,
  onSessionSelect,
  onToggle,
  selectedSessionId,
  sessionCache,
  sessionsLoading
}) {
  if (harnesses.length === 0) {
    return jsx(EmptyState, {
      description: 'Relay reported no supported harnesses on this machine yet.',
      title: 'No harnesses detected'
    })
  }

  return jsxs('div', {
    className: 'flex min-h-0 flex-1 flex-col overflow-y-auto pb-2',
    role: 'group',
    'aria-label': 'Harnesses',
    children: [
      sessionsLoading.error
        ? jsxs('div', {
            className: 'flex items-center justify-between gap-2 px-4 py-1 text-xs text-(--ui-text-tertiary)',
            children: [jsx('span', { className: 'truncate', children: sessionsLoading.error }), jsx(Button, { onClick: () => void onRetry(), size: 'xs', type: 'button', variant: 'text', children: 'Retry' })]
          })
        : null,
      ...harnesses.map(harness => {
        const expanded = expandedProviders.includes(harness.provider)
        const entry = sessionCache[harness.provider]

        return jsxs('div', { key: harness.provider, children: [
          jsx(HarnessGroupHeader, { expanded, harness, onToggle: () => onToggle(harness.provider) }),
          expanded && harness.status === 'installed'
            ? !entry || entry.loading
              ? jsx('div', { className: 'flex justify-center py-3', children: jsx(Loader, {}) })
              : entry.error
                ? jsx('div', { className: 'px-4 py-2 pl-9 text-xs text-(--ui-text-tertiary)', children: entry.error })
                : entry.sessions.length === 0
                  ? jsx('div', { className: 'px-4 py-2 pl-9 text-xs text-(--ui-text-tertiary)', children: 'No native sessions found for this harness.' })
                  : entry.sessions.map(session =>
                      jsx(HarnessSessionRow, {
                        onSelect: () => onSessionSelect(harness.provider, session.id),
                        selected: selectedSessionId === session.id,
                        session
                      }, session.id))
            : null
        ] }, harness.provider)
      })
    ]
  })
}

function SessionDetail({ detail }) {
  if (!detail) {
    return jsx(EmptyState, {
      description: 'Expand a harness and pick one of its native sessions to inspect it.',
      title: 'Select a session'
    })
  }

  if (detail.loading) {
    return jsx('div', { className: 'flex flex-1 items-center justify-center', children: jsx(Loader, {}) })
  }

  if (detail.error) {
    return jsx(ErrorState, {
      action: null,
      description: detail.error,
      title: 'Session could not be loaded'
    })
  }

  const snapshot = detail.snapshot

  return jsxs('div', {
    className: 'min-h-0 flex-1 overflow-y-auto',
    children: [
      jsxs('header', {
        className: 'flex items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-2 text-xs text-(--ui-text-tertiary)',
        children: [
          jsxs('span', { className: 'truncate', children: [
            HARNESS_META[snapshot.provider]?.label || snapshot.provider,
            ' · ',
            snapshot.id
          ] }),
          snapshot.capturedAt ? jsx('time', { children: formatStamp(snapshot.capturedAt) }) : null
        ]
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-(--ui-stroke-tertiary) px-4 py-2 text-xs text-(--ui-text-tertiary)',
        children: [
          snapshot.lineCount != null ? jsx('span', { children: `${snapshot.lineCount} records` }) : null,
          snapshot.byteCount != null ? jsx('span', { children: `${(snapshot.byteCount / 1024).toFixed(1)} KiB` }) : null,
          snapshot.eventTypes.length ? jsx('span', { children: snapshot.eventTypes.join(', ') }) : null,
          snapshot.redacted ? jsx('span', { 'data-redacted': 'true', children: 'redacted preview' }) : null
        ]
      }),
      jsxs('p', {
        className: 'whitespace-pre-wrap break-words px-4 py-3 text-sm text-(--ui-text-primary)',
        children: snapshot.preview || 'This transcript has no redactable preview text yet.'
      })
    ]
  })
}

function RelayPage() {
  const ctx = pluginContext
  const [connections, setConnections] = useState({
    channels: { guidance: '', message: '', status: 'loading' },
    harnesses: { loginAvailable: false, message: '', status: 'loading' }
  })
  const [harnessLogin, setHarnessLogin] = useState({ message: '', status: 'idle' })
  const [channelSetup, setChannelSetup] = useState('')
  const [loginPending, setLoginPending] = useState(false)
  const [channels, setChannels] = useState([])
  const [channelError, setChannelError] = useState('')
  const [selectedChannelId, setSelectedChannelId] = useState('')
  const [history, setHistory] = useState({})
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [sending, setSending] = useState(false)
  const [retry, setRetry] = useState(null)
  // Harness view state. The channels surface keeps its own selection; the
  // harness view is a read-only inspector over native provider sessions.
  const [surface, setSurface] = useState('channels')
  const [harnesses, setHarnesses] = useState([])
  const [harnessError, setHarnessError] = useState('')
  const [expandedProviders, setExpandedProviders] = useState([])
  const [sessionCache, setSessionCache] = useState({})
  const [sessionSelection, setSessionSelection] = useState({ id: '', provider: '' })
  const [sessionDetail, setSessionDetail] = useState(null)

  const connection = connections.channels
  const harnessConnection = connections.harnesses
  const connectionRef = useRef(connection)
  const selectedRef = useRef(selectedChannelId)
  const historyRef = useRef(history)
  const historyGeneration = useRef(0)
  const storedSelectionRead = useRef(false)
  const retryRef = useRef(null)
  const sessionGeneration = useRef({})
  const detailGeneration = useRef(0)
  const loginPollInFlight = useRef(false)

  connectionRef.current = connection
  selectedRef.current = selectedChannelId
  historyRef.current = history

  const patchHistory = useCallback((channelId, patch) => {
    const current = historyRef.current[channelId] || emptyHistory()
    const nextEntry = typeof patch === 'function' ? patch(current) : { ...current, ...patch }
    const next = { ...historyRef.current, [channelId]: nextEntry }

    historyRef.current = next
    setHistory(next)
  }, [])

  const noteChannelAuthRequired = useCallback(error => {
    if (isAuthError(error)) {
      setConnections(current => ({
        ...current,
        channels: { ...current.channels, message: '', status: 'auth_required' }
      }))

      return true
    }

    return false
  }, [])

  const noteHarnessAuthRequired = useCallback(error => {
    if (isAuthError(error)) {
      setConnections(current => ({
        ...current,
        harnesses: { ...current.harnesses, message: '', status: 'auth_required' }
      }))

      return true
    }

    return false
  }, [])

  const loadHistory = useCallback(async channelId => {
    if (!channelId || typeof ctx?.rest !== 'function') {
      return
    }

    const generation = ++historyGeneration.current
    patchHistory(channelId, current => ({ ...current, error: '', loading: true }))

    try {
      const response = await ctx.rest(`/channels/${encodeURIComponent(channelId)}/messages?limit=${HISTORY_LIMIT}`)
      const normalized = normalizeMessages(response)

      if (generation !== historyGeneration.current || channelId !== selectedRef.current) {
        return
      }

      patchHistory(channelId, { ...emptyHistory(), archived: normalized.archived, messages: normalized.messages })
    } catch (error) {
      if (generation !== historyGeneration.current || channelId !== selectedRef.current) {
        return
      }

      const authRequired = noteChannelAuthRequired(error)
      patchHistory(channelId, current => ({
        ...current,
        error: authRequired ? 'Relay authorization is required to refresh this transcript.' : errorMessage(error, 'Relay could not refresh this transcript.'),
        loading: false
      }))
    }
  }, [ctx, noteChannelAuthRequired, patchHistory])

  const chooseChannel = useCallback(async channelId => {
    if (!channelId) {
      return
    }

    selectedRef.current = channelId
    historyGeneration.current += 1
    setSelectedChannelId(channelId)
    safeStorageSet(ctx, channelId)
    await loadHistory(channelId)
  }, [ctx, loadHistory])

  const refreshPage = useCallback(async () => {
    if (typeof ctx?.rest !== 'function') {
      const unavailable = { message: 'Relay’s local API bridge is unavailable.', status: 'error' }

      setConnections({
        channels: { ...unavailable, guidance: '' },
        harnesses: { ...unavailable, loginAvailable: false }
      })

      return
    }

    setPending(true)

    try {
      const status = normalizeConnections(await ctx.rest('/connection/status'))

      setConnections(status)
      if (status.harnesses.status === 'ready') {
        setHarnessLogin({ message: '', status: 'ready' })
      } else if (status.harnesses.status === 'auth_required') {
        try {
          setHarnessLogin(normalizeHarnessLogin(await ctx.rest('/harnesses/login')))
        } catch (error) {
          setHarnessLogin({ message: errorMessage(error, 'Relay Login status could not be checked.'), status: 'error' })
        }
      }

      if (status.channels.status !== 'ready') {
        return
      }

      try {
        const nextChannels = normalizeChannels(await ctx.rest('/channels'))

        setChannels(nextChannels)
        setChannelError('')

        if (!storedSelectionRead.current) {
          storedSelectionRead.current = true
          const stored = safeStorageGet(ctx)

          if (stored && nextChannels.some(channel => channel.id === stored)) {
            selectedRef.current = stored
            setSelectedChannelId(stored)
          }
        }

        const selected = nextChannels.some(channel => channel.id === selectedRef.current)
          ? selectedRef.current
          : nextChannels[0]?.id || ''

        if (selected) {
          await chooseChannel(selected)
        } else {
          selectedRef.current = ''
          setSelectedChannelId('')
        }
      } catch (error) {
        if (noteChannelAuthRequired(error)) {
          return
        }

        setChannelError(errorMessage(error, 'Relay could not refresh channels.'))
      }
    } catch (error) {
      const message = errorMessage(error, 'Relay could not check the connection.')

      setConnections({
        channels: { guidance: '', message, status: 'error' },
        harnesses: { loginAvailable: false, message, status: 'error' }
      })
    } finally {
      setPending(false)
    }
  }, [chooseChannel, ctx, noteChannelAuthRequired])

  const refreshLatest = useCallback(async () => {
    if (connectionRef.current.status === 'ready' && selectedRef.current) {
      await loadHistory(selectedRef.current)
    }
  }, [loadHistory])

  const send = useCallback(async manualRetry => {
    const channelId = selectedRef.current
    const retryAttempt = manualRetry ? retryRef.current : null
    const messageText = retryAttempt?.text ?? draft.trim()
    const clientMessageId = retryAttempt?.clientMessageId ?? newClientMessageId()

    if (!channelId || !messageText || connectionRef.current.status !== 'ready' || typeof ctx?.rest !== 'function') {
      return
    }

    setSending(true)

    try {
      const response = await ctx.rest(`/channels/${encodeURIComponent(channelId)}/messages`, {
        body: { clientMessageId, format: 'markdown', text: messageText },
        method: 'POST'
      })

      if (response?.ok === false) {
        throw new Error(text(response.error, 'Relay did not accept this message.'))
      }

      retryRef.current = null
      setRetry(null)
      setDraft('')
    } catch (error) {
      const attempt = { channelId, clientMessageId, text: messageText }

      retryRef.current = attempt
      setRetry({
        ...attempt,
        error: isAuthError(error) ? 'Relay authorization is required before sending.' : errorMessage(error, 'Relay could not confirm this message. Retry safely with the same message id.'),
        retryable: isRetryableError(error)
      })
      noteChannelAuthRequired(error)
    } finally {
      setSending(false)
      // An accepted post (or an ambiguous transport result) can change history.
      void loadHistory(channelId)
    }
  }, [ctx, draft, loadHistory, noteChannelAuthRequired])

  const loadHarnesses = useCallback(async () => {
    if (typeof ctx?.rest !== 'function') {
      setHarnessError('Relay’s local API bridge is unavailable.')

      return
    }

    try {
      const nextHarnesses = normalizeHarnesses(await ctx.rest('/harnesses'))

      setHarnesses(nextHarnesses)
      setHarnessError('')
    } catch (error) {
      if (!noteHarnessAuthRequired(error)) {
        setHarnessError(errorMessage(error, 'Relay could not list harness sessions.'))
      }
    }
  }, [ctx, noteHarnessAuthRequired])

  const pollHarnessLogin = useCallback(async () => {
    if (typeof ctx?.rest !== 'function' || loginPollInFlight.current) {
      return
    }

    loginPollInFlight.current = true
    try {
      const next = normalizeHarnessLogin(await ctx.rest('/harnesses/login'))

      setHarnessLogin(next)
      if (next.status === 'ready') {
        setConnections(current => ({
          ...current,
          harnesses: { ...current.harnesses, message: '', status: 'ready' }
        }))
        await refreshPage()
      }
    } catch (error) {
      const message = errorMessage(error, 'Relay Login status could not be checked.')

      setHarnessLogin(current => current.status === 'pending'
        ? { ...current, message }
        : { message, status: 'error' })
    } finally {
      loginPollInFlight.current = false
    }
  }, [ctx, refreshPage])

  const startHarnessLogin = useCallback(async () => {
    if (typeof ctx?.rest !== 'function') {
      setHarnessLogin({ message: 'Relay’s local API bridge is unavailable.', status: 'error' })

      return
    }

    setLoginPending(true)
    try {
      const next = normalizeHarnessLogin(await ctx.rest('/harnesses/login/start', { method: 'POST' }))

      setHarnessLogin(next)
      if (next.status === 'ready') {
        setConnections(current => ({
          ...current,
          harnesses: { ...current.harnesses, message: '', status: 'ready' }
        }))
        await refreshPage()
      }
    } catch (error) {
      setHarnessLogin({ message: errorMessage(error, 'Relay Login could not be started.'), status: 'error' })
    } finally {
      setLoginPending(false)
    }
  }, [ctx, refreshPage])

  // Channel operator access is still grant-based: the backend hands back only
  // the validated loopback Relay root so Desktop can open setup without ever
  // seeing a grant or credential.
  const openChannelSetup = useCallback(async () => {
    if (typeof ctx?.rest !== 'function') {
      setChannelSetup('Relay\u2019s local API bridge is unavailable.')

      return
    }

    const recovery = 'Supply an approved scoped grant when starting Hermes, then restart Hermes and refresh.'

    try {
      const onboarding = await ctx.rest('/connection/onboarding')
      const url = text(onboarding?.url)
      const opened = Boolean(url)
        && typeof ctx?.os?.openExternal === 'function'
        && Boolean(await ctx.os.openExternal(url))

      setChannelSetup(opened ? `Relay opened in your browser. ${recovery}` : recovery)
    } catch (error) {
      setChannelSetup(`Relay could not be opened. ${recovery} ${errorMessage(error, '')}`.trim())
    }
  }, [ctx])

  const cancelHarnessLogin = useCallback(async () => {
    if (typeof ctx?.rest !== 'function') {
      return
    }

    setLoginPending(true)
    try {
      setHarnessLogin(normalizeHarnessLogin(await ctx.rest('/harnesses/login', { method: 'DELETE' })))
    } catch (error) {
      const message = errorMessage(error, 'Relay Login could not be cancelled.')

      setHarnessLogin(current => current.status === 'pending'
        ? { ...current, message }
        : { message, status: 'error' })
    } finally {
      setLoginPending(false)
    }
  }, [ctx])

  const toggleProvider = useCallback(provider => {
    setExpandedProviders(current =>
      current.includes(provider)
        ? current.filter(item => item !== provider)
        : [...current, provider]
    )

    if (expandedProviders.includes(provider) || !harnesses.some(row => row.provider === provider && row.status === 'installed')) {
      return
    }

    // First expansion loads (or refreshes once per mount) that harness's rows.
    // Generations are per provider so one harness's refresh can never discard
    // another's in-flight response.
    const generation = (sessionGeneration.current[provider] || 0) + 1

    sessionGeneration.current = { ...sessionGeneration.current, [provider]: generation }

    setSessionCache(cache => ({
      ...cache,
      [provider]: cache[provider] ? { ...cache[provider], loading: true } : { error: '', loading: true, sessions: [] }
    }))

    void (async () => {
      try {
        const sessions = normalizeHarnessSessions(await ctx.rest(`/harnesses/${encodeURIComponent(provider)}/sessions`), provider)

        if (generation !== sessionGeneration.current[provider]) {
          return
        }

        setSessionCache(cache => ({ ...cache, [provider]: { error: '', loading: false, sessions } }))
      } catch (error) {
        if (generation !== sessionGeneration.current[provider]) {
          return
        }

        noteHarnessAuthRequired(error)
        setSessionCache(cache => ({
          ...cache,
          [provider]: {
            error: isAuthError(error) ? 'Relay authorization is required for harness sessions.' : errorMessage(error, 'Relay could not list this harness’s sessions.'),
            loading: false,
            sessions: []
          }
        }))
      }
    })()
  }, [ctx, expandedProviders, harnesses, noteHarnessAuthRequired])

  const chooseSession = useCallback(async (provider, sessionId) => {
    if (!provider || !sessionId || typeof ctx?.rest !== 'function') {
      return
    }

    const generation = ++detailGeneration.current

    setSessionSelection({ id: sessionId, provider })
    setSessionDetail({ loading: true, snapshot: null })

    try {
      const snapshot = normalizeHarnessSnapshot(
        await ctx.rest(`/harnesses/${encodeURIComponent(provider)}/sessions/${encodeURIComponent(sessionId)}`),
        provider,
        sessionId
      )

      if (generation !== detailGeneration.current) {
        return
      }

      setSessionDetail({ loading: false, snapshot })
    } catch (error) {
      if (generation !== detailGeneration.current) {
        return
      }

      noteHarnessAuthRequired(error)
      setSessionDetail({
        loading: false,
        snapshot: null,
        error: isAuthError(error) ? 'Relay authorization is required for this session.' : errorMessage(error, 'Relay could not read this session snapshot.')
      })
    }
  }, [ctx, noteHarnessAuthRequired])

  useEffect(() => {
    void refreshPage()
  }, [refreshPage])

  // Harness rows load when the operator switches to the harness surface, and
  // refresh on each switch so a freshly started harness shows up.
  useEffect(() => {
    if (surface === 'harnesses' && harnessConnection.status === 'ready') {
      void loadHarnesses()
    }
  }, [harnessConnection.status, loadHarnesses, surface])

  useEffect(() => {
    if (harnessLogin.status !== 'pending') {
      return undefined
    }

    const timer = setInterval(() => {
      void pollHarnessLogin()
    }, POLL_INTERVAL_MS)

    return () => clearInterval(timer)
  }, [harnessLogin.status, pollHarnessLogin])

  useEffect(() => {
    if (connection.status !== 'ready' || !selectedChannelId) {
      return undefined
    }

    const timer = setInterval(() => {
      void refreshLatest()
    }, POLL_INTERVAL_MS)

    return () => clearInterval(timer)
  }, [connection.status, refreshLatest, selectedChannelId])

  const selectedChannel = channels.find(channel => channel.id === selectedChannelId) || null
  const transcript = history[selectedChannelId] || emptyHistory()
  const canSend = Boolean(selectedChannel) && !selectedChannel.archived && !transcript.archived && connection.status === 'ready' && !sending
  const composerHint = selectedChannel?.archived || transcript.archived
    ? 'This channel is archived.'
    : connection.status === 'offline'
      ? 'Relay is offline. Your draft is preserved until it reconnects.'
      : connection.status === 'auth_required'
        ? 'Channel operator access is required before sending.'
        : 'Write a message…'

  return jsxs('main', {
    'aria-label': 'Relay channels',
    className: 'flex h-full min-h-0 flex-col text-(--ui-text-primary)',
    children: [
      jsx(ConnectionPanel, {
        connections,
        login: harnessLogin,
        loginPending,
        onCancelLogin: cancelHarnessLogin,
        onConnectHarnesses: startHarnessLogin,
        onOpenChannelSetup: openChannelSetup,
        onPollLogin: pollHarnessLogin,
        onRetry: refreshPage,
        pending,
        setupNote: channelSetup
      }),
      jsxs('div', {
        className: 'flex min-h-0 flex-1 flex-col md:flex-row',
        children: [
          jsxs('aside', {
            'aria-label': 'Relay channels',
            className: 'flex min-h-36 shrink-0 flex-col border-b border-(--ui-stroke-tertiary) md:w-72 md:border-r md:border-b-0',
            children: [
              jsxs('div', {
                'aria-label': 'Relay surfaces',
                className: 'flex gap-1 px-3 pt-3',
                role: 'tablist',
                children: [
                  jsx(Button, {
                    'aria-selected': surface === 'channels',
                    'data-surface': 'channels',
                    onClick: () => setSurface('channels'),
                    role: 'tab',
                    size: 'sm',
                    type: 'button',
                    variant: surface === 'channels' ? 'secondary' : 'ghost',
                    children: 'Channels'
                  }),
                  jsx(Button, {
                    'aria-selected': surface === 'harnesses',
                    'data-surface': 'harnesses',
                    onClick: () => setSurface('harnesses'),
                    role: 'tab',
                    size: 'sm',
                    type: 'button',
                    variant: surface === 'harnesses' ? 'secondary' : 'ghost',
                    children: 'Harnesses'
                  })
                ]
              }),
              jsx('div', { className: 'px-4 pt-4 text-xs font-medium uppercase tracking-wide text-(--ui-text-tertiary)', children: surface === 'channels' ? 'Channels' : 'Harnesses' }),
              surface === 'channels'
                ? jsx(ChannelList, { channels, error: channelError, loading: pending && channels.length === 0, onRetry: refreshPage, onSelect: chooseChannel, selectedChannelId })
                : harnessError && harnesses.length === 0
                  ? jsxs('div', {
                      className: 'flex flex-1 flex-col items-center justify-center gap-2 px-4 text-sm text-(--ui-text-secondary)',
                      children: [
                        jsx('span', { className: 'text-center', children: harnessError }),
                        jsx(Button, { onClick: () => void loadHarnesses(), size: 'sm', type: 'button', variant: 'secondary', children: 'Retry harnesses' })
                      ]
                    })
                  : jsx(HarnessList, {
                      expandedProviders,
                      harnesses,
                      onRetry: loadHarnesses,
                      onSessionSelect: (provider, sessionId) => void chooseSession(provider, sessionId),
                      onToggle: toggleProvider,
                      selectedSessionId: sessionSelection.id,
                      sessionCache,
                      sessionsLoading: { error: '' }
                    })
            ]
          }),
          jsxs('section', {
            'aria-label': selectedChannel ? `${selectedChannel.name} transcript` : 'Relay transcript',
            className: 'flex min-h-0 min-w-0 flex-1 flex-col',
            children: [
              jsxs('header', {
                className: 'flex shrink-0 items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-3',
                children: [
                  jsxs('div', {
                    className: 'min-w-0',
                    children: [jsx('h1', { className: 'truncate text-sm font-medium', children: surface === 'harnesses'
                      ? (sessionDetail?.snapshot
                          ? `${HARNESS_META[sessionDetail.snapshot.provider]?.label || sessionDetail.snapshot.provider} session`
                          : 'Harness sessions')
                      : (selectedChannel?.name || 'Relay') }), surface === 'channels' && selectedChannel?.summary ? jsx('p', { className: 'truncate text-xs text-(--ui-text-tertiary)', children: selectedChannel.summary }) : null]
                  }),
                  jsx(Button, { disabled: pending, onClick: () => void refreshPage(), size: 'sm', type: 'button', variant: 'ghost', children: 'Refresh' })
                ]
              }),
              surface === 'channels'
                ? jsx(Transcript, { channel: selectedChannel, entry: transcript, onRetry: refreshLatest })
                : jsx(SessionDetail, { detail: sessionDetail }),
              surface === 'channels'
                ? jsxs('form', {
                className: 'shrink-0 border-t border-(--ui-stroke-tertiary) p-4',
                onSubmit: event => {
                  event.preventDefault()
                  void send(false)
                },
                children: [
                  retry
                    ? jsxs('div', {
                        className: 'mb-2 flex items-center justify-between gap-3 text-xs text-(--ui-text-secondary)',
                        role: 'alert',
                        children: [
                          jsx('span', { children: retry.error }),
                          retry.retryable
                            ? jsx(Button, { 'data-testid': 'retry-send', disabled: !canSend || retry.channelId !== selectedChannelId, onClick: () => void send(true), size: 'xs', type: 'button', variant: 'secondary', children: 'Retry send' })
                            : null
                        ]
                      })
                    : null,
                  jsx(Textarea, {
                    'aria-label': 'Relay message',
                    disabled: !selectedChannel || connection.status !== 'ready' || Boolean(selectedChannel?.archived || transcript.archived),
                    onChange: event => setDraft(text(event?.target?.value)),
                    onKeyDown: event => {
                      if ((event?.metaKey || event?.ctrlKey) && event?.key === 'Enter' && canSend) {
                        event.preventDefault()
                        void send(false)
                      }
                    },
                    placeholder: composerHint,
                    readOnly: connection.status !== 'ready',
                    rows: 3,
                    value: draft
                  }),
                  jsxs('div', {
                    className: 'mt-2 flex items-center justify-between gap-3',
                    children: [
                      jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: canSend ? 'Sends Markdown to Relay.' : composerHint }),
                      jsx(Button, { disabled: !canSend || !draft.trim(), type: 'submit', children: sending ? 'Sending…' : 'Send' })
                    ]
                  })
                ]
              })
                : null
            ]
          })
        ]
      })
    ]
  })
}

export default {
  defaultEnabled: false,
  description: 'Native Relay channel client for Hermes Desktop.',
  id: PLUGIN_ID,
  name: 'Relay',
  register(ctx) {
    pluginContext = ctx
    relayWorkspaceClose = null

    ctx.registerMany([
      {
        area: ROUTES_AREA,
        data: { path: RELAY_ROUTE },
        id: 'page',
        render: () => jsx(RelayPage, {})
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
        render: () => jsx(RelayPane, {}),
        title: 'Relay'
      }
    ])

    let stopVisibility = null

    try {
      const visibility = typeof host?.paneVisibility === 'function'
        ? host.paneVisibility(`${PLUGIN_ID}:pane`)
        : null
      const syncWorkspace = visible => {
        if (visible) {
          openRelaySurface()
        } else {
          closeRelayWorkspace()
        }
      }

      if (visibility && typeof visibility.get === 'function') {
        syncWorkspace(visibility.get())
      }
      if (visibility && typeof visibility.listen === 'function') {
        stopVisibility = visibility.listen(syncWorkspace)
      }
    } catch {
      // Older Desktop builds still expose the pane's explicit Open button.
    }

    if (typeof ctx.onDispose === 'function') {
      ctx.onDispose(() => {
        stopVisibility?.()
        closeRelayWorkspace()
        if (pluginContext === ctx) {
          pluginContext = null
        }
      })
    }
  }
}
