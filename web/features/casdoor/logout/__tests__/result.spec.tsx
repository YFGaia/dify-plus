import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { CasdoorLogoutResult } from '../result'

const { native, legacy, state } = vi.hoisted(() => ({
  native: vi.fn(),
  legacy: vi.fn(),
  state: { status: 'unavailable' },
}))
vi.mock('@/service/base', () => ({ request: legacy }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example/console/api' }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('@/next/navigation', () => ({
  useSearchParams: () => new URLSearchParams({ status: state.status }),
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: translations } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(translations)
})
const path = `/console/api/auth/casdoor/logout/${'A'.repeat(43)}`
let data: unknown
let requestOptions: RequestInit[]
beforeEach(() => {
  vi.clearAllMocks()
  state.status = 'unavailable'
  data = { status: 'handoff_ready', handoff: { handoff_path: path } }
  requestOptions = []
  const NativeRequest = globalThis.Request
  // happy-dom omits cache/redirect properties; verify the real transport's constructor options.
  vi.stubGlobal(
    'Request',
    class extends NativeRequest {
      constructor(input: RequestInfo | URL, init?: RequestInit) {
        super(input, init)
        requestOptions.push(init ?? {})
      }
    },
  )
  vi.stubGlobal('fetch', native)
  native.mockImplementation(async () => {
    return new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } })
  })
})
afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function mount() {
  const client = new QueryClient()
  return render(
    <QueryClientProvider client={client}>
      <CasdoorLogoutResult />
    </QueryClientProvider>,
  )
}

it('shows a fixed returned result with only an explicit sign-in link and no auth probe', () => {
  state.status = 'returned'
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('The sign-out request returned.')
  expect(screen.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/signin')
  expect(screen.queryByRole('button')).not.toBeInTheDocument()
  expect(native).not.toHaveBeenCalled()
  expect(legacy).not.toHaveBeenCalled()
})

it('continues only after the user clicks, with anonymous native transport and no refresh recovery', async () => {
  const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
  mount()
  expect(native).not.toHaveBeenCalled()
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry enterprise sign-out' }))
  await waitFor(() => expect(native).toHaveBeenCalledOnce())
  const request: Request = native.mock.calls[0]![0]
  expect(request.url).toBe('https://console.example/console/api/auth/casdoor/logout/retry')
  expect(request.method).toBe('POST')
  expect(request.credentials).toBe('include')
  expect(requestOptions).toContainEqual(
    expect.objectContaining({ cache: 'no-store', redirect: 'error', credentials: 'include' }),
  )
  await waitFor(() =>
    expect(assign).toHaveBeenCalledExactlyOnceWith(`https://console.example${path}`),
  )
  expect(legacy).not.toHaveBeenCalled()
})

it.each([
  { status: 'local_only', handoff: null },
  { status: 'handoff_ready', handoff: { handoff_path: 'https://op.example/logout?hint=private' } },
])(
  'keeps failed or unavailable continuation anonymous and does not navigate: %j',
  async (value) => {
    data = value
    const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    mount()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry enterprise sign-out' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('unavailable or has expired')
    expect(assign).not.toHaveBeenCalled()
    expect(legacy).not.toHaveBeenCalled()
  },
)
