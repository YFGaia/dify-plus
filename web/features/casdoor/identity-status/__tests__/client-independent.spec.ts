import type { GetAccountCasdoorIdentityResponse } from '@dify/contracts/api/console/account/types.gen'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { casdoorIdentityQueryOptions } from '../client'

vi.mock('@/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config')>()),
  API_PREFIX: 'http://localhost:3000/console/api',
}))

const profileA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const profileB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const response = (): GetAccountCasdoorIdentityResponse => ({
  actions: {
    adopt: false,
    link: false,
    logout: false,
    reauthenticate: false,
    release: false,
    retry: false,
    unlink: false,
  },
  binding: 'unlinked',
  current_membership_has_more: false,
  current_membership_next: null,
  current_memberships: [],
  identity_has_more: false,
  identity_next: null,
  identities: [],
  membership_has_more: false,
  membership_next: null,
  memberships: [],
})

type PendingResponse = (value: Response) => void
let pending: PendingResponse[]
let fetchMock: ReturnType<typeof vi.fn<typeof fetch>>
let client: QueryClient

beforeEach(() => {
  pending = []
  fetchMock = vi.fn<typeof fetch>(() => new Promise((resolve) => pending.push(resolve)))
  vi.stubGlobal('fetch', fetchMock)
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  seedAccountProfileQuery(client, { id: profileA })
})

afterEach(() => {
  client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const json = (body: unknown) =>
  new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })

describe('independent authenticated-self observer lifecycle', () => {
  it('moves an active observer from profile A to B while A is pending and never exposes late A as B', async () => {
    const observer = new QueryObserver(client, casdoorIdentityQueryOptions(client))
    const seen: Array<{ status: string; data: GetAccountCasdoorIdentityResponse | undefined }> = []
    const unsubscribe = observer.subscribe((result) => {
      seen.push({ status: result.status, data: result.data })
    })
    const observerA = new QueryObserver(client, casdoorIdentityQueryOptions(client))
    const unsubscribeA = observerA.subscribe(() => {})

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(pending).toHaveLength(1)
    const keyA = observer.getCurrentQuery().queryKey

    seedAccountProfileQuery(client, { id: profileB })
    const optionsB = casdoorIdentityQueryOptions(client)
    observer.setOptions(optionsB)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
    expect(observer.getCurrentQuery().queryKey).toEqual(optionsB.queryKey)
    expect(optionsB.queryKey).not.toEqual(keyA)
    expect(observer.getCurrentResult().data).toBeUndefined()

    pending[1]!(json(response()))
    await vi.waitFor(() => expect(observer.getCurrentResult().status).toBe('success'))
    expect(client.getQueryData(optionsB.queryKey)).toEqual(response())

    pending[0]!(json(response()))
    await vi.waitFor(() => expect(client.getQueryState(keyA)?.status).toBe('error'))
    expect(observerA.getCurrentResult().error).toMatchObject({
      message: 'casdoor_self_context_changed',
    })
    expect(client.getQueryData(keyA)).toBeUndefined()
    expect(client.getQueryData(optionsB.queryKey)).toEqual(response())
    expect(observer.getCurrentResult()).toMatchObject({ status: 'success', data: response() })
    expect(
      seen.every(
        ({ data }) => data === undefined || data === client.getQueryData(optionsB.queryKey),
      ),
    ).toBe(true)

    unsubscribe()
    unsubscribeA()
  })
})
