import type { GetAccountCasdoorIdentityResponse } from '@dify/contracts/api/console/account/types.gen'
import { toast } from '@langgenius/dify-ui/toast'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
const accountB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const workspace = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const cursors = [
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  '33333333-3333-4333-8333-333333333333',
] as const
const sentinel = 'private-raw-error@example.test'
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
const paged = (): GetAccountCasdoorIdentityResponse => ({
  ...fixture(),
  identity_has_more: true,
  identity_next: cursors[0],
  membership_has_more: true,
  membership_next: cursors[1],
  current_membership_has_more: true,
  current_membership_next: cursors[2],
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
let selfResponse: (request: Request) => Promise<Response>
let profileName: string
let profileId: string
let success: ReturnType<typeof vi.spyOn>
let error: ReturnType<typeof vi.spyOn>
let log: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Request', NativeRequest)
  avatar.onSave = undefined
  requests = []
  profileName = 'Original name'
  profileId = accountA
  selfResponse = async () => json(fixture())
  success = vi.spyOn(toast, 'success').mockImplementation(() => '')
  error = vi.spyOn(toast, 'error').mockImplementation(() => '')
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
const regionNames = [
  'Identity records',
  'Recorded workspace access',
  'Current local workspace access',
] as const
const selfRequests = () =>
  requests.filter((request) => new URL(request.url).pathname.endsWith('/account/casdoor-identity'))
const lastQuery = () => Object.fromEntries(new URL(selfRequests().at(-1)!.url).searchParams)
async function loaded() {
  await screen.findByText('Linked', { exact: true })
}

// Finite UI boundary cases: schema-field completeness belongs to the accepted client suite.
describe('enterprise identity passive Account panel', () => {
  it('separates historical and current local access, preserves unknowns, and exposes no enabled actions or internal codes', async () => {
    mount()
    await loaded()
    expect(panel()).toHaveAccessibleDescription(
      'Linking and unlinking are unavailable here. Contact your administrator.',
    )
    const identity = within(screen.getByRole('region', { name: regionNames[0] }))
    expect(identity.getByText('Inactive', { exact: true })).toBeVisible()
    expect(identity.getByText('Archived', { exact: true })).toBeVisible()
    expect(identity.getByText('********', { exact: true })).toBeVisible()
    expect(identity.getAllByText('Unknown', { exact: true })).toHaveLength(3)
    expect(identity.getAllByText('Yes', { exact: true })).toHaveLength(2)
    expect(identity.getByText('No', { exact: true })).toBeVisible()
    expect(
      within(screen.getByRole('region', { name: regionNames[1] })).getByText(
        'Controlled withdrawal',
      ),
    ).toBeVisible()
    expect(
      within(screen.getByRole('region', { name: regionNames[1] })).getByText('Management released'),
    ).toBeVisible()
    expect(
      within(screen.getByRole('region', { name: regionNames[2] })).getByText('Owner'),
    ).toBeVisible()
    expect(
      within(screen.getByRole('region', { name: regionNames[2] })).getByText('Historical record'),
    ).toBeVisible()
    expect(within(panel()).getAllByText(workspace)).toHaveLength(2)
    for (const button of within(panel()).getAllByRole('button')) expect(button).toBeDisabled()
    expect(within(panel()).queryByRole('link')).not.toBeInTheDocument()
    expect(panel().innerHTML).not.toMatch(
      /local_override|history_present|controlled_withdrawal|baseline_generation|recorded_source|remote_actual_state|aaaaaaaa/,
    )
    expect(selfRequests()).toHaveLength(1)
    expect(selfRequests()[0]).toMatchObject({
      method: 'GET',
      cache: 'no-store',
      credentials: 'include',
    })
    expect(lastQuery()).toEqual({ limit: '20' })
    expect(error).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
  })

  it('renders unlinked empty pages without claiming a linked identity', async () => {
    selfResponse = async () =>
      json({
        ...fixture(),
        binding: 'unlinked',
        identities: [],
        memberships: [],
        current_memberships: [],
      })
    mount()
    expect(await screen.findByText('Not linked', { exact: true })).toBeVisible()
    expect(screen.queryByText('Linked', { exact: true })).not.toBeInTheDocument()
    expect(within(panel()).getAllByText('No records on this page.')).toHaveLength(3)
  })

  it('keeps loading and malformed failure private and provides only a fixed self GET read retry', async () => {
    const pending = deferred<Response>()
    selfResponse = () => pending.promise
    mount()
    expect(screen.getByRole('status')).toHaveTextContent('Loading')
    expect(screen.queryByText('Linked', { exact: true })).not.toBeInTheDocument()
    await act(async () => {
      pending.resolve(json({ ...fixture(), private_error: sentinel }))
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enterprise identity information is unavailable.',
    )
    expect(panel().innerHTML).not.toContain(sentinel)
    expect(screen.queryByText('Example organization')).not.toBeInTheDocument()
    const user = userEvent.setup()
    await user.tab()
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveFocus()
    selfResponse = async () => json(fixture())
    await user.keyboard('{Enter}')
    await loaded()
    expect(selfRequests()).toHaveLength(2)
    for (const request of selfRequests()) expect(request.method).toBe('GET')
    expect(error).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()
  })

  it('blocks missing profile fetches and removes displayed identity when the anchor becomes absent', async () => {
    client.removeQueries({ queryKey: userProfileQueryOptions().queryKey })
    mount()
    expect(screen.getByRole('alert')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeDisabled()
    expect(selfRequests()).toHaveLength(0)
    await act(async () => {
      seedAccountProfileQuery(client, { id: accountA })
    })
    await loaded()
    await act(async () => {
      client.removeQueries({ queryKey: userProfileQueryOptions().queryKey })
    })
    expect(screen.getByRole('alert')).toBeVisible()
    expect(screen.queryByText('Example organization')).not.toBeInTheDocument()
    expect(screen.queryByText('Linked', { exact: true })).not.toBeInTheDocument()
    expect(selfRequests()).toHaveLength(1)
  })

  it('changes each canonical next and first-page cursor independently with semantic keyboard order', async () => {
    selfResponse = async () => json(paged())
    mount()
    await loaded()
    const user = userEvent.setup()
    await user.tab()
    expect(
      within(screen.getByRole('region', { name: regionNames[0] })).getByRole('button', {
        name: 'Next page',
      }),
    ).toHaveFocus()
    await user.tab()
    expect(
      within(screen.getByRole('region', { name: regionNames[1] })).getByRole('button', {
        name: 'Next page',
      }),
    ).toHaveFocus()
    const expected: Record<string, string> = { limit: '20' }
    const keys = ['identity_after', 'membership_after', 'current_membership_after'] as const
    for (const [index, key] of keys.entries()) {
      await user.click(
        within(screen.getByRole('region', { name: regionNames[index] })).getByRole('button', {
          name: 'Next page',
        }),
      )
      await loaded()
      expected[key] = cursors[index]!
      expect(lastQuery()).toEqual(expected)
    }
    for (const [index, key] of keys.entries()) {
      await user.click(
        within(screen.getByRole('region', { name: regionNames[index] })).getByRole('button', {
          name: 'First page',
        }),
      )
      await loaded()
      delete expected[key]
      expect(lastQuery()).toEqual(expected)
    }
    expect(selfRequests()).toHaveLength(7)
  })

  it('uses actual Account profile changes to reset all cursors and reject late malformed A data after B is visible', async () => {
    const late = deferred<Response>()
    selfResponse = async () => json(paged())
    mount(true)
    await loaded()
    const user = userEvent.setup()
    for (const name of regionNames) {
      await user.click(
        within(screen.getByRole('region', { name })).getByRole('button', { name: 'Next page' }),
      )
      await loaded()
    }
    expect(lastQuery()).toEqual({
      limit: '20',
      identity_after: cursors[0],
      membership_after: cursors[1],
      current_membership_after: cursors[2],
    })
    selfResponse = () => late.promise
    let pending!: Promise<void>
    await act(async () => {
      pending = client.invalidateQueries({
        queryKey: consoleQuery.account.casdoorIdentity.get.key(),
      })
    })
    const beforeSwitch = selfRequests().length
    selfResponse = async () =>
      json({
        ...fixture(),
        identities: [{ ...fixture().identities[0]!, organization: 'B organization' }],
      })
    profileId = accountB
    await act(async () => {
      seedAccountProfileQuery(client, { id: accountB, name: 'Account B' })
    })
    expect(await screen.findByText('B organization')).toBeVisible()
    expect(lastQuery()).toEqual({ limit: '20' })
    expect(selfRequests()).toHaveLength(beforeSwitch + 1)
    expect(screen.queryByText('Example organization')).not.toBeInTheDocument()
    await act(async () => {
      late.resolve(json({ ...fixture(), private_error: sentinel }))
      await pending
    })
    expect(screen.getByText('B organization')).toBeVisible()
    expect(panel().innerHTML).not.toContain(sentinel)
    for (const name of regionNames)
      expect(
        within(screen.getByRole('region', { name })).getByRole('button', { name: 'First page' }),
      ).toBeDisabled()
  })

  it.each(['success', 'synchronous failure', 'rejected promise'] as const)(
    'preserves actual Account name success and avatar callback profile Promise with optional self invalidation %s',
    async (mode) => {
      mount(true)
      await loaded()
      const profileKey = userProfileQueryOptions().queryKey
      const selfKey = consoleQuery.account.casdoorIdentity.get.key()
      const original = client.invalidateQueries.bind(client)
      const profilePromises: Promise<void>[] = []
      const invalidations = vi
        .spyOn(client, 'invalidateQueries')
        .mockImplementation((filters, options) => {
          if (JSON.stringify(filters?.queryKey) === JSON.stringify(selfKey)) {
            if (mode === 'synchronous failure') throw new Error(sentinel)
            if (mode === 'rejected promise') return Promise.reject(new Error(sentinel))
          }
          const result = original(filters, options)
          if (JSON.stringify(filters?.queryKey) === JSON.stringify(profileKey))
            profilePromises.push(result)
          return result
        })
      const user = userEvent.setup()
      await user.click(screen.getByRole('button', { name: 'Edit' }))
      const dialog = screen.getByRole('dialog')
      await user.clear(within(dialog).getByRole('textbox', { name: 'Name' }))
      await user.type(within(dialog).getByRole('textbox', { name: 'Name' }), 'Updated name')
      await user.click(within(dialog).getByRole('button', { name: 'Save' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(success).toHaveBeenCalledTimes(1)
      expect(error).not.toHaveBeenCalled()
      expect(invalidations.mock.calls.slice(0, 2).map(([filter]) => filter?.queryKey)).toEqual([
        profileKey,
        selfKey,
      ])
      let returned: unknown
      await act(async () => {
        returned = avatar.onSave?.()
      })
      expect(returned).toBe(profilePromises[1])
      await act(async () => {
        await returned
      })
      expect(invalidations.mock.calls.slice(2, 4).map(([filter]) => filter?.queryKey)).toEqual([
        profileKey,
        selfKey,
      ])
      expect(
        requests.find((request) => new URL(request.url).pathname.endsWith('/account/name'))?.method,
      ).toBe('POST')
      if (mode === 'success') await waitFor(() => expect(selfRequests()).toHaveLength(3))
      expect(error).not.toHaveBeenCalled()
      expect(panel().innerHTML).not.toContain(sentinel)
    },
  )
})
