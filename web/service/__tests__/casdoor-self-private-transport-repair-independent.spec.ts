import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

async function loadRefreshOwner(getPrefix: () => string) {
  vi.resetModules()
  vi.doMock('@/config', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/config')>()),
    get API_PREFIX() {
      return getPrefix()
    },
  }))
  return import('../refresh-token')
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.useFakeTimers()
  vi.stubGlobal('Request', NativeRequest)
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: vi.fn((key: string) => values.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => values.set(key, value)),
    removeItem: vi.fn((key: string) => values.delete(key)),
  })
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.doUnmock('@/config')
})

describe('repaired private refresh failure ownership', () => {
  it('keeps the invocation private across async config drift and preserves ordinary logging', async () => {
    let prefix = '/console/api'
    const { refreshAccessTokenOrReLogin } = await loadRefreshOwner(() => prefix)
    const cleanupError = new Error('synthetic private cleanup failure')
    const privateRawError = new Error('synthetic private refresh failure')
    const refreshError = new Error('synthetic ordinary refresh failure')
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(localStorage, 'getItem').mockReturnValue(null)
    vi.spyOn(localStorage, 'removeItem').mockImplementationOnce(() => {
      throw cleanupError
    })
    vi.spyOn(globalThis, 'fetch')
      .mockImplementationOnce(() => {
        prefix = '/changed/api'
        return Promise.reject(privateRawError)
      })
      .mockImplementationOnce(() => {
        throw refreshError
      })

    const origin = window.location.origin
    const privateRequest = new NativeRequest(`${origin}/console/api/account/casdoor-identity`, {
      method: 'GET',
    })
    const ordinaryRequest = new NativeRequest(`${origin}/console/api/account/profile`, {
      method: 'GET',
    })

    await expect(refreshAccessTokenOrReLogin(1000, privateRequest)).rejects.toBe(cleanupError)
    expect(log).not.toHaveBeenCalled()

    await expect(refreshAccessTokenOrReLogin(1000, ordinaryRequest)).rejects.toBe(refreshError)
    expect(log).toHaveBeenCalledExactlyOnceWith(refreshError)

    // The original timeout remains scheduled and still performs cleanup after both races settle.
    const cleanupCallsBeforeTimeout = vi.mocked(localStorage.removeItem).mock.calls.length
    await vi.advanceTimersByTimeAsync(1000)
    expect(vi.mocked(localStorage.removeItem).mock.calls.length).toBeGreaterThan(
      cleanupCallsBeforeTimeout,
    )
    expect(log).toHaveBeenCalledExactlyOnceWith(refreshError)
    // Allow Vitest's native unhandled-rejection observer to see any orphan rejection.
    await vi.advanceTimersByTimeAsync(0)
  })
})
