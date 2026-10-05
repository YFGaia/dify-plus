import type { CasdoorRevisionResponse } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RPLogoutDiagnostic } from '../diagnostic'

const { transport } = vi.hoisted(() => ({ transport: vi.fn() }))
vi.mock('@/service/base', () => ({ request: transport }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example/console/api' }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: translations } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(translations)
})
const revision: CasdoorRevisionResponse = {
  revision_id: '11111111-1111-4111-8111-111111111111',
  namespace_id: '22222222-2222-4222-8222-222222222222',
  secret_configured: true,
  configuration: {
    organization: 'org',
    application: 'app',
    client_id: 'client',
    default_workspace_id: '33333333-3333-4333-8333-333333333333',
    backend_api_url: 'https://idp.example',
    browser_frontend_url: 'https://idp.example',
    expected_issuer: 'https://idp.example',
    rp_logout: true,
  },
}
const handoff = `/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}`
const returned = 'The sign-out request returned. Other enterprise sessions may remain active.'
let summary: Record<string, unknown>
let startReply: unknown
beforeEach(() => {
  vi.clearAllMocks()
  summary = {
    revision_id: revision.revision_id,
    namespace_id: revision.namespace_id,
    profile_available: true,
    status: 'not_run',
  }
  startReply = { status: 'started', reason: null, handoff: { handoff_path: handoff } }
  transport.mockImplementation(async (_url, init, options) => {
    const url = new URL(options.request.url)
    expect(init.cache).toBe('no-store')
    expect(options.silent).toBe(true)
    if (url.pathname.endsWith('/rp-logout-status')) {
      expect(url.searchParams.get('revision_id')).toBe(revision.revision_id)
      return new Response(JSON.stringify(summary), {
        headers: { 'content-type': 'application/json' },
      })
    }
    expect(url.pathname.endsWith('/test-rp-logout')).toBe(true)
    expect(options.request.method).toBe('POST')
    expect(JSON.parse(await options.request.clone().text())).toEqual({
      revision_id: revision.revision_id,
      etag: 4,
    })
    return new Response(JSON.stringify(startReply), {
      headers: { 'content-type': 'application/json' },
    })
  })
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})
function mount() {
  const client = new QueryClient()
  render(
    <QueryClientProvider client={client}>
      <RPLogoutDiagnostic revision={revision} etag={4} />
    </QueryClientProvider>,
  )
  return client
}

it('requires explicit current-browser impact confirmation before testing the saved exact draft', async () => {
  const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
  mount()
  const button = screen.getByRole('button', { name: 'Test enterprise sign-out' })
  await waitFor(() => expect(transport).toHaveBeenCalledOnce())
  expect(button).toBeDisabled()
  const user = userEvent.setup()
  await user.click(screen.getByRole('checkbox', { name: /Your Dify session remains/ }))
  await user.click(button)
  await waitFor(() =>
    expect(assign).toHaveBeenCalledExactlyOnceWith(`https://console.example${handoff}`),
  )
})

it.each([{ revision_id: '44444444-4444-4444-8444-444444444444' }, { profile_available: false }])(
  'does not start an unavailable or mismatched draft: %j',
  async (override) => {
    summary = { ...summary, ...override }
    mount()
    await waitFor(() => expect(transport).toHaveBeenCalledOnce())
    await userEvent.setup().click(screen.getByRole('checkbox'))
    expect(screen.getByRole('button', { name: 'Test enterprise sign-out' })).toBeDisabled()
  },
)

it('expires a displayed observation at its nearest deadline and refreshes once without polling', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-05T00:00:00Z'))
  summary = {
    ...summary,
    status: 'passed',
    checked_at: '2026-10-04T23:59:59Z',
    expires_at: '2026-10-05T00:00:01Z',
  }
  mount()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(screen.getByText(returned)).toBeInTheDocument()
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000)
  })
  expect(screen.queryByText(returned)).not.toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('Expired')
  expect(transport).toHaveBeenCalledTimes(2)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(60_000)
  })
  expect(transport).toHaveBeenCalledTimes(2)
})

it('does not claim a passed observation without valid freshness fields', async () => {
  summary = { ...summary, status: 'passed', checked_at: null, expires_at: null }
  mount()
  await waitFor(() => expect(transport).toHaveBeenCalledOnce())
  expect(screen.queryByText(returned)).not.toBeInTheDocument()
})
