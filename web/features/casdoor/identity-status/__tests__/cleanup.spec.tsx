import type { GetAccountCasdoorIdentityResponse } from '@dify/contracts/api/console/account/types.gen'
import { toast } from '@langgenius/dify-ui/toast'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import AccountPage from '@/app/account/(commonLayout)/account-page'
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
const avatar = vi.hoisted(() => ({ onSave: undefined as (() => unknown) | undefined }))
vi.mock('@/app/account/(commonLayout)/account-page/AvatarWithEdit', () => ({
  default: ({ onSave }: { onSave?: () => unknown }) => {
    avatar.onSave = onSave
    return (
      <button
        type="button"
        onClick={() => {
          onSave?.()
        }}
      >
        Save avatar
      </button>
    )
  },
}))
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => '/account',
  useSearchParams: () => new URLSearchParams(),
}))

const accountA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const workspace = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const fixture = (): GetAccountCasdoorIdentityResponse => ({
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
      organization: 'Example organization',
      masked_identifier: '********',
      activity: 'inactive',
      lifecycle: 'archived',
      profile_consistency: 'historical',
      avatar_status: 'unknown',
      avatar_recorded_at: null,
      avatar_last_reason: null,
      avatar_recorded_generation: null,
      avatar_consistency: 'unknown',
      avatar_current_local_differs_from_last_applied: null,
      sync_generation: 42,
      name: {
        baseline_generation: 1,
        recorded_generation: 2,
        last_reason: 'local_override',
        last_status: 'local_override',
        last_sync_at: null,
        current_local_differs_from_last_applied: true,
      },
      email: {
        current_differs: true,
        last_differs: false,
        last_status: 'different',
        verified: null,
      },
    },
  ],
  memberships: [
    {
      id: null,
      identity_id: accountA,
      namespace_id: null,
      workspace_id: workspace,
      recorded_source: 'mapping',
      recorded_finalization: 'finalized',
      recorded_ownership: 'released',
      state: 'controlled_withdrawal',
      consistency: 'historical',
      tombstone: false,
      join_presence: 'absent',
      local_role: null,
      remote_actual_state: 'unknown',
    },
  ],
  current_memberships: [
    {
      id: null,
      workspace_id: workspace,
      local_role: 'owner',
      state: 'history_present',
      join_presence: 'present',
      remote_actual_state: 'unknown',
    },
  ],
})
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
let client: QueryClient
let requests: Request[]
let selfResponse: (request: Request) => Promise<Response>
let profileName: string
let profileId: string
let log: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Request', NativeRequest)
  avatar.onSave = undefined
  requests = []
  profileName = 'Original name'
  profileId = accountA
  selfResponse = async () => json(fixture())
  vi.spyOn(toast, 'success').mockImplementation(() => '')
  vi.spyOn(toast, 'error').mockImplementation(() => '')
  log = vi.spyOn(console, 'error').mockImplementation(() => {})
  client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })
  seedAccountProfileQuery(client, { id: accountA, name: profileName })
  client.setQueryData(systemFeaturesQueryOptions().queryKey, createSystemFeaturesFixture())
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init)
      requests.push(request)
      const path = new URL(request.url).pathname
      if (path.endsWith('/account/casdoor-identity')) return selfResponse(request)
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
      if (path.endsWith('/account/name')) {
        profileName = (await request.json()).name
        return json({ result: 'success' })
      }
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
const panel = () => screen.getByRole('region', { name: 'Enterprise identity' })
async function loaded() {
  await screen.findByText('Linked', { exact: true })
}

describe('completed cleanup on the actual AccountPage', () => {
  it('labels the failed synchronization and cleaned reservation without attachment success or retry promises', async () => {
    const data = fixture()
    Object.assign(data.identities[0]!, {
      avatar_status: 'failed_storage_cleaned',
      avatar_recorded_at: '2026-10-05T04:00:00.000000+00:00',
      avatar_last_reason: 'attachment_lost',
      avatar_recorded_generation: 1,
      avatar_consistency: 'current',
    })
    selfResponse = async () => json(data)
    mount(true)
    await loaded()
    const region = within(panel())
    expect(region.getByText('Avatar operation record').nextElementSibling).toHaveTextContent(
      'This avatar synchronization failed; the reserved file was cleaned up.',
    )
    expect(region.getByText('Last avatar operation record').nextElementSibling).toHaveTextContent(
      '2026-10-05T04:00:00.000000+00:00',
    )
    expect(region.getByText('Local avatar attachment could not be confirmed')).toBeVisible()
    expect(
      region.queryByText('Local attachment recorded; storage not confirmed'),
    ).not.toBeInTheDocument()
    expect(
      region.getByText('Local avatar differs from last attachment').nextElementSibling,
    ).toHaveTextContent('Unknown')
    expect(region.queryByRole('button', { name: /retry|resend/i })).not.toBeInTheDocument()
    expect(requests.every((request) => request.method === 'GET')).toBe(true)
    expect(log).not.toHaveBeenCalled()
  })
  it('keeps a pending unknown read passive and rejects an unregistered cleanup success', async () => {
    mount(true)
    await loaded()
    expect(
      within(panel()).queryByText(
        'This avatar synchronization failed; the reserved file was cleaned up.',
      ),
    ).not.toBeInTheDocument()
    selfResponse = async () =>
      json({
        ...fixture(),
        identities: [{ ...fixture().identities[0]!, avatar_status: 'cleanup_synced' }],
      })
    await act(async () => {
      await client.invalidateQueries({ queryKey: consoleQuery.account.casdoorIdentity.get.key() })
    })
    expect(await screen.findByRole('alert')).toBeVisible()
    expect(within(panel()).queryByText('Example organization')).not.toBeInTheDocument()
    expect(requests.every((request) => request.method === 'GET')).toBe(true)
    expect(log).not.toHaveBeenCalled()
  })
})
