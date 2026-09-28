import type { GetLoginConfigResponse } from '@dify/contracts/api/console/login-config/types.gen'
import { QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { createSystemFeaturesFixture } from '@/test/console/system-features'
import { loginConfigQueryOptions, systemFeaturesQueryOptions } from '../client'
import { asSystemFeaturesExtend } from '../extend'

vi.mock('@langgenius/dify-ui/toast', () => ({ toast: { error: vi.fn() } }))

const publicFeatures = createSystemFeaturesFixture()
const config: GetLoginConfigResponse = {
  ...publicFeatures,
  is_custom_auth2: true,
  is_custom_auth2_logout: 'https://identity.example/logout',
  ding_talk: true,
  ding_talk_client_id: 'client-id',
  ding_talk_corp_id: 'corp-id',
  rmb_to_usd_rate: 8,
}
const anonymous = { accountId: null, workspaceId: null } as const
let queryClient: QueryClient

beforeEach(() => {
  vi.clearAllMocks()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})
afterEach(() => {
  queryClient.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

const mockResponse = (body: unknown, status = 200) =>
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const request = new Request(input, init)
    if (request.url.endsWith('/login_config_bootstrap'))
      return Response.json({ ok: true, token: 'bootstrap-token' })
    return Response.json(body, { status })
  })

describe('public feature and fork login configuration boundaries', () => {
  it.each([
    { ping: true },
    { deployment_edition: 'COMMUNITY' },
    { ...publicFeatures, webapp_auth: {} },
  ])('rejects malformed public responses before they enter query cache: %j', async (body) => {
    mockResponse(body)
    const options = systemFeaturesQueryOptions()
    await expect(queryClient.fetchQuery(options)).rejects.toThrow()
    expect(queryClient.getQueryData(options.queryKey)).toBeUndefined()
  })

  it('reads and caches only the public snapshot without requesting login config or license', async () => {
    const fetch = mockResponse({
      ...config,
      license: { ...config.license, seats: { size: 4 } },
    })
    const options = systemFeaturesQueryOptions()
    await expect(queryClient.fetchQuery(options)).resolves.toEqual(publicFeatures)
    expect(queryClient.getQueryData(options.queryKey)).toEqual(publicFeatures)
    expect(fetch).toHaveBeenCalledOnce()
    expect(new Request(fetch.mock.calls[0]![0]).url).toMatch(/\/system-features$/)
  })

  it.each([{ ping: true }, { ...config, is_custom_auth2: 'true' }])(
    'rejects malformed login configuration before caching: %j',
    async (body) => {
      mockResponse(body)
      const options = loginConfigQueryOptions(anonymous)
      await expect(queryClient.fetchQuery(options)).rejects.toThrow()
      expect(queryClient.getQueryData(options.queryKey)).toBeUndefined()
    },
  )

  it('keeps shared public fields authoritative and projects only actual fork fields', async () => {
    const response = {
      ...config,
      branding: { ...config.branding, application_title: 'outdated login title' },
      license: { ...config.license, expired_at: '2030-01-01', seats: { size: 10 } },
    }
    mockResponse(response)
    const options = loginConfigQueryOptions(anonymous)
    const login = await queryClient.fetchQuery(options)
    const view = asSystemFeaturesExtend(publicFeatures, login)
    expect(view.branding).toEqual(publicFeatures.branding)
    expect(view.license).toEqual(publicFeatures.license)
    expect(view.is_custom_auth2).toBe(true)
    expect(view.rmb_to_usd_rate).toBe(8)
    expect(view).not.toHaveProperty('is_custom_auth2_button')
    expect(login.license).not.toHaveProperty('seats')
    expect(queryClient.getQueryData(systemFeaturesQueryOptions().queryKey)).toBeUndefined()
  })

  it('cannot read another account or workspace config from the same cache', async () => {
    const fetch = mockResponse(config)
    const alice = loginConfigQueryOptions({ accountId: 'alice', workspaceId: 'one' })
    const bob = loginConfigQueryOptions({ accountId: 'bob', workspaceId: 'one' })
    const otherWorkspace = loginConfigQueryOptions({ accountId: 'alice', workspaceId: 'two' })
    await queryClient.fetchQuery(alice)
    expect(queryClient.getQueryData(bob.queryKey)).toBeUndefined()
    expect(queryClient.getQueryData(otherWorkspace.queryKey)).toBeUndefined()
    expect(queryClient.getQueryData(loginConfigQueryOptions(anonymous).queryKey)).toBeUndefined()
    await queryClient.fetchQuery(bob)
    expect(
      fetch.mock.calls.filter(([input]) => new Request(input).url.endsWith('/login_config')),
    ).toHaveLength(2)
    queryClient.clear()
    expect(queryClient.getQueryData(alice.queryKey)).toBeUndefined()
  })

  it('preserves forbidden config responses without caching a public fallback', async () => {
    const fetch = mockResponse({ code: 'forbidden', message: 'Invalid token' }, 403)
    const options = loginConfigQueryOptions(anonymous)
    await expect(queryClient.fetchQuery(options)).rejects.toThrow()
    expect(queryClient.getQueryData(options.queryKey)).toBeUndefined()
    expect(
      fetch.mock.calls.some(([input]) => new Request(input).url.endsWith('/system-features')),
    ).toBe(false)
  })

  it('refuses SSR login configuration before bootstrap or license network calls', async () => {
    const fetch = mockResponse(config)
    vi.stubGlobal('window', undefined)
    await expect(queryClient.fetchQuery(loginConfigQueryOptions(anonymous))).rejects.toThrow(
      'Login configuration is only available in the browser',
    )
    expect(fetch).not.toHaveBeenCalled()
  })

  it('does not synthesize fork authentication from fields on a public response', () => {
    const view = asSystemFeaturesExtend({ ...publicFeatures, ...config })
    expect(view.is_custom_auth2).toBe(false)
    expect(view.ding_talk).toBe(false)
    expect(view.rmb_to_usd_rate).toBe(7.26)
  })
})
