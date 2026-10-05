import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AccountDropdown from '@/app/components/header/account-dropdown'
import { createAccountProfileQueryClient } from '@/test/console/account-profile'
import { renderWithConsoleQuery } from '@/test/console/query-data'

const { transport, legacyPost, push, resetUser, events } = vi.hoisted(() => ({
  transport: vi.fn(),
  legacyPost: vi.fn(),
  push: vi.fn(),
  resetUser: vi.fn(),
  events: [] as string[],
}))
vi.mock('@/service/base', () => ({ request: transport, post: legacyPost, get: vi.fn() }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example/console/api' }))
vi.mock('@/env', () => ({ env: { NEXT_PUBLIC_API_PREFIX: 'https://console.example/console/api' } }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('@/next/navigation', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/next/navigation')>()),
  useRouter: () => ({ push }),
}))
vi.mock('@/app/components/base/amplitude/utils', () => ({ resetUser }))
vi.mock('next-themes', () => ({ useTheme: () => ({ theme: 'system', setTheme: vi.fn() }) }))
vi.mock('nuqs', async (importOriginal) => ({
  ...(await importOriginal<typeof import('nuqs')>()),
  useQueryState: () => [null, vi.fn()],
}))

const local = `/console/api/auth/casdoor/logout/${'A'.repeat(43)}`
let reply: unknown
let failed: boolean
function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  events.length = 0
  failed = false
  reply = {
    result: 'success',
    casdoor_logout: { status: 'handoff_ready', handoff: { handoff_path: local } },
  }
  resetUser.mockImplementation(() => events.push('reset'))
  push.mockImplementation(() => events.push('signin'))
  transport.mockImplementation(async (_url, _init, options) => {
    const path = new URL(options.request.url).pathname
    if (path.endsWith('/logout')) {
      expect(options.request.method).toBe('POST')
      events.push('local')
      return response(reply, failed ? 500 : 200)
    }
    if (path.endsWith('/auth/casdoor/session'))
      return response({ source: 'local_only', verified: false, rp_logout_available: false })
    return response({ code: 'unauthorized', message: 'Unavailable' }, 401)
  })
})

async function logout() {
  const client = createAccountProfileQueryClient({
    id: '11111111-1111-4111-8111-111111111111',
    name: 'Current User',
    email: 'current@example.test',
    avatar_url: '',
  })
  client.setQueryData(['previous-account-secret'], 'private')
  const clear = client.clear.bind(client)
  vi.spyOn(client, 'clear').mockImplementation(() => {
    events.push('cache')
    clear()
  })
  const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {
    expect(client.getQueryData(['previous-account-secret'])).toBeUndefined()
    events.push('handoff')
  })
  renderWithConsoleQuery(
    <AccountDropdown
      trigger={({ ariaLabel }) => (
        <button type="button" aria-label={ariaLabel}>
          Account
        </button>
      )}
    />,
    { queryClient: client, features: { education: { enabled: false } } },
  )
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'common.account.account' }))
  await user.click(await screen.findByText('common.userProfile.logout'))
  return { client, assign }
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Original logout owner with RP navigation', () => {
  it('clears the real QueryClient before the sole Header navigation to a local handoff', async () => {
    const { assign, client } = await logout()
    await waitFor(() =>
      expect(assign).toHaveBeenCalledExactlyOnceWith(`https://console.example${local}`),
    )
    expect(events).toEqual(['local', 'cache', 'reset', 'handoff'])
    expect(client.getQueryData(['previous-account-secret'])).toBeUndefined()
    expect(push).not.toHaveBeenCalled()
    expect(legacyPost).not.toHaveBeenCalled()
  })

  it.each([
    { result: 'success' },
    { result: 'success', casdoor_logout: { status: 'local_only', handoff: null } },
    {
      result: 'success',
      casdoor_logout: { status: 'local_only', handoff: { handoff_path: local } },
    },
    {
      result: 'success',
      casdoor_logout: {
        status: 'handoff_ready',
        handoff: { handoff_path: 'https://op.example/logout' },
      },
    },
    { result: 'success', casdoor_logout: { status: 'handoff_ready', handoff: null } },
  ])('preserves successful original local/other-provider sign-out for %j', async (value) => {
    reply = value
    const { assign, client } = await logout()
    await waitFor(() => expect(push).toHaveBeenCalledExactlyOnceWith('/signin'))
    expect(events).toEqual(['local', 'cache', 'reset', 'signin'])
    expect(client.getQueryData(['previous-account-secret'])).toBeUndefined()
    expect(assign).not.toHaveBeenCalled()
  })

  it('preserves local state and performs no navigation when the original logout fails', async () => {
    failed = true
    const { assign, client } = await logout()
    await waitFor(() => expect(transport).toHaveBeenCalled())
    expect(client.getQueryData(['previous-account-secret'])).toBe('private')
    expect(assign).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
    expect(resetUser).not.toHaveBeenCalled()
  })
})
