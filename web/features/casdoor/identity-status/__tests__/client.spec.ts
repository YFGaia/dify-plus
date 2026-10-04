import type {
  GetAccountCasdoorIdentityData,
  GetAccountCasdoorIdentityResponse,
} from '@dify/contracts/api/console/account/types.gen'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { userProfileQueryOptions } from '@/features/account-profile/client'
import { consoleQuery } from '@/service/console'
import { useLogout } from '@/service/use-common'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { casdoorIdentityQueryOptions } from '../client'

vi.mock('@/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config')>()),
  API_PREFIX: 'http://localhost:3000/console/api',
}))
const id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const otherId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const sentinel = 'synthetic-raw-private@example.test'
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
  current_membership_has_more: false,
  current_membership_next: null,
  identity_has_more: false,
  identity_next: null,
  membership_has_more: false,
  membership_next: null,
  identities: [
    {
      activity: 'inactive',
      avatar_status: 'unknown',
      email: { current_differs: null, last_differs: null, last_status: null, verified: null },
      id,
      lifecycle: 'archived',
      masked_identifier: '********',
      name: {
        baseline_generation: null,
        current_local_differs_from_last_applied: null,
        last_reason: null,
        last_status: null,
        last_sync_at: null,
        recorded_generation: null,
      },
      namespace_id: null,
      organization: null,
      profile_consistency: 'unknown',
      sync_generation: null,
    },
  ],
  memberships: [
    {
      consistency: 'unknown',
      id: null,
      identity_id: null,
      join_presence: 'unknown',
      local_role: null,
      namespace_id: null,
      recorded_finalization: null,
      recorded_ownership: null,
      recorded_source: null,
      remote_actual_state: 'unknown',
      state: 'unknown',
      tombstone: null,
      workspace_id: null,
    },
  ],
  current_memberships: [
    {
      id: null,
      join_presence: 'unknown',
      local_role: null,
      remote_actual_state: 'unknown',
      state: 'unknown',
      workspace_id: null,
    },
  ],
})
const json = (body: unknown) =>
  new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })
