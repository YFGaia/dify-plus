import type { GetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/types.gen'
import type { DeepPartial } from '@/test/console/system-features'
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { seedSystemFeatures } from '@/test/console/query-data'
import NormalForm from '../normal-form'

const boundary = vi.hoisted(() => ({
  params: new URLSearchParams(),
  profile: vi.fn(),
  replace: vi.fn(),
}))

vi.mock('@/next/navigation', () => ({
  useSearchParams: () => boundary.params,
  useRouter: () => ({ replace: boundary.replace, push: vi.fn() }),
}))

vi.mock('@/features/account-profile/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/account-profile/client')>()
  return {
    ...actual,
    userProfileQueryOptions: () => ({
      ...actual.userProfileQueryOptions(),
      queryFn: boundary.profile,
      retry: false,
    }),
  }
})

const display = {
  enabled: true,
  button_text: 'Company sign in',
  start_path: '/console/api/auth/casdoor/login',
}
const response = (raw: unknown) =>
  new Response(JSON.stringify(raw), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })
const clients: QueryClient[] = []
let fetchDisplay: ReturnType<typeof vi.fn<() => Promise<Response>>>

function mount(features: DeepPartial<GetSystemFeaturesResponse> = {}, client?: QueryClient) {
  const queryClient = client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(queryClient)
  seedSystemFeatures(queryClient, { enable_email_password_login: false, ...features })
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <NormalForm />
      </QueryClientProvider>,
    ),
    queryClient,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  boundary.params = new URLSearchParams()
  boundary.profile.mockRejectedValue(new Response(null, { status: 401 }))
  fetchDisplay = vi.fn().mockImplementation(async () => response(display))
  vi.stubGlobal('Request', NativeRequest)
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const request = new Request(input, init)
    if (!new URL(request.url).pathname.endsWith('/auth/casdoor/display'))
      throw new Error(`Unexpected request: ${request.url}`)
    return fetchDisplay()
  })
})

afterEach(() => {
  cleanup()
  focusManager.setFocused(undefined)
  for (const client of clients.splice(0)) client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('independent native sign-in regressions', () => {
  it('ignores an earlier observer response after unmount and a fresh disabled read', async () => {
    const first = Promise.withResolvers<Response>()
    const second = Promise.withResolvers<Response>()
    fetchDisplay.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)

    const original = mount()
    await waitFor(() => expect(fetchDisplay).toHaveBeenCalledTimes(1))
    original.unmount()

    mount({}, original.queryClient)
    await waitFor(() => expect(fetchDisplay).toHaveBeenCalledTimes(2))
    await act(async () => second.resolve(response({ ...display, enabled: false })))
    expect(await screen.findByText('login.noLoginMethod')).toBeInTheDocument()

    await act(async () => first.resolve(response(display)))
    await waitFor(() => {
      expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
      expect(screen.getByText('login.noLoginMethod')).toBeInTheDocument()
    })
    expect(boundary.replace).not.toHaveBeenCalled()
  })

  it('does not refetch after TanStack focus settles true', async () => {
    const { queryClient } = mount()
    await screen.findByRole('link', { name: display.button_text })
    expect(fetchDisplay).toHaveBeenCalledTimes(1)

    let resolveSettled!: () => void
    const focusSettled = new Promise<void>((resolve) => {
      resolveSettled = resolve
    })
    let unsubscribe = () => {}
    unsubscribe = focusManager.subscribe(async (focused) => {
      if (!focused) return
      unsubscribe()
      await queryClient.resumePausedMutations()
      resolveSettled()
    })
    await act(async () => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
      await focusSettled
    })

    expect(fetchDisplay).toHaveBeenCalledTimes(1)
  })
})
