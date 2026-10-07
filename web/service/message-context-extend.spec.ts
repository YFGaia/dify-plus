import Cookies from 'js-cookie'
import { deleteMessageContext, messageContextList } from './message-context-extend'

vi.mock('@/config', () => ({
  API_PREFIX: '/console/api',
  CSRF_COOKIE_NAME: () => 'csrf_token',
  CSRF_HEADER_NAME: 'X-CSRF-Token',
}))

const fetchMock = vi.fn<typeof fetch>()
const query = { conversation_id: 'conversation /&?', message_id: 'answer /&?' }

beforeEach(() => {
  vi.clearAllMocks()
  Cookies.remove('csrf_token')
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(({ task }) => {
  Object.assign(task.meta, { assertionCount: expect.getState().assertionCalls })
  Cookies.remove('csrf_token')
  vi.unstubAllGlobals()
})

describe('message context transport', () => {
  it('skips both requests without a Console CSRF cookie', async () => {
    expect.assertions(3)
    expect(await messageContextList({ conversation_id: query.conversation_id })).toEqual([])
    expect(await deleteMessageContext(query)).toBeUndefined()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('sends the generated GET query and reads a string array', async () => {
    expect.assertions(6)
    Cookies.set('csrf_token', 'console-session')
    fetchMock.mockResolvedValueOnce(Response.json(['answer-2', 'answer-1']))
    expect(await messageContextList({ conversation_id: query.conversation_id })).toEqual([
      'answer-2',
      'answer-1',
    ])
    const [input, init] = fetchMock.mock.calls[0] ?? []
    const request = input as Request
    expect(new URL(request.url).pathname).toBe('/console/api/message/context')
    expect(Object.fromEntries(new URL(request.url).searchParams)).toEqual({
      conversation_id: query.conversation_id,
    })
    expect(request.method).toBe('GET')
    expect(request.headers.get('X-CSRF-Token')).toBe('console-session')
    expect(init).toMatchObject({ credentials: 'include', cache: 'no-store', redirect: 'error' })
  })

  it('sends only the selected conversation and message in DELETE and accepts "ok"', async () => {
    expect.assertions(4)
    Cookies.set('csrf_token', 'console-session')
    fetchMock.mockResolvedValueOnce(Response.json('ok'))
    expect(await deleteMessageContext(query)).toBe('ok')
    const request = fetchMock.mock.calls[0]?.[0] as Request
    expect(request.method).toBe('DELETE')
    expect(Object.fromEntries(new URL(request.url).searchParams)).toEqual(query)
    expect(request.headers.get('X-CSRF-Token')).toBe('console-session')
  })

  it.each(['GET', 'DELETE'])(
    'propagates %s 401 without refreshing or changing location',
    async (method) => {
      expect.assertions(3)
      Cookies.set('csrf_token', 'expired-session')
      const before = window.location.href
      fetchMock.mockResolvedValueOnce(Response.json({ message: 'Unauthorized' }, { status: 401 }))
      const call =
        method === 'GET'
          ? messageContextList({ conversation_id: query.conversation_id })
          : deleteMessageContext(query)
      await expect(call).rejects.toMatchObject({ status: 401 })
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(window.location.href).toBe(before)
    },
  )

  it('rejects malformed list responses', async () => {
    expect.assertions(1)
    Cookies.set('csrf_token', 'console-session')
    fetchMock.mockResolvedValueOnce(Response.json({ data: ['answer-1'] }))
    await expect(messageContextList({ conversation_id: 'conversation-1' })).rejects.toThrow()
  })

  it.each([{}, 'failed'])('rejects an unsuccessful deletion response %j', async (response) => {
    expect.assertions(1)
    Cookies.set('csrf_token', 'console-session')
    fetchMock.mockResolvedValueOnce(Response.json(response))
    await expect(deleteMessageContext(query)).rejects.toThrow()
  })

  it('rejects empty required IDs before sending requests', async () => {
    expect.assertions(4)
    Cookies.set('csrf_token', 'console-session')
    await expect(messageContextList({ conversation_id: '' })).rejects.toThrow()
    await expect(deleteMessageContext({ ...query, conversation_id: '' })).rejects.toThrow()
    await expect(deleteMessageContext({ ...query, message_id: '' })).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reads the current cookie again for DELETE after an authenticated GET', async () => {
    expect.assertions(3)
    Cookies.set('csrf_token', 'console-session')
    fetchMock.mockResolvedValueOnce(Response.json(['answer-1']))
    expect(await messageContextList({ conversation_id: 'conversation-1' })).toEqual(['answer-1'])
    Cookies.remove('csrf_token')
    expect(await deleteMessageContext(query)).toBeUndefined()
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
