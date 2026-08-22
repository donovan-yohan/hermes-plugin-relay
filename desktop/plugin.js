/**
 * Relay's native Desktop client. This is an uncompiled ESM disk plugin: it owns
 * one full page and its sidebar entry, and talks only to its scoped plugin API.
 */

import {
  Button,
  EmptyState,
  ErrorState,
  Loader,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
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

let pluginContext = null

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

function normalizeConnection(response) {
  const status = typeof response === 'string' ? response : response?.status

  if (!CONNECTION_STATES.has(status)) {
    throw new Error('Relay returned an invalid connection status.')
  }

  return { message: text(response?.message), status }
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

function ConnectionBanner({ connection, onAuthorize, onRetry, pending }) {
  const copy = {
    auth_required: {
      action: 'Authorize Relay',
      body: 'Relay needs authorization before channels can be updated.',
      title: 'Authorization required'
    },
    error: {
      action: 'Retry connection',
      body: connection.message || 'Relay returned a recoverable connection error.',
      title: 'Relay needs attention'
    },
    loading: {
      body: 'Checking the Relay connection…',
      title: 'Connecting to Relay'
    },
    offline: {
      action: 'Retry connection',
      body: 'Showing cached transcript data. Your draft is preserved and sending is paused.',
      title: 'Relay is offline'
    },
    ready: {
      body: 'Channel updates are live.',
      title: 'Relay connected'
    }
  }[connection.status] || {
    action: 'Retry connection',
    body: 'Relay returned an unknown state.',
    title: 'Relay needs attention'
  }

  const action = connection.status === 'auth_required' ? onAuthorize : onRetry

  return jsxs('section', {
    'aria-live': 'polite',
    className: cn(
      'flex shrink-0 items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-2 text-sm',
      connection.status === 'ready' ? 'text-(--ui-text-secondary)' : 'text-(--ui-text-primary)'
    ),
    'data-connection': connection.status,
    role: 'status',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-2',
        children: [
          jsx(StatusDot, { tone: connection.status === 'ready' ? 'good' : connection.status === 'offline' ? 'bad' : 'warn' }),
          jsxs('div', {
            className: 'min-w-0',
            children: [
              jsx('div', { className: 'font-medium', children: copy.title }),
              jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: copy.body })
            ]
          })
        ]
      }),
      copy.action
        ? jsx(Button, {
            disabled: pending,
            onClick: () => void action(),
            size: 'sm',
            type: 'button',
            variant: 'secondary',
            children: pending ? 'Working…' : copy.action
          })
        : null
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
      description: 'Connect or authorize Relay, then retry to load the channels available to you.',
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

function RelayPage() {
  const ctx = pluginContext
  const [connection, setConnection] = useState({ message: '', status: 'loading' })
  const [channels, setChannels] = useState([])
  const [channelError, setChannelError] = useState('')
  const [selectedChannelId, setSelectedChannelId] = useState('')
  const [history, setHistory] = useState({})
  const [draft, setDraft] = useState('')
  const [pending, setPending] = useState(false)
  const [sending, setSending] = useState(false)
  const [retry, setRetry] = useState(null)

  const connectionRef = useRef(connection)
  const selectedRef = useRef(selectedChannelId)
  const historyRef = useRef(history)
  const historyGeneration = useRef(0)
  const storedSelectionRead = useRef(false)
  const retryRef = useRef(null)

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

  const noteAuthRequired = useCallback(error => {
    if (isAuthError(error)) {
      setConnection({ message: '', status: 'auth_required' })

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

      const authRequired = noteAuthRequired(error)
      patchHistory(channelId, current => ({
        ...current,
        error: authRequired ? 'Relay authorization is required to refresh this transcript.' : errorMessage(error, 'Relay could not refresh this transcript.'),
        loading: false
      }))
    }
  }, [ctx, noteAuthRequired, patchHistory])

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
      setConnection({ message: 'Relay’s local API bridge is unavailable.', status: 'error' })

      return
    }

    setPending(true)

    try {
      const status = normalizeConnection(await ctx.rest('/connection/status'))

      setConnection(status)
      if (status.status !== 'ready') {
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
        if (noteAuthRequired(error)) {
          return
        }

        setChannelError(errorMessage(error, 'Relay could not refresh channels.'))
      }
    } catch (error) {
      if (!noteAuthRequired(error)) {
        setConnection({ message: errorMessage(error, 'Relay could not check the connection.'), status: 'error' })
      }
    } finally {
      setPending(false)
    }
  }, [chooseChannel, ctx, noteAuthRequired])

  const refreshLatest = useCallback(async () => {
    if (connectionRef.current.status === 'ready' && selectedRef.current) {
      await loadHistory(selectedRef.current)
    }
  }, [loadHistory])

  const authorize = useCallback(async () => {
    if (typeof ctx?.rest !== 'function') {
      setConnection({ message: 'Relay’s local API bridge is unavailable.', status: 'error' })

      return
    }

    setPending(true)

    try {
      await ctx.rest('/connection/authorize', { method: 'POST' })
      await refreshPage()
    } catch (error) {
      if (!noteAuthRequired(error)) {
        setConnection({ message: errorMessage(error, 'Relay authorization could not be started.'), status: 'error' })
      }
    } finally {
      setPending(false)
    }
  }, [ctx, noteAuthRequired, refreshPage])

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
      setRetry({ ...attempt, error: isAuthError(error) ? 'Relay authorization is required before sending.' : errorMessage(error, 'Relay could not confirm this message. Retry safely with the same message id.') })
      noteAuthRequired(error)
    } finally {
      setSending(false)
      // An accepted post (or an ambiguous transport result) can change history.
      void loadHistory(channelId)
    }
  }, [ctx, draft, loadHistory, noteAuthRequired])

  useEffect(() => {
    void refreshPage()
  }, [refreshPage])

  useEffect(() => {
    if (typeof ctx?.socket !== 'function') {
      return undefined
    }

    try {
      return ctx.socket('/events', () => {
        void refreshLatest()
      })
    } catch {
      return undefined
    }
  }, [ctx, refreshLatest])

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
        ? 'Authorize Relay before sending.'
        : 'Write a message…'

  return jsxs('main', {
    'aria-label': 'Relay channels',
    className: 'flex h-full min-h-0 flex-col text-(--ui-text-primary)',
    children: [
      jsx(ConnectionBanner, { connection, onAuthorize: authorize, onRetry: refreshPage, pending }),
      jsxs('div', {
        className: 'flex min-h-0 flex-1 flex-col md:flex-row',
        children: [
          jsxs('aside', {
            'aria-label': 'Relay channels',
            className: 'flex min-h-36 shrink-0 flex-col border-b border-(--ui-stroke-tertiary) md:w-72 md:border-r md:border-b-0',
            children: [
              jsx('div', { className: 'px-4 pt-4 text-xs font-medium uppercase tracking-wide text-(--ui-text-tertiary)', children: 'Channels' }),
              jsx(ChannelList, { channels, error: channelError, loading: pending && channels.length === 0, onRetry: refreshPage, onSelect: chooseChannel, selectedChannelId })
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
                    children: [jsx('h1', { className: 'truncate text-sm font-medium', children: selectedChannel?.name || 'Relay' }), selectedChannel?.summary ? jsx('p', { className: 'truncate text-xs text-(--ui-text-tertiary)', children: selectedChannel.summary }) : null]
                  }),
                  jsx(Button, { disabled: pending, onClick: () => void refreshPage(), size: 'sm', type: 'button', variant: 'ghost', children: 'Refresh' })
                ]
              }),
              jsx(Transcript, { channel: selectedChannel, entry: transcript, onRetry: refreshLatest }),
              jsxs('form', {
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
                          jsx(Button, { 'data-testid': 'retry-send', disabled: !canSend || retry.channelId !== selectedChannelId, onClick: () => void send(true), size: 'xs', type: 'button', variant: 'secondary', children: 'Retry send' })
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
    ctx.registerMany([
      {
        area: ROUTES_AREA,
        data: { path: RELAY_ROUTE },
        id: 'page',
        render: () => jsx(RelayPage, {})
      },
      {
        area: SIDEBAR_NAV_AREA,
        data: { codicon: 'comment-discussion', label: 'Relay', path: RELAY_ROUTE },
        id: 'nav',
        order: 55
      }
    ])
  }
}
