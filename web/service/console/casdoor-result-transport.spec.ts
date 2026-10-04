import { focusManager, MutationObserver, onlineManager, QueryClient } from '@tanstack/react-query'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

const handoff = 'A'.repeat(43)
const restrictedResult = {
  code: 'authorization_pending',
  correlation_id: '11111111-1111-4111-8111-111111111111',
  retry_allowed: false,
}
const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const deferred = () => Promise.withResolvers<void>()

async function loadClient(
  apiPrefix: string | (() => string) = '/console/api',
  delay?: { entered: ReturnType<typeof deferred>; release: ReturnType<typeof deferred> },
) {
  vi.resetModules()
  vi.doMock('@/config', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/config')>()),
    get API_PREFIX() {
      return typeof apiPrefix === 'function' ? apiPrefix() : apiPrefix
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
  // Keep the base and its ky/401 recovery implementation real; observe delegation only.
  // oxlint-disable-next-line no-restricted-imports -- This regression spies on the real legacy adapter to prove bypass and preserve ordinary 401 recovery.
  const base = await import('../base')
  const baseRequest = vi.spyOn(base, 'request')
  const client = await import('./index')
  return { ...client, baseRequest, refresh }
}

beforeEach(() => {
  vi.clearAllMocks()
})
afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.doUnmock('@/config')
  vi.doUnmock('@/utils/client')
  vi.doUnmock('@langgenius/dify-ui/toast')
  vi.doUnmock('../refresh-token')
  vi.doUnmock('@dify/contracts/api/console/orpc.gen')
  focusManager.setFocused(undefined)
  onlineManager.setOnline(true)
})

