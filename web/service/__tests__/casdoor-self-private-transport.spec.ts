import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

const sentinel = 'synthetic-private-raw-identity@example.test'
const selfPath = '/console/api/account/casdoor-identity'
const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
const unauthorized = () => jsonResponse({ code: 'unauthorized', message: sentinel }, 401)

async function loadOwners(prefix: string | (() => string) = '/console/api') {
  vi.resetModules()
  // Deployment configuration is synthetic; every transport/decoder/refresh owner stays real.
  vi.doMock('@/config', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/config')>()),
    get API_PREFIX() {
      return typeof prefix === 'function' ? prefix() : prefix
    },
  }))
  const client = await import('../console')
  // oxlint-disable-next-line no-restricted-imports -- Exercise the original private catch boundary and its error identity.
  const base = await import('../base')
  const refresh = await import('../refresh-token')
  const { toast: activeToast } = await import('@langgenius/dify-ui/toast')
  return {
    ...client,
    ...base,
    ...refresh,
    notification: vi.spyOn(activeToast, 'error').mockImplementation(() => ''),
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.stubGlobal('Request', NativeRequest)
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      values.set(key, value)
    }),
    removeItem: vi.fn((key: string) => {
      values.delete(key)
    }),
  })
  document.cookie = 'csrf_token=synthetic-csrf; path=/'
  document.cookie = '__Host-csrf_token=synthetic-csrf; path=/; secure'
})
afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.doUnmock('@/config')
  document.cookie = 'csrf_token=; Max-Age=0; path=/'
  document.cookie = '__Host-csrf_token=; Max-Age=0; path=/; secure'
})

describe('private Casdoor self original transport', () => {
  it.each([
    '/console/api',
    '/console/api/',
    'https://api.example.test/console/api',
    '/prefix/../console/api',
  ])('preserves original 401 refresh retry and private GET options at %s', async (prefix) => {
    const requests: Request[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const req = new Request(
        input instanceof Request ? input : new URL(String(input), window.location.origin),
        init,
      )
      requests.push(req)
      if (requests.length === 1) return unauthorized()
      return jsonResponse(requests.length === 2 ? {} : { raw_extra: sentinel })
    })
    const { consoleClient, notification } = await loadOwners(prefix)
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const controller = new AbortController()
    const query = {
      identity_after: '11111111-1111-4111-8111-111111111111',
      membership_after: '22222222-2222-4222-8222-222222222222',
      current_membership_after: '33333333-3333-4333-8333-333333333333',
      limit: 3,
    }
    await expect(
      consoleClient.account.casdoorIdentity.get(
        { query },
        { signal: controller.signal, context: { silent: false, keepalive: true } },
      ),
    ).resolves.toEqual({ raw_extra: sentinel })
    expect(requests.map((req) => req.method)).toEqual(['GET', 'POST', 'GET'])
    const expected = new URL(prefix, window.location.origin)
    for (const req of [requests[0]!, requests[2]!]) {
      expect(new URL(req.url).origin).toBe(expected.origin)
      expect(new URL(req.url).pathname).toBe(
        `${expected.pathname.replace(/\/$/, '')}/account/casdoor-identity`,
      )
      expect(Object.fromEntries(new URL(req.url).searchParams)).toEqual({ ...query, limit: '3' })
      expect(req.credentials).toBe('include')
      expect(req.cache).toBe('no-store')
      expect(req.keepalive).toBe(true)
      expect(req.redirect).toBe('manual')
      expect(req.headers.get('X-CSRF-Token')).toBe('synthetic-csrf')
      expect(req.body).toBeNull()
      expect(req.signal.aborted).toBe(false)
    }
    expect(requests[1]!.url).toBe(new URL(`${prefix}/refresh-token`, window.location.origin).href)
    expect(requests[1]!.credentials).toBe('include')
    expect(requests[1]!.headers.get('content-type')).toBe('application/json;utf-8')
    expect(requests[1]!.body).toBeNull()
    expect(localStorage.getItem('is_other_tab_refreshing')).toBeNull()
    expect(localStorage.getItem('last_refresh_time')).toBeNull()
    controller.abort()
    expect(requests[2]!.signal.aborted).toBe(true)
    expect(notification).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
  })

  it.each([
    'network',
    'sync-fetch',
    'http',
    'malformed-json',
    'sync-json',
    'refresh-storage',
    'refresh-sync-fetch',
  ] as const)('excludes raw logs and notifications for %s', async (fault) => {
    const error = new Error(sentinel)
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    let attempts = 0
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
      attempts++
      if (fault === 'network') return Promise.reject(error)
      if (fault === 'sync-fetch' || (fault === 'refresh-sync-fetch' && attempts === 2)) throw error
      if (fault === 'refresh-storage' || fault === 'refresh-sync-fetch' || fault === 'sync-json')
        return Promise.resolve(unauthorized())
      if (fault === 'http') return Promise.resolve(jsonResponse({ message: sentinel }, 503))
      return Promise.resolve(
        new Response(`invalid-json-${sentinel}`, {
          headers: { 'content-type': 'application/json' },
        }),
      )
    })
    const { consoleClient, notification } = await loadOwners()
    if (fault === 'refresh-storage')
      vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
        throw error
      })
    if (fault === 'sync-json')
      vi.spyOn(Response.prototype, 'json').mockImplementation(() => {
        throw error
      })
    await expect(
      consoleClient.account.casdoorIdentity.get({}, { context: { silent: false } }),
    ).rejects.toBeDefined()
    expect(fetch).toHaveBeenCalledTimes(fault === 'refresh-sync-fetch' ? 2 : 1)
    expect(log).not.toHaveBeenCalled()
    expect(notification).not.toHaveBeenCalled()
  })

  it.each(['https://foreign.example.test/console/api', '/wrong/api'])(
    'rejects changed configuration before private dispatch: %s',
    async (changed) => {
      let reads = 0
      const { consoleClient, notification } = await loadOwners(() =>
        ++reads === 1 ? '/console/api' : changed,
      )
      const fetch = vi.spyOn(globalThis, 'fetch')
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      await expect(consoleClient.account.casdoorIdentity.get({})).rejects.toThrow(
        'Invalid account identity request.',
      )
      expect(fetch).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
      expect(notification).not.toHaveBeenCalled()
    },
  )

  it('retains non-self network error identity and browser logging even with silent', async () => {
    const error = new Error(sentinel)
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(error)
    const { consoleClient, notification } = await loadOwners()
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    await expect(
      consoleClient.account.profile.get(undefined, { context: { silent: true } }),
    ).rejects.toBe(error)
    expect(log).toHaveBeenCalledExactlyOnceWith(error)
    expect(notification).not.toHaveBeenCalled()
  })

  it('retains non-self HTTP notification and default cache', async () => {
    const requests: Request[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      requests.push(new Request(input, init))
      return jsonResponse({ message: sentinel }, 503)
    })
    const { consoleClient, notification } = await loadOwners()
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    await expect(consoleClient.account.profile.get()).rejects.toBeDefined()
    expect(notification).toHaveBeenCalledExactlyOnceWith(sentinel)
    expect(log).toHaveBeenCalledTimes(1)
    expect(requests[0]!.cache).toBe('default')
  })
})

