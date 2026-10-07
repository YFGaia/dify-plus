import type { ChatItem } from '../../types'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Cookies from 'js-cookie'
import { ChatWithHistoryContext, useChatWithHistoryContext } from '../../chat-with-history/context'
import Chat from '../index'

vi.mock('@/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config')>()),
  API_PREFIX: '/console/api',
  CSRF_COOKIE_NAME: () => 'csrf_token',
  CSRF_HEADER_NAME: 'X-CSRF-Token',
}))

// Rich message bodies, audio input and log viewers are independent of marker ownership.
vi.mock('../answer', () => ({
  default: ({ item }: { item: ChatItem }) => <article>{item.content}</article>,
}))
vi.mock('../question', () => ({ default: ({ item }: { item: ChatItem }) => <p>{item.content}</p> }))
vi.mock('../chat-input-area', () => ({ default: () => null }))
vi.mock('../chat-log-modals', () => ({ default: () => null }))

const fetchMock = vi.fn<typeof fetch>()
const restoreName = 'extend.configuration.restoreContext'
const items: ChatItem[] = [
  { id: 'question-1', content: 'Question one', isAnswer: false },
  { id: 'answer-1', content: 'Answer one', isAnswer: true },
  { id: 'question-2', content: 'Question two', isAnswer: false },
  { id: 'answer-2', content: 'Answer two', isAnswer: true },
]

function Conversation({ id, responding = false }: { id: string; responding?: boolean }) {
  const defaults = useChatWithHistoryContext()
  return (
    // oxlint-disable-next-line eslint-react/no-context-provider -- use-context-selector exposes a Provider and does not support React 19 context shorthand.
    <ChatWithHistoryContext.Provider value={{ ...defaults, currentConversationId: id }}>
      <Chat chatList={items} isResponding={responding} noChatInput noStopResponding />
    </ChatWithHistoryContext.Provider>
  )
}

let client: QueryClient
function renderConversation(id = 'conversation-1', responding = false) {
  const view = render(
    <QueryClientProvider client={client}>
      <Conversation id={id} responding={responding} />
    </QueryClientProvider>,
  )
  return {
    ...view,
    switchTo: (nextId: string, nextResponding = false) =>
      view.rerender(
        <QueryClientProvider client={client}>
          <Conversation id={nextId} responding={nextResponding} />
        </QueryClientProvider>,
      ),
  }
}

function requests() {
  return fetchMock.mock.calls.map(([input]) => {
    const request = input as Request
    return { method: request.method, query: Object.fromEntries(new URL(request.url).searchParams) }
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  Cookies.set('csrf_token', 'console-session')
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 300_000 }, mutations: { retry: false } },
  })
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(({ task }) => {
  Object.assign(task.meta, { assertionCount: expect.getState().assertionCalls })
  client.clear()
  Cookies.remove('csrf_token')
  vi.unstubAllGlobals()
})

