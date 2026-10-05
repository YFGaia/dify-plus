import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { useLogout } from '@/service/use-common'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { casdoorSessionQueryOptions } from '../client'
import { CasdoorSessionNotice } from '../index'

const { transport, logoutPost } = vi.hoisted(() => ({ transport: vi.fn(), logoutPost: vi.fn() }))
vi.mock('@/service/base', () => ({ request: transport, post: logoutPost }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example.test/console/api' }))
vi.mock('@/env', () => ({
  env: { NEXT_PUBLIC_API_PREFIX: 'https://console.example.test/console/api' },
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: translations } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(translations)
})
const account = '11111111-1111-4111-8111-111111111111'
const other = '22222222-2222-4222-8222-222222222222'
const sourceLabel = 'This session was created by Casdoor.'
const localLabel =
  'Signing out here ends your Dify session. Your enterprise session may remain active.'
let data: unknown
function response(value: unknown) {
  return new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
}
function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  seedAccountProfileQuery(client, { id: account })
  const view = render(
    <QueryClientProvider client={client}>
      <CasdoorSessionNotice />
    </QueryClientProvider>,
  )
  return { client, ...view }
}
beforeEach(() => {
  vi.clearAllMocks()
  data = {
    source: 'casdoor',
    verified: true,
    rp_logout_available: false,
    expires_at: new Date(Date.now() + 60_000).toISOString(),
  }
  transport.mockImplementation(async (_url, init, options) => {
    if (new URL(options.request.url).pathname === '/console/api/logout') {
      expect(options.request.method).toBe('POST')
      return response({ result: 'success' })
    }
    expect(new URL(options.request.url).pathname).toBe('/console/api/auth/casdoor/session')
    expect(options.request.method).toBe('GET')
    expect(init.cache).toBe('no-store')
    expect(options.silent).toBe(true)
    return response(data)
  })
  logoutPost.mockResolvedValue({ result: 'success' })
})
afterEach(() => {
  vi.useRealTimers()
})

describe('Casdoor session notice through native Console transport', () => {
  it('shows the verified current source and explains that enterprise logout remains unavailable', async () => {
    mount()
    await screen.findByText(sourceLabel)
    expect(screen.getByText(localLabel)).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })
  it.each([
    { source: 'local_only', verified: false, rp_logout_available: false, expires_at: null },
    { source: 'local_only', verified: true, rp_logout_available: false, expires_at: null },
    {
      source: 'local_only',
      verified: false,
      rp_logout_available: false,
      expires_at: '2030-01-01T00:00:00Z',
    },
    {
      source: 'casdoor',
      verified: false,
      rp_logout_available: false,
      expires_at: '2030-01-01T00:00:00Z',
    },
    {
      source: 'local_only',
      verified: false,
      rp_logout_available: true,
      expires_at: null,
    },
    { source: 'casdoor', verified: true, rp_logout_available: false, expires_at: null },
  ])(
    'never treats unavailable or contradictory data as a source/optional-capability proof: %j',
    async (value) => {
      data = value
      const { client } = mount()
      await waitFor(() => expect(client.isFetching()).toBe(0))
      expect(screen.queryByText(sourceLabel)).not.toBeInTheDocument()
      if (
        value.source !== 'local_only' ||
        value.verified ||
        value.expires_at !== null ||
        value.rp_logout_available
      )
        expect(client.getQueryData(casdoorSessionQueryOptions(client).queryKey)).toBeUndefined()
    },
  )
  it('explains the accepted optional enterprise attempt only on a fresh verified source', async () => {
    data = {
      source: 'casdoor',
      verified: true,
      rp_logout_available: true,
      expires_at: new Date(Date.now() + 60_000).toISOString(),
    }
    mount()
    await screen.findByText(sourceLabel)
    expect(
      screen.getByText(
        'Signing out here will also attempt to end your enterprise session in this browser.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText(localLabel)).not.toBeInTheDocument()
  })
  it('drops an in-flight source response after the profile owner changes', async () => {
    let deliver!: (response: Response) => void
    transport.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          deliver = resolve
        }),
    )
    const { client } = mount()
    const oldKey = casdoorSessionQueryOptions(client).queryKey
    await waitFor(() => expect(transport).toHaveBeenCalledOnce())
    transport.mockResolvedValue(
      response({
        source: 'local_only',
        verified: false,
        rp_logout_available: false,
        expires_at: null,
      }),
    )
    seedAccountProfileQuery(client, { id: other })
    deliver(response(data))
    await waitFor(() => expect(client.isFetching()).toBe(0))
    expect(client.getQueryData(oldKey)).toBeUndefined()
    expect(screen.queryByText(sourceLabel)).not.toBeInTheDocument()
  })
  it('hides already-resolved provenance when the current profile owner changes', async () => {
    const { client } = mount()
    await screen.findByText(sourceLabel)
    transport.mockImplementation(() => new Promise<Response>(() => {}))
    act(() => {
      seedAccountProfileQuery(client, { id: other })
    })
    await waitFor(() => expect(transport).toHaveBeenCalledTimes(2))
    expect(screen.queryByText(sourceLabel)).not.toBeInTheDocument()
  })
  it('hides expired provenance and performs one nearest-deadline refresh', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-05T00:00:00Z'))
    data = {
      source: 'casdoor',
      verified: true,
      rp_logout_available: false,
      expires_at: '2026-10-05T00:00:01Z',
    }
    const { client } = mount()
    await act(async () => {
      await client.fetchQuery(casdoorSessionQueryOptions(client))
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText(sourceLabel)).toBeInTheDocument()
    data = { source: 'local_only', verified: false, rp_logout_available: false, expires_at: null }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(screen.queryByText(sourceLabel)).not.toBeInTheDocument()
    expect(transport).toHaveBeenCalledTimes(2)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(transport).toHaveBeenCalledTimes(2)
  })
  it('keeps the original local logout request and removes user-scoped provenance/cache after success', async () => {
    const client = new QueryClient()
    seedAccountProfileQuery(client, { id: account })
    client.setQueryData(casdoorSessionQueryOptions(client).queryKey, data)
    function LocalLogout() {
      const logout = useLogout()
      return <button onClick={() => logout.mutate({})}>Sign out</button>
    }
    render(
      <QueryClientProvider client={client}>
        <LocalLogout />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await waitFor(() => expect(transport).toHaveBeenCalledOnce())
    expect(logoutPost).not.toHaveBeenCalled()
    await waitFor(() => expect(client.getQueryCache().getAll()).toHaveLength(0))
  })
})