describe('native public Casdoor transport', () => {
  it.each([
    '/console/api',
    '/console/api/',
    'https://api.example.test/console/api',
    'https://api.example.test/console/api/',
  ])(
    'preserves generated GET serialization, raw payloads and dispatch options at %s',
    async (prefix) => {
      // Next bundles the native Undici Request: happy-dom omits cache and keepalive.
      vi.stubGlobal('Request', NativeRequest)
      const requests: Request[] = []
      const payload = { ...restrictedResult, private_extra: 'synthetic-raw-sentinel' }
      const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
        const request = new Request(input, init)
        requests.push(request)
        return jsonResponse(request.url.includes('/result?') ? payload : {})
      })
      const { consoleClient, baseRequest } = await loadClient(prefix)
      const beforeRequest = vi.fn()
      const controller = new AbortController()
      const signal = controller.signal
      await expect(
        consoleClient.auth.casdoor.result.get(
          { query: { handoff } },
          {
            signal,
            context: { beforeCasdoorResultRequest: beforeRequest, keepalive: true },
          },
        ),
      ).resolves.toEqual(payload)
      await expect(
        consoleClient.auth.casdoor.display.get(undefined, {
          context: { beforeCasdoorResultRequest: beforeRequest },
        }),
      ).resolves.toEqual({})
      expect(fetch).toHaveBeenCalledTimes(2)
      expect(beforeRequest).toHaveBeenCalledTimes(1)
      expect(baseRequest).not.toHaveBeenCalled()
      const baseURL = new URL(prefix, window.location.origin)
      for (const request of requests) {
        expect(request.method).toBe('GET')
        expect(request.body).toBeNull()
        expect(request.credentials).toBe('include')
        expect(request.redirect).toBe('error')
        expect(request.cache).toBe('no-store')
        expect(request.headers.has('X-Login-Config-Token')).toBe(false)
        expect(new URL(request.url).origin).toBe(baseURL.origin)
      }
      const resultURL = new URL(requests[0]!.url)
      expect(resultURL.pathname).toBe(`${baseURL.pathname.replace(/\/$/, '')}/auth/casdoor/result`)
      expect([...resultURL.searchParams]).toEqual([['handoff', handoff]])
      expect(new URL(requests[1]!.url).search).toBe('')
      expect(requests[0]!.keepalive).toBe(true)
      expect(requests[0]!.signal.aborted).toBe(false)
      controller.abort()
      expect(requests[0]!.signal.aborted).toBe(true)
    },
  )

  it.each(['https://other.example.test/console/api', '/different/api'])(
    'rejects a changed configured origin or path before the owner callback: %s',
    async (changedPrefix) => {
      let reads = 0
      const fetch = vi.spyOn(globalThis, 'fetch')
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { consoleClient } = await loadClient(() =>
        ++reads === 1 ? '/console/api' : changedPrefix,
      )
      const beforeRequest = vi.fn()
      await expect(
        consoleClient.auth.casdoor.result.get(
          { query: { handoff } },
          {
            context: { beforeCasdoorResultRequest: beforeRequest },
          },
        ),
      ).rejects.toThrow('Invalid public sign-in request.')
      expect(beforeRequest).not.toHaveBeenCalled()
      expect(fetch).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
    },
  )

  it('waits for the real auth loader, then calls the owner and fetch in one stack', async () => {
    const delay = { entered: deferred(), release: deferred() }
    const events: string[] = []
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
      events.push('fetch')
      return jsonResponse(restrictedResult)
    })
    const { consoleClient } = await loadClient('/console/api', delay)
    const beforeRequest = vi.fn(() => {
      events.push('owner')
      queueMicrotask(() => events.push('microtask'))
    })
    const pending = consoleClient.auth.casdoor.result.get(
      { query: { handoff } },
      {
        context: { beforeCasdoorResultRequest: beforeRequest },
      },
    )
    await delay.entered.promise
    expect(beforeRequest).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
    delay.release.resolve()
    await expect(pending).resolves.toEqual(restrictedResult)
    expect(events).toEqual(['owner', 'fetch', 'microtask'])
    expect(beforeRequest).toHaveBeenCalledTimes(1)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each(['missing', 'throws'] as const)(
    'rejects a %s owner callback before dispatch without logging',
    async (mode) => {
      const fetch = vi.spyOn(globalThis, 'fetch')
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { consoleClient, baseRequest } = await loadClient()
      const beforeRequest = vi.fn(() => {
        throw new Error('synthetic-handoff-error')
      })
      await expect(
        consoleClient.auth.casdoor.result.get(
          { query: { handoff } },
          {
            context: mode === 'throws' ? { beforeCasdoorResultRequest: beforeRequest } : {},
          },
        ),
      ).rejects.toThrow()
      expect(fetch).not.toHaveBeenCalled()
      expect(baseRequest).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
      expect(beforeRequest).toHaveBeenCalledTimes(mode === 'throws' ? 1 : 0)
    },
  )

  it.each(['inactive', 'changed-url'] as const)(
    'lets a delayed owner reject %s before dispatch',
    async (transition) => {
      // Transport callback simulation only: actual mounted UI acceptance belongs to its owner.
      const delay = { entered: deferred(), release: deferred() }
      const owner = { alive: true, handoff }
      const fetch = vi.spyOn(globalThis, 'fetch')
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { consoleClient } = await loadClient('/console/api', delay)
      const pending = consoleClient.auth.casdoor.result.get(
        { query: { handoff } },
        {
          context: {
            beforeCasdoorResultRequest: () => {
              if (!owner.alive || owner.handoff !== handoff) throw new Error('Unavailable.')
            },
          },
        },
      )
      const rejected = expect(pending).rejects.toThrow('Unavailable.')
      await delay.entered.promise
      if (transition === 'inactive') owner.alive = false
      else owner.handoff = 'B'.repeat(43)
      delay.release.resolve()
      await rejected
      expect(fetch).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
    },
  )

  it('allows overlapping delayed owners to remove a shared command only once', async () => {
    const delay = { entered: deferred(), release: deferred() }
    let commandPresent = true
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(restrictedResult))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { consoleClient } = await loadClient('/console/api', delay)
    const beforeRequest = () => {
      if (!commandPresent) throw new Error('Unavailable.')
      commandPresent = false
    }
    const calls = [1, 2].map(() =>
      consoleClient.auth.casdoor.result.get(
        { query: { handoff } },
        {
          context: { beforeCasdoorResultRequest: beforeRequest },
        },
      ),
    )
    const pending = Promise.allSettled(calls)
    await delay.entered.promise
    expect(fetch).not.toHaveBeenCalled()
    delay.release.resolve()
    const results = await pending
    expect(results.map((result) => result.status)).toEqual(['fulfilled', 'rejected'])
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  describe.each(['result', 'display'] as const)('%s faults', (operation) => {
    it.each([400, 401, 403, 409, 429, 503, 'network', 'lost-reply', 'redirect'] as const)(
      'dispatches %s once without base recovery, navigation or raw logging',
      async (fault) => {
        const log = vi.spyOn(console, 'error').mockImplementation(() => {})
        const reload = vi.spyOn(window.location, 'reload').mockImplementation(() => {})
        const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
        const replace = vi.spyOn(window.location, 'replace').mockImplementation(() => {})
        const originalURL = window.location.href
        const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
          if (fault === 'network' || fault === 'lost-reply')
            throw new TypeError(`synthetic-${fault}-${handoff}`)
          if (fault === 'redirect')
            return new Response(null, {
              status: 302,
              headers: { location: 'https://redirect.example.test/' },
            })
          return jsonResponse({ code: 'unauthorized', message: `synthetic-${handoff}` }, fault)
        })
        const { consoleClient, baseRequest, refresh } = await loadClient()
        const beforeRequest = vi.fn()
        const pending =
          operation === 'result'
            ? consoleClient.auth.casdoor.result.get(
                { query: { handoff } },
                { context: { beforeCasdoorResultRequest: beforeRequest } },
              )
            : consoleClient.auth.casdoor.display.get()
        await expect(pending).rejects.toThrow()
        expect(fetch).toHaveBeenCalledTimes(1)
        expect(beforeRequest).toHaveBeenCalledTimes(operation === 'result' ? 1 : 0)
        expect(baseRequest).not.toHaveBeenCalled()
        expect(refresh).not.toHaveBeenCalled()
        expect(reload).not.toHaveBeenCalled()
        expect(assign).not.toHaveBeenCalled()
        expect(replace).not.toHaveBeenCalled()
        expect(window.location.href).toBe(originalURL)
        expect(log).not.toHaveBeenCalled()
      },
    )
  })

  it('uses native mutationOptions with retry disabled and no focus/reconnect replay', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const fetch = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(jsonResponse({ message: 'synthetic-error' }, 503))
    const { consoleQuery } = await loadClient()
    const beforeRequest = vi.fn()
    const client = new QueryClient({ defaultOptions: { mutations: { retry: 3 } } })
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
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(beforeRequest).toHaveBeenCalledTimes(1)
    } finally {
      unsubscribe()
      client.unmount()
      client.clear()
    }
  })

  it('preserves other auth and HEAD delegation, keepalive, errors and ordinary 401 recovery', async () => {
    const requests: Request[] = []
    let ordinaryAttempts = 0
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const request = new Request(input, init)
      requests.push(request)
      if (request.method === 'HEAD') return new Response(null, { status: 405 })
      ordinaryAttempts++
      return ordinaryAttempts === 1
        ? jsonResponse({ code: 'unauthorized', message: 'synthetic-expiry' }, 401)
        : jsonResponse({ data: [] })
    })
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { consoleClient, baseRequest, refresh } = await loadClient()
    const beforeRequest = vi.fn(() => {
      throw new Error('Must not run.')
    })
    await expect(
      consoleClient.auth.plugin.datasource.list.get(undefined, {
        context: { keepalive: true, silent: true, beforeCasdoorResultRequest: beforeRequest },
      }),
    ).resolves.toEqual({ data: [] })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(baseRequest).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('/auth/plugin/datasource/list'),
      expect.objectContaining({ keepalive: true }),
      expect.objectContaining({ fetchCompat: true, silent: true }),
    )
    await expect(
      consoleClient.auth.casdoor.display.head(undefined, {
        context: { silent: true, beforeCasdoorResultRequest: beforeRequest },
      }),
    ).rejects.toBeDefined()
    expect(baseRequest).toHaveBeenCalledTimes(2)
    expect(fetch).toHaveBeenCalledTimes(3)
    expect(requests.map((request) => request.method)).toEqual(['GET', 'GET', 'HEAD'])
    expect(beforeRequest).not.toHaveBeenCalled()
    expect(log).toHaveBeenCalledTimes(1)
  })
})
