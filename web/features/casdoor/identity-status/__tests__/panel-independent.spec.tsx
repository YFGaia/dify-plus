import type { GetAccountCasdoorIdentityResponse } from '@dify/contracts/api/console/account/types.gen'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen } from '@testing-library/react'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import AccountPage from '@/app/account/(commonLayout)/account-page'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { systemFeaturesQueryOptions } from '@/features/system-features/client'
import { consoleQuery } from '@/service/console'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { createSystemFeaturesFixture } from '@/test/console/system-features'
import EnterpriseIdentityPanel from '..'

vi.mock('@/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config')>()),
  API_PREFIX: 'http://localhost:3000/console/api',
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: extend } = await import('@/i18n/en-US/extend.json')
  const { default: common } = await import('@/i18n/en-US/common.json')
  return createReactI18nextMock({ ...common, ...extend })
})
vi.mock('@/app/account/(commonLayout)/account-page/AvatarWithEdit', () => ({
  default: () => <div aria-hidden="true" />,
}))
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => '/account',
  useSearchParams: () => new URLSearchParams(),
}))

const accountA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const accountB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const workspace = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const privateError = 'private-profile-or-identity-error@example.test'
const identity = (organization: string): GetAccountCasdoorIdentityResponse => ({
  actions: {
    adopt: false,
    link: false,
    logout: false,
    reauthenticate: false,
    release: false,
    retry: false,
    unlink: false,
  },
  binding: 'linked',
  identity_has_more: false,
  identity_next: null,
  membership_has_more: false,
  membership_next: null,
  current_membership_has_more: false,
  current_membership_next: null,
  identities: [
    {
      id: accountA,
      namespace_id: null,
      organization,
      masked_identifier: '********',
      activity: 'active',
      lifecycle: 'active',
      profile_consistency: 'consistent',
      avatar_status: 'unknown',
      sync_generation: 1,
      name: {
        baseline_generation: null,
        recorded_generation: null,
        last_reason: null,
        last_status: null,
        last_sync_at: null,
        current_local_differs_from_last_applied: null,
      },
      email: {
        current_differs: null,
        last_differs: null,
        last_status: null,
        verified: null,
      },
    },
  ],
  memberships: [],
  current_memberships: [
    {
      id: null,
      workspace_id: workspace,
      local_role: null,
      state: 'unknown',
      join_presence: 'unknown',
      remote_actual_state: 'unknown',
    },
  ],
})
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((accept) => {
    resolve = accept
  })
  return { promise, resolve }
}

let client: QueryClient
let requests: Request[]
let respond: (request: Request) => Promise<Response>
let profileId: string
let profileName: string
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Request', NativeRequest)
  requests = []
  profileId = accountA
  profileName = 'Account A'
  respond = async () => json(identity('Initial organization'))
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  seedAccountProfileQuery(client, { id: accountA, name: profileName })
  client.setQueryData(systemFeaturesQueryOptions().queryKey, createSystemFeaturesFixture())
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init)
      requests.push(request)
      const path = new URL(request.url).pathname
      if (path.endsWith('/account/casdoor-identity')) return respond(request)
      if (path.endsWith('/account/profile'))
        return json({
          id: profileId,
          name: profileName,
          email: 'owner@example.test',
          avatar_url: null,
          avatar: '',
          is_password_set: false,
          timezone: 'UTC',
        })
      if (path.endsWith('/apps'))
        return json({ data: [], page: 1, limit: 100, total: 0, has_more: false })
      if (path.endsWith('/features')) return json({ education: { enabled: false } })
      throw new Error(`Unexpected synthetic route: ${path}`)
    }),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const mount = (account = false) =>
  render(
    <QueryClientProvider client={client}>
      {account ? <AccountPage /> : <EnterpriseIdentityPanel />}
    </QueryClientProvider>,
  )
const selfRequests = () =>
  requests.filter((request) => new URL(request.url).pathname.endsWith('/account/casdoor-identity'))
const selfKey = () => consoleQuery.account.casdoorIdentity.get.key()

