import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

async function loadRefreshOwner() {
  vi.resetModules()
  vi.doMock('@/config', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/config')>()),
    API_PREFIX: '/console/api',
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

describe('independent private self refresh privacy boundary', () => {
  it('keeps a cleanup failure private while ordinary account refresh keeps its original log', async () => {
    const { refreshAccessTokenOrReLogin } = await loadRefreshOwner()
    const refreshError = new Error('synthetic ordinary refresh fetch failure')
    const cleanupError = new Error('synthetic refresh cleanup failure')
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockImplementationOnce(() => {
        throw refreshError
      })
    vi.spyOn(localStorage, 'getItem').mockReturnValue(null)
    vi.spyOn(localStorage, 'removeItem').mockImplementationOnce(() => {
      throw cleanupError
    })

    const origin = window.location.origin
    const selfRequest = new Request(`${origin}/console/api/account/casdoor-identity`, {
      method: 'GET',
    })
    const ordinaryAccountRequest = new Request(`${origin}/console/api/account/profile`, {
      method: 'GET',
    })

    await expect(refreshAccessTokenOrReLogin(1000, selfRequest)).rejects.toBe(cleanupError)
    expect(log).not.toHaveBeenCalled()

    await expect(refreshAccessTokenOrReLogin(1000, ordinaryAccountRequest)).rejects.toBe(
      refreshError,
    )
    expect(log).toHaveBeenCalledExactlyOnceWith(refreshError)
    expect(globalThis.fetch).toHaveBeenCalledTimes(2)
    expect(localStorage.removeItem).toHaveBeenCalledTimes(3)
  })
})
