import { focusManager, MutationObserver, onlineManager, QueryClient } from '@tanstack/react-query'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

const handoff = 'A'.repeat(43)
const resultBody = {
  code: 'authorization_pending',
  correlation_id: '11111111-1111-4111-8111-111111111111',
  retry_allowed: false,
}
const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const deferred = () => Promise.withResolvers<void>()

async function loadClient(delay?: {
  entered: ReturnType<typeof deferred>
  release: ReturnType<typeof deferred>
}) {
  vi.resetModules()
  vi.doMock('@/config', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/config')>()),
    get API_PREFIX() {
      return '/console/api'
    },
  }))
  vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
  vi.doMock('@langgenius/dify-ui/toast', () => ({ toast: { error: vi.fn() } }))
  const refresh = vi.fn().mockResolvedValue(undefined)
  vi.doMock('../refresh-token', () => ({ refreshAccessTokenOrReLogin: refresh }))
  if (delay) {
    vi.doMock('@dify/contracts/api/console/orpc.gen', async (importOriginal) => {
      const actual = await importOriginal<typeof import('@dify/contracts/api/console/orpc.gen')>()
      return {
        ...actual,
        contractLoaders: {
          ...actual.contractLoaders,
          auth: async () => {
            delay.entered.resolve()
            await delay.release.promise
            return actual.contractLoaders.auth()
          },
        },
      }
    })
  }
  // Keep the actual base/ky recovery path available and observe whether this route delegates to it.
  // oxlint-disable-next-line no-restricted-imports -- Call-through spy proves the exact public route bypasses base while ordinary auth still recovers.
  const base = await import('../base')
  const baseRequest = vi.spyOn(base, 'request')
  const { consoleClient, consoleQuery } = await import('./index')
  return { consoleClient, consoleQuery, baseRequest, refresh }
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.doUnmock('@/utils/client')
  vi.doUnmock('@/config')
  vi.doUnmock('@langgenius/dify-ui/toast')
  vi.doUnmock('../refresh-token')
  vi.doUnmock('@dify/contracts/api/console/orpc.gen')
  focusManager.setFocused(undefined)
  onlineManager.setOnline(true)
})

describe('independent Casdoor result transport contract', () => {
  it('holds dispatch until the generated auth loader resolves, then runs owner and fetch in order', async () => {
    vi.stubGlobal('Request', NativeRequest)
    const delay = { entered: deferred(), release: deferred() }
    const events: string[] = []
    const raw = { ...resultBody, private_extra: 'synthetic-raw-sentinel' }
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const request = new Request(input, init)
      events.push(`fetch:${request.method}:${new URL(request.url).pathname}`)
      return jsonResponse(raw)
    })
    const { consoleClient, baseRequest } = await loadClient(delay)
    const owner = vi.fn(() => {
      events.push('owner')
      queueMicrotask(() => events.push('microtask'))
    })
    const pending = consoleClient.auth.casdoor.result.get(
      { query: { handoff } },
      { context: { beforeCasdoorResultRequest: owner } },
    )

    await delay.entered.promise
    expect(owner).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    delay.release.resolve()
    await expect(pending).resolves.toEqual(raw)

    expect(events).toEqual(['owner', 'fetch:GET:/console/api/auth/casdoor/result', 'microtask'])
    expect(fetch).toHaveBeenCalledOnce()
    expect(owner).toHaveBeenCalledOnce()
    expect(baseRequest).not.toHaveBeenCalled()
    const request = new Request(fetch.mock.calls[0]![0], fetch.mock.calls[0]![1])
    expect([...new URL(request.url).searchParams]).toEqual([['handoff', handoff]])
    expect(request.credentials).toBe('include')
    expect(request.cache).toBe('no-store')
    expect(request.redirect).toBe('error')
  })

  it.each(['absent', 'throws'] as const)(
    'does not dispatch or log when the result owner callback is %s',
    async (mode) => {
      const fetch = vi.spyOn(globalThis, 'fetch')
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { consoleClient, baseRequest, refresh } = await loadClient()
      const callback = vi.fn(() => {
        throw new Error('synthetic-owner-rejection')
      })
      await expect(
        consoleClient.auth.casdoor.result.get(
          { query: { handoff } },
          { context: mode === 'throws' ? { beforeCasdoorResultRequest: callback } : {} },
        ),
      ).rejects.toThrow()
      expect(fetch).not.toHaveBeenCalled()
      expect(callback).toHaveBeenCalledTimes(mode === 'throws' ? 1 : 0)
      expect(baseRequest).not.toHaveBeenCalled()
      expect(refresh).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
    },
  )

  it('returns display payload unchanged, including an empty object without synthesized defaults', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({}))
    const { consoleClient } = await loadClient()
    const owner = vi.fn()
    await expect(
      consoleClient.auth.casdoor.display.get(undefined, {
        context: { beforeCasdoorResultRequest: owner },
      }),
    ).resolves.toEqual({})
    expect(fetch).toHaveBeenCalledOnce()
    expect(owner).not.toHaveBeenCalled()
  })

  it.each([401, 403, 409, 503] as const)(
    'surfaces HTTP %i once without auth refresh, logging, or focus/reconnect replay',
    async (status) => {
      const fetch = vi
        .spyOn(globalThis, 'fetch')
        .mockResolvedValue(jsonResponse({ message: 'synthetic-failure' }, status))
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { consoleQuery, refresh, baseRequest } = await loadClient()
      const beforeRequest = vi.fn()
      const client = new QueryClient({ defaultOptions: { mutations: { retry: 4 } } })
      client.mount()
      const observer = new MutationObserver(
        client,
        consoleQuery.auth.casdoor.result.get.mutationOptions({
          retry: false,
          networkMode: 'always',
          gcTime: 0,
          context: { beforeCasdoorResultRequest: beforeRequest },
        }),
      )
      const unsubscribe = observer.subscribe(() => {})
      try {
        await expect(observer.mutate({ query: { handoff } })).rejects.toThrow()
        expect(observer.getCurrentResult().status).toBe('error')
        focusManager.setFocused(false)
        onlineManager.setOnline(false)
        focusManager.setFocused(true)
        onlineManager.setOnline(true)
        await client.resumePausedMutations()
        expect(fetch).toHaveBeenCalledOnce()
        expect(beforeRequest).toHaveBeenCalledOnce()
        expect(refresh).not.toHaveBeenCalled()
        expect(baseRequest).not.toHaveBeenCalled()
        expect(log).not.toHaveBeenCalled()
      } finally {
        unsubscribe()
        client.unmount()
        client.clear()
      }
    },
  )

  it('keeps ordinary auth recovery and HEAD display on the existing base transport', async () => {
    let attempts = 0
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const request = new Request(input, init)
      if (request.method === 'HEAD') return new Response(null, { status: 405 })
      attempts += 1
      return attempts === 1
        ? jsonResponse({ code: 'unauthorized' }, 401)
        : jsonResponse({ data: [] })
    })
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { consoleClient, baseRequest, refresh } = await loadClient()
    const owner = vi.fn()
    await expect(
      consoleClient.auth.plugin.datasource.list.get(undefined, {
        context: { beforeCasdoorResultRequest: owner },
      }),
    ).resolves.toEqual({ data: [] })
    await expect(consoleClient.auth.casdoor.display.head(undefined)).rejects.toBeDefined()
    expect(refresh).toHaveBeenCalledOnce()
    expect(baseRequest).toHaveBeenCalledTimes(2)
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(owner).not.toHaveBeenCalled()
    expect(log).toHaveBeenCalledOnce()
  })
})