describe('Chat message context markers', () => {
  it('makes no GET or DELETE and offers no restore action for an anonymous WebApp', async () => {
    Cookies.remove('csrf_token')
    const view = renderConversation()
    await act(async () => view.switchTo('conversation-2'))
    expect(screen.queryByRole('button', { name: restoreName })).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('skips context requests before a conversation exists', async () => {
    renderConversation('')
    await act(async () => {})
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('uses the current conversation and the selected answer, then refreshes after deleting', async () => {
    const user = userEvent.setup()
    fetchMock.mockResolvedValueOnce(Response.json(['question-1', 'answer-2']))
    renderConversation()
    const button = await screen.findByRole('button', { name: restoreName })
    expect(screen.getAllByRole('button', { name: restoreName })).toHaveLength(1)
    expect(requests()).toEqual([{ method: 'GET', query: { conversation_id: 'conversation-1' } }])
    fetchMock.mockResolvedValueOnce(Response.json('ok')).mockResolvedValueOnce(Response.json([]))
    await user.click(button)
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: restoreName })).not.toBeInTheDocument(),
    )
    await waitFor(() =>
      expect(requests()).toEqual([
        { method: 'GET', query: { conversation_id: 'conversation-1' } },
        { method: 'DELETE', query: { conversation_id: 'conversation-1', message_id: 'answer-2' } },
        { method: 'GET', query: { conversation_id: 'conversation-1' } },
      ]),
    )
  })

  it('does not reuse the previous conversation markers or IDs after switching', async () => {
    const user = userEvent.setup()
    fetchMock.mockResolvedValueOnce(Response.json(['answer-1']))
    const view = renderConversation()
    await screen.findByRole('button', { name: restoreName })
    fetchMock.mockResolvedValueOnce(Response.json(['answer-2']))
    view.switchTo('conversation-2')
    const button = await screen.findByRole('button', { name: restoreName })
    fetchMock.mockResolvedValueOnce(Response.json('ok')).mockResolvedValueOnce(Response.json([]))
    await user.click(button)
    await waitFor(() =>
      expect(requests()).toContainEqual({
        method: 'DELETE',
        query: { conversation_id: 'conversation-2', message_id: 'answer-2' },
      }),
    )
    expect(
      requests()
        .filter((request) => request.method === 'GET')
        .every((request) => !('app_id' in request.query)),
    ).toBe(true)
  })

  it('ignores a late list response for a previous conversation', async () => {
    let resolveOld!: (value: Response) => void
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve
      }),
    )
    const view = renderConversation()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    fetchMock.mockResolvedValueOnce(Response.json([]))
    view.switchTo('conversation-2')
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    await act(async () => resolveOld(Response.json(['answer-1'])))
    expect(screen.queryByRole('button', { name: restoreName })).not.toBeInTheDocument()
  })

  it('refreshes the markers when an answer finishes responding', async () => {
    fetchMock.mockResolvedValueOnce(Response.json([]))
    const view = renderConversation()
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    view.switchTo('conversation-1', true)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    fetchMock.mockResolvedValueOnce(Response.json(['answer-2']))
    view.switchTo('conversation-1', false)
    await screen.findByRole('button', { name: restoreName })
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('keeps a late DELETE result scoped to its original conversation', async () => {
    const user = userEvent.setup()
    fetchMock.mockResolvedValueOnce(Response.json(['answer-1']))
    const view = renderConversation()
    const oldButton = await screen.findByRole('button', { name: restoreName })
    let resolveDelete!: (value: Response) => void
    fetchMock.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveDelete = resolve
      }),
    )
    await user.click(oldButton)
    expect(oldButton).toBeDisabled()
    fetchMock.mockResolvedValueOnce(Response.json(['answer-2']))
    view.switchTo('conversation-2')
    const currentButton = await screen.findByRole('button', { name: restoreName })
    await act(async () => resolveDelete(Response.json('ok')))
    expect(currentButton).toBeInTheDocument()
    expect(currentButton).toBeEnabled()
    expect(requests()).toEqual([
      { method: 'GET', query: { conversation_id: 'conversation-1' } },
      { method: 'DELETE', query: { conversation_id: 'conversation-1', message_id: 'answer-1' } },
      { method: 'GET', query: { conversation_id: 'conversation-2' } },
    ])
  })

  it('treats GET 401 as a failed optional query without navigation or retry', async () => {
    const before = window.location.href
    fetchMock.mockResolvedValueOnce(Response.json({ message: 'Unauthorized' }, { status: 401 }))
    renderConversation()
    await waitFor(() =>
      expect(client.getQueryState(['message-context-extend', 'conversation-1'])?.status).toBe(
        'error',
      ),
    )
    expect(screen.queryByRole('button', { name: restoreName })).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(window.location.href).toBe(before)
  })

  it('preserves the marker after a failed DELETE and never changes location', async () => {
    const user = userEvent.setup()
    const before = window.location.href
    fetchMock.mockResolvedValueOnce(Response.json(['answer-1']))
    renderConversation()
    const button = await screen.findByRole('button', { name: restoreName })
    fetchMock.mockResolvedValueOnce(Response.json({ message: 'Unauthorized' }, { status: 401 }))
    await user.click(button)
    await waitFor(() => expect(button).toBeEnabled())
    expect(button).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(window.location.href).toBe(before)
  })

  it('skips DELETE if the Console cookie disappears after markers were loaded', async () => {
    const user = userEvent.setup()
    fetchMock.mockResolvedValueOnce(Response.json(['answer-1']))
    renderConversation()
    const button = await screen.findByRole('button', { name: restoreName })
    Cookies.remove('csrf_token')
    await user.click(button)
    await waitFor(() => expect(button).toBeEnabled())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(button).toBeInTheDocument()
  })
})