const objectPaths = [
  '',
  'actions',
  'identities.0',
  'identities.0.email',
  'identities.0.name',
  'memberships.0',
  'current_memberships.0',
]
function objectAt(data: unknown, path: string): Record<string, unknown> {
  return path
    .split('.')
    .filter(Boolean)
    .reduce<unknown>((value, key) => (value as Record<string, unknown>)[key], data) as Record<
    string,
    unknown
  >
}
const fieldPaths = objectPaths.flatMap((path) =>
  Object.keys(objectAt(fixture(), path)).map((key) => ({ path, key })),
)
const idPaths = [
  ['', 'identity_next'],
  ['', 'membership_next'],
  ['', 'current_membership_next'],
  ['identities.0', 'id'],
  ['identities.0', 'namespace_id'],
  ['memberships.0', 'id'],
  ['memberships.0', 'identity_id'],
  ['memberships.0', 'namespace_id'],
  ['memberships.0', 'workspace_id'],
  ['current_memberships.0', 'id'],
  ['current_memberships.0', 'workspace_id'],
] as const
let client: QueryClient
let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>
beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Request', NativeRequest)
  fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => json(fixture()))
  vi.stubGlobal('fetch', fetchMock)
  client = new QueryClient()
  seedAccountProfileQuery(client, { id })
})
afterEach(() => {
  client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
async function rejectsPayload(data: unknown) {
  fetchMock.mockResolvedValue(json(data))
  const options = casdoorIdentityQueryOptions(client)
  await expect(client.fetchQuery(options)).rejects.toThrow('casdoor_self_invalid_response')
  expect(client.getQueryData(options.queryKey)).toBeUndefined()
  expect(client.getQueryState(options.queryKey)?.status).toBe('error')
  expect(fetchMock).toHaveBeenCalledTimes(1)
}

describe('authenticated self strict before cache', () => {
  it('caches the generated nullable response through original private transport with zero freshness and silent defaults', async () => {
    const options = casdoorIdentityQueryOptions(client)
    expect(options).toMatchObject({ retry: false, staleTime: 0, gcTime: 0, enabled: true })
    expect(options).not.toHaveProperty('initialData')
    expect(options).not.toHaveProperty('placeholderData')
    await expect(client.fetchQuery(options)).resolves.toEqual(fixture())
    expect(client.getQueryData(options.queryKey)).toEqual(fixture())
    await client.fetchQuery(options)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    for (const [input, init] of fetchMock.mock.calls) {
      const request = new Request(input, init)
      expect(request.cache).toBe('no-store')
      expect(request.credentials).toBe('include')
      expect(Object.fromEntries(new URL(request.url).searchParams)).toEqual({ limit: '20' })
      expect(request.body).toBeNull()
    }
  })
  it('caches an unlinked empty result without inventing presence', async () => {
    const data = {
      ...fixture(),
      binding: 'unlinked' as const,
      identities: [],
      memberships: [],
      current_memberships: [],
    }
    fetchMock.mockResolvedValue(json(data))
    await expect(client.fetchQuery(casdoorIdentityQueryOptions(client))).resolves.toEqual(data)
  })
  it('preserves canonical nullable IDs and unrestricted generated generations and timestamps', async () => {
    const data = fixture()
    for (const [path, key] of idPaths) objectAt(data, path)[key] = id
    Object.assign(data.identities[0]!.name, {
      last_sync_at: 'historical text',
      baseline_generation: -1,
      recorded_generation: 0,
    })
    data.identities[0]!.sync_generation = -2
    data.identities[0]!.organization = 'x'.repeat(255)
    fetchMock.mockResolvedValue(json(data))
    await expect(client.fetchQuery(casdoorIdentityQueryOptions(client))).resolves.toEqual(data)
  })
  it.each(objectPaths)('rejects extra fields in object %s before cache', async (path) => {
    const data = fixture()
    objectAt(data, path).extra = sentinel
    await rejectsPayload(data)
  })
  it.each(fieldPaths)('rejects missing field $path / $key before cache', async ({ path, key }) => {
    const data = fixture()
    delete objectAt(data, path)[key]
    await rejectsPayload(data)
  })
  it.each(fieldPaths)(
    'rejects malformed field $path / $key before cache',
    async ({ path, key }) => {
      const data = fixture()
      objectAt(data, path)[key] = { raw: sentinel }
      await rejectsPayload(data)
    },
  )
  it.each(idPaths)('rejects uppercase canonical identifier %s.%s', async (path, key) => {
    const data = fixture()
    objectAt(data, path)[key] = id.toUpperCase()
    await rejectsPayload(data)
  })
  it.each(idPaths)('rejects malformed canonical identifier %s.%s', async (path, key) => {
    const data = fixture()
    objectAt(data, path)[key] = sentinel
    await rejectsPayload(data)
  })
  it.each(Object.keys(fixture().actions))('rejects authority action %s', async (key) => {
    const data = fixture()
    objectAt(data, 'actions')[key] = true
    await rejectsPayload(data)
  })
  it.each([
    ['identities.0', 'masked_identifier', sentinel],
    ['identities.0', 'avatar_status', 'available'],
    ['memberships.0', 'remote_actual_state', 'active'],
    ['current_memberships.0', 'remote_actual_state', 'active'],
    ['identities.0', 'organization', 'x'.repeat(256)],
    ['identities.0', 'activity', 'invented'],
  ])('rejects forbidden scalar %s.%s', async (path, key, value) => {
    const data = fixture()
    objectAt(data, path!)[key!] = value
    await rejectsPayload(data)
  })
  it.each(['identity_after', 'membership_after', 'current_membership_after'])(
    'keeps independent query cursor %s and anchor out of API',
    async (cursor) => {
      const options = casdoorIdentityQueryOptions(client, { [cursor]: id, limit: 50 })
      const other = casdoorIdentityQueryOptions(client, { [cursor]: otherId, limit: 50 })
      expect(options.queryKey).not.toEqual(other.queryKey)
      await client.fetchQuery(options)
      const [input, init] = fetchMock.mock.calls[0]!
      expect(Object.fromEntries(new URL(new Request(input, init).url).searchParams)).toEqual({
        [cursor]: id,
        limit: '50',
      })
    },
  )
  it.each([
    ...['identity_after', 'membership_after', 'current_membership_after'].flatMap((cursor) =>
      [null, id.toUpperCase(), sentinel].map((value) => ({ [cursor]: value })),
    ),
    { limit: 0 },
    { limit: 51 },
    { limit: null },
    { limit: 1.5 },
    { account_id: id },
    { query: {} },
    { body: {} },
    null,
  ])('rejects invalid query %# before dispatch', (query) => {
    expect(() =>
      casdoorIdentityQueryOptions(client, query as GetAccountCasdoorIdentityData['query']),
    ).toThrow('casdoor_self_invalid_query')
    expect(fetchMock).not.toHaveBeenCalled()
  })
  it('isolates account changes and rejects stale options before dispatch', async () => {
    const first = casdoorIdentityQueryOptions(client)
    await client.fetchQuery(first)
    seedAccountProfileQuery(client, { id: otherId })
    const second = casdoorIdentityQueryOptions(client)
    expect(first.queryKey).not.toEqual(second.queryKey)
    expect(client.getQueryData(second.queryKey)).toBeUndefined()
    await expect(client.fetchQuery(first)).rejects.toThrow('casdoor_self_context_changed')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await client.fetchQuery(second)
  })
  it('disables missing profile observers and rejects imperative reads without dispatch', async () => {
    client.removeQueries({ queryKey: userProfileQueryOptions().queryKey })
    const options = casdoorIdentityQueryOptions(client)
    expect(options.enabled).toBe(false)
    await expect(client.fetchQuery(options)).rejects.toThrow('casdoor_self_context_changed')
    expect(fetchMock).not.toHaveBeenCalled()
  })
  it.each(['change', 'remove'])('rejects a late response after profile %s', async (transition) => {
    let release!: (response: Response) => void
    fetchMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve
        }),
    )
    const options = casdoorIdentityQueryOptions(client)
    const pending = client.fetchQuery(options)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    if (transition === 'change') seedAccountProfileQuery(client, { id: otherId })
    else client.removeQueries({ queryKey: userProfileQueryOptions().queryKey })
    release(json(fixture()))
    await expect(pending).rejects.toThrow('casdoor_self_context_changed')
    expect(client.getQueryData(options.queryKey)).toBeUndefined()
  })
  it('actual logout clears profile and pending self cache and prevents stale options dispatch', async () => {
    let release!: (response: Response) => void
    fetchMock.mockImplementation((input, init) =>
      new Request(input, init).url.endsWith('/logout')
        ? Promise.resolve(json({ result: 'success' }))
        : new Promise((resolve) => {
            release = resolve
          }),
    )
    const options = casdoorIdentityQueryOptions(client)
    const pending = client.fetchQuery(options).catch((error) => error)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    const { result } = renderHook(() => useLogout(), {
      wrapper: ({ children }) => createElement(QueryClientProvider, { client }, children),
    })
    await act(async () => {
      await result.current.mutateAsync()
    })
    expect(client.getQueryCache().getAll()).toHaveLength(0)
    release(json(fixture()))
    await pending
    expect(client.getQueryData(options.queryKey)).toBeUndefined()
    await expect(client.fetchQuery(options)).rejects.toThrow('casdoor_self_context_changed')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
  it('actual profile patch invalidates original profile and all anchored self pages only', async () => {
    const self = casdoorIdentityQueryOptions(client)
    await client.fetchQuery(self)
    client.setQueryData(['unrelated'], 'keep')
    fetchMock.mockResolvedValue(json({ result: 'success' }))
    const mutation = client
      .getMutationCache()
      .build(client, consoleQuery.account.profile.patch.mutationOptions())
    await mutation.execute({ body: { name: 'Updated' } })
    expect(client.getQueryState(userProfileQueryOptions().queryKey)?.isInvalidated).toBe(true)
    expect(client.getQueryState(self.queryKey)?.isInvalidated).toBe(true)
    expect(client.getQueryState(['unrelated'])?.isInvalidated).toBe(false)
  })
  it('returns a fixed local transport code without raw error or retries', async () => {
    fetchMock.mockRejectedValue(new Error(sentinel))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { toast } = await import('@langgenius/dify-ui/toast')
    const notification = vi.spyOn(toast, 'error').mockImplementation(() => '')
    await expect(client.fetchQuery(casdoorIdentityQueryOptions(client))).rejects.toThrow(
      'casdoor_self_unavailable',
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(log).not.toHaveBeenCalled()
    expect(notification).not.toHaveBeenCalled()
  })
})