describe('enterprise identity panel independent lifecycle', () => {
  it('hides successful self data after a failed refetch while retaining only the fixed error message', async () => {
    mount()
    expect(await screen.findByText('Initial organization')).toBeVisible()
    respond = async () => json({ message: privateError }, 503)
    await act(async () => {
      await client.invalidateQueries({ queryKey: selfKey() })
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enterprise identity information is unavailable.',
    )
    expect(screen.queryByText('Initial organization')).not.toBeInTheDocument()
    expect(screen.queryByText('Linked', { exact: true })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeEnabled()
    expect(screen.getByRole('region', { name: 'Enterprise identity' }).innerHTML).not.toContain(
      privateError,
    )
    expect(selfRequests()).toHaveLength(2)
    expect(selfRequests().every((request) => request.method === 'GET')).toBe(true)
  })

  it('rejects a delayed valid A response after the real Account profile switches to B', async () => {
    const lateA = deferred<Response>()
    mount(true)
    expect(await screen.findByText('Initial organization')).toBeVisible()
    respond = () => lateA.promise
    let pending!: Promise<void>
    await act(async () => {
      pending = client.invalidateQueries({ queryKey: selfKey() })
    })
    const beforeSwitch = selfRequests().length
    respond = async () => json(identity('B organization'))
    profileId = accountB
    profileName = 'Account B'
    await act(async () => {
      seedAccountProfileQuery(client, { id: accountB, name: profileName })
    })
    expect(await screen.findByText('B organization')).toBeVisible()
    expect(screen.queryByText('Initial organization')).not.toBeInTheDocument()
    expect(selfRequests()).toHaveLength(beforeSwitch + 1)
    await act(async () => {
      lateA.resolve(json(identity('Delayed valid A organization')))
      await pending
    })
    expect(screen.getByText('B organization')).toBeVisible()
    expect(screen.queryByText('Delayed valid A organization')).not.toBeInTheDocument()
    const cachedOrganizations = client
      .getQueriesData<GetAccountCasdoorIdentityResponse>({ queryKey: selfKey() })
      .flatMap(([, data]) => data?.identities.map((item) => item.organization) ?? [])
    expect(cachedOrganizations).toContain('B organization')
    expect(cachedOrganizations).not.toContain('Delayed valid A organization')
    expect(selfRequests().at(-1)?.url).toContain('limit=20')
    expect(new URL(selfRequests().at(-1)!.url).searchParams.has('identity_after')).toBe(false)
  })

  it('keeps self data hidden through profile error recovery and refetches after profile removal recovery', async () => {
    mount()
    expect(await screen.findByText('Initial organization')).toBeVisible()
    await act(async () => {
      await expect(
        client.fetchQuery({
          ...userProfileQueryOptions(),
          staleTime: 0,
          retry: false,
          queryFn: async () => {
            throw new Error(privateError)
          },
        }),
      ).rejects.toThrow(privateError)
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enterprise identity information is unavailable.',
    )
    expect(screen.queryByText('Initial organization')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
    expect(screen.getByRole('region', { name: 'Enterprise identity' }).innerHTML).not.toContain(
      privateError,
    )
    respond = async () => json(identity('Recovered organization'))
    await act(async () => {
      seedAccountProfileQuery(client, { id: accountA, name: profileName })
    })
    const errorRecoveryReusedOldIdentity = screen.queryByText('Initial organization') !== null
    const requestCountAfterErrorRecovery = selfRequests().length
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    await act(async () => {
      client.removeQueries({ queryKey: userProfileQueryOptions().queryKey })
    })
    expect(screen.queryByText('Initial organization')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
    await act(async () => {
      seedAccountProfileQuery(client, { id: accountA, name: profileName })
    })
    expect(await screen.findByText('Recovered organization')).toBeVisible()
    expect(screen.queryByText('Initial organization')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(selfRequests()).toHaveLength(requestCountAfterErrorRecovery + 1)
    expect(
      errorRecoveryReusedOldIdentity,
      `same-ID profile error recovery must fetch before showing the previously cached identity; self GET count was ${requestCountAfterErrorRecovery}`,
    ).toBe(false)
  })
})