const mismatches = [
  'suffix',
  'lookalike',
  'foreign',
  'method',
  'url-mismatch',
  'request-mismatch',
  'init-method',
  'not-request',
  'not-compat',
  'public',
  'marketplace',
] as const

describe('private self original base catch scope', () => {
  it.each(['exact', ...mismatches] as const)(
    'preserves catch error identity with classification %s',
    async (variant) => {
      const { request } = await loadOwners()
      const error = new Error(sentinel)
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(unauthorized())
      vi.spyOn(Response.prototype, 'json').mockImplementation(() => {
        throw error
      })
      const exact = new URL(selfPath, window.location.origin).href
      const target =
        variant === 'suffix'
          ? `${exact}/extra`
          : variant === 'lookalike'
            ? `${exact}-other`
            : variant === 'foreign'
              ? `https://foreign.example.test${selfPath}`
              : exact
      const original = new Request(variant === 'request-mismatch' ? `${exact}/other` : target, {
        method: variant === 'method' ? 'POST' : 'GET',
      })
      const rejection = await request(
        variant === 'url-mismatch' ? `${exact}/other` : target,
        { method: variant === 'init-method' ? 'POST' : undefined },
        {
          fetchCompat: variant !== 'not-compat',
          request:
            variant === 'not-request' ? ({ url: exact, method: 'GET' } as Request) : original,
          silent: true,
          isPublicAPI: variant === 'public',
          isMarketplaceAPI: variant === 'marketplace',
        },
      ).catch((caught: unknown) => caught)
      if (variant !== 'not-request') expect(rejection).toBe(error)
      // Invalid Request reaches the fetcher's own validation; all other stimuli reach the original outer catch.
      if (variant === 'not-request') expect(log).not.toHaveBeenCalled()
      else if (variant === 'exact') expect(log).not.toHaveBeenCalled()
      else expect(log).toHaveBeenCalledExactlyOnceWith(error)
    },
  )
})

describe('original refresh independently classifies originating request', () => {
  it.each([
    'exact',
    'normalized-prefix',
    'suffix',
    'lookalike',
    'foreign',
    'method',
    'not-request',
    'absent',
  ] as const)('preserves raw rejection and lock release for %s', async (variant) => {
    const { refreshAccessTokenOrReLogin } = await loadOwners(
      variant === 'normalized-prefix' ? '/prefix/../console/api/' : '/console/api',
    )
    const error = new Error(sentinel)
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw error
    })
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const exact = new URL(selfPath, window.location.origin).href
    const target =
      variant === 'suffix'
        ? `${exact}/extra`
        : variant === 'lookalike'
          ? `${exact}-other`
          : variant === 'foreign'
            ? `https://foreign.example.test${selfPath}`
            : exact
    const original =
      variant === 'absent'
        ? undefined
        : variant === 'not-request'
          ? ({ url: exact, method: 'GET' } as Request)
          : new Request(target, { method: variant === 'method' ? 'POST' : 'GET' })
    await expect(refreshAccessTokenOrReLogin(100000, original)).rejects.toBe(error)
    if (variant === 'exact' || variant === 'normalized-prefix') expect(log).not.toHaveBeenCalled()
    else expect(log).toHaveBeenCalledExactlyOnceWith(error)
    expect(localStorage.removeItem).toHaveBeenCalledWith('is_other_tab_refreshing')
    expect(localStorage.removeItem).toHaveBeenCalledWith('last_refresh_time')
  })
})

describe('private refresh cleanup rejection ownership', () => {
  it('keeps sync, async and HTTP refresh failures handled when final cleanup also fails', async () => {
    for (const fault of ['sync', 'async', 'http'] as const) {
      let prefix = '/console/api'
      const { refreshAccessTokenOrReLogin } = await loadOwners(() => prefix)
      const original = new Request(new URL(selfPath, window.location.origin))
      const refreshError = new Error(`${sentinel}-${fault}`)
      const cleanupError = new Error(`${sentinel}-cleanup`)
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      vi.spyOn(localStorage, 'getItem').mockReturnValue(null)
      vi.spyOn(localStorage, 'removeItem').mockImplementation(() => {
        throw cleanupError
      })
      vi.spyOn(globalThis, 'fetch').mockImplementation(() => {
        // Classification must remain anchored to the originating invocation, including after I/O.
        if (fault !== 'sync') prefix = '/changed/api'
        if (fault === 'sync') throw refreshError
        if (fault === 'async') return Promise.reject(refreshError)
        return Promise.resolve(jsonResponse({ message: sentinel }, 401))
      })
      await expect(refreshAccessTokenOrReLogin(1000, original)).rejects.toBe(cleanupError)
      expect(log).not.toHaveBeenCalled()
      // Yield to the native event loop so the runner observes any orphan rejection.
      await vi.advanceTimersByTimeAsync(0)
      vi.restoreAllMocks()
      vi.clearAllTimers()
    }
  })

  it.each(['pending', 'settled'] as const)(
    'contains private timeout cleanup failure after %s refresh',
    async (state) => {
      const { refreshAccessTokenOrReLogin } = await loadOwners()
      const original = new Request(new URL(selfPath, window.location.origin))
      const cleanupError = new Error(`${sentinel}-timeout-cleanup`)
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const response = Promise.withResolvers<Response>()
      vi.spyOn(globalThis, 'fetch').mockReturnValue(response.promise)
      const pending = refreshAccessTokenOrReLogin(1000, original)
      const rejection = state === 'pending' ? expect(pending).rejects.toBe(cleanupError) : undefined
      if (state === 'settled') {
        response.resolve(jsonResponse({}))
        await expect(pending).resolves.toBeUndefined()
      }
      vi.spyOn(localStorage, 'removeItem').mockImplementation(() => {
        throw cleanupError
      })
      await expect(vi.advanceTimersByTimeAsync(1000)).resolves.toBeDefined()
      await rejection
      expect(log).not.toHaveBeenCalled()
      expect(localStorage.removeItem).toHaveBeenCalledWith('is_other_tab_refreshing')
    },
  )
})
