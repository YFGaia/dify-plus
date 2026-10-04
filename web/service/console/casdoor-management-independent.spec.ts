import type { CasdoorConfiguration } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { dehydrate, MutationObserver, onlineManager, QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

const revisionId = '22222222-2222-4222-8222-222222222222'
const configuration: CasdoorConfiguration = {
  application: 'independent-app',
  backend_api_url: 'https://casdoor.example.test',
  browser_frontend_url: 'https://casdoor.example.test',
  client_id: 'independent-client',
  default_workspace_id: revisionId,
  expected_issuer: 'https://casdoor.example.test',
  organization: 'independent-org',
}
const emptyConfiguration = {
  enabled: false,
  etag: 0,
  active: null,
  active_revision_id: null,
  draft: null,
  draft_revision_id: null,
}
const staticResult = {
  revision_id: revisionId,
  etag: 1,
  kind: 'static' as const,
  status: 'passed' as const,
  static_only: true as const,
  checked_at: '2026-10-01T00:00:00Z',
  certificate_summaries: [
    {
      fingerprint: 'c'.repeat(64),
      not_before: '2026-09-01T00:00:00Z',
      accept_until: '2026-11-01T00:00:00Z',
    },
  ],
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  })
}

async function loadConsoleWithRequest(request: ReturnType<typeof vi.fn>) {
  vi.resetModules()
  vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
  vi.doMock('../base', () => ({ request }))
  return import('@/service/console')
}

function capturedRequest(request: ReturnType<typeof vi.fn>, index = 0): Request {
  const captured = request.mock.calls[index]?.[2]?.request
  if (!(captured instanceof Request)) throw new Error('Expected the Console request owner input')
  return captured
}

describe('independent Casdoor Console query integration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    onlineManager.setOnline(true)
    vi.restoreAllMocks()
    vi.doUnmock('../base')
    vi.doUnmock('@langgenius/dify-ui/toast')
    vi.doUnmock('./refresh-token')
    document.cookie = 'csrf_token=; Max-Age=0; path=/'
  })

  it('loads numeric workspace pagination through the current generated Console client path', async () => {
    const request = vi.fn().mockResolvedValue(
      jsonResponse({
        page: 3,
        limit: 20,
        total: 41,
        has_more: true,
        workspaces: [],
        earliest_created_workspace: null,
        earliest_created_ambiguous: false,
      }),
    )
    const { consoleClient } = await loadConsoleWithRequest(request)
    const result = await consoleClient.systemManageExtend.integration.casdoor.workspaces.get({
      query: { page: 3, limit: 20 },
    })
    const sent = capturedRequest(request)
    const url = new URL(sent.url)

    expect(result).toMatchObject({ page: 3, limit: 20, earliest_created_workspace: null })
    expect(url.pathname).toMatch(
      /\/console\/api\/system-manage-extend\/integration\/casdoor\/workspaces$/,
    )
    expect(url.searchParams.get('page')).toBe('3')
    expect(url.searchParams.get('limit')).toBe('20')
    expect(sent.method).toBe('GET')
    expect(sent.body).toBeNull()
  })

  it('passes a generated Casdoor request through the existing authenticated CSRF request owner', async () => {
    vi.resetModules()
    vi.doUnmock('../base')
    vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
    vi.doMock('@langgenius/dify-ui/toast', () => ({ toast: { error: vi.fn() } }))
    vi.doMock('./refresh-token', () => ({ refreshAccessTokenOrReLogin: vi.fn() }))
    document.cookie = 'csrf_token=synthetic-csrf-token; path=/'
    const requests: Request[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      requests.push(new Request(input, init))
      return jsonResponse(emptyConfiguration)
    })
    const { consoleClient } = await import('@/service/console')

    await expect(consoleClient.systemManageExtend.integration.casdoor.get()).resolves.toEqual(
      emptyConfiguration,
    )

    expect(requests).toHaveLength(1)
    expect(requests[0]?.method).toBe('GET')
    expect(new URL(requests[0]!.url).pathname).toMatch(
      /\/console\/api\/system-manage-extend\/integration\/casdoor$/,
    )
    expect(requests[0]?.body).toBeNull()
    expect(requests[0]?.headers.get('X-CSRF-Token')).toBe('synthetic-csrf-token')
    expect(requests[0]?.credentials).toBe('include')
  })

  it.each(['save', 'clear'] as const)(
    'invalidates only configuration and static-query caches after successful %s',
    async (action) => {
      const request = vi.fn().mockResolvedValue(jsonResponse({ ...emptyConfiguration, etag: 2 }))
      const { consoleQuery } = await loadConsoleWithRequest(request)
      const casdoor = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient()
      const configurationKey = casdoor.get.queryKey()
      const staticKey = casdoor.validate.post.queryKey({
        input: { body: { revision_id: revisionId, etag: 1 } },
      })
      const permissionKey = casdoor.permissions.get.queryKey()
      client.setQueryData(configurationKey, emptyConfiguration)
      client.setQueryData(staticKey, staticResult)
      client.setQueryData(permissionKey, { can_manage_casdoor: true })
      const featureOnSuccess = vi.fn()

      if (action === 'save') {
        await new MutationObserver(
          client,
          casdoor.put.mutationOptions({ onSuccess: featureOnSuccess }),
        ).mutate({ body: { configuration, etag: 1, secret: 'independent-input-secret' } })
      } else {
        await new MutationObserver(
          client,
          casdoor.clearSecret.post.mutationOptions({ onSuccess: featureOnSuccess }),
        ).mutate({ body: { revision_id: revisionId, etag: 1 } })
      }

      expect(featureOnSuccess).toHaveBeenCalledOnce()
      expect(client.getQueryState(configurationKey)?.isInvalidated).toBe(true)
      expect(client.getQueryState(staticKey)?.isInvalidated).toBe(true)
      expect(client.getQueryState(permissionKey)?.isInvalidated).toBe(false)
      expect(client.getQueryData(configurationKey)).toEqual(emptyConfiguration)
      expect(JSON.stringify(dehydrate(client))).not.toContain('independent-input-secret')
      expect(capturedRequest(request).method).toBe(action === 'save' ? 'PUT' : 'POST')
      expect(new URL(capturedRequest(request).url).pathname).toMatch(
        action === 'save' ? /\/casdoor$/ : /\/casdoor\/clear-secret$/,
      )
      expect(await capturedRequest(request).json()).toMatchObject(
        action === 'save'
          ? { configuration, etag: 1, secret: 'independent-input-secret' }
          : { revision_id: revisionId, etag: 1 },
      )
      for (const options of [
        casdoor.put.mutationOptions(),
        casdoor.clearSecret.post.mutationOptions(),
        casdoor.validate.post.mutationOptions(),
      ])
        expect(options).toMatchObject({ gcTime: 0, networkMode: 'always', retry: false })
      client.clear()
    },
  )

  it.each(['save', 'clear'] as const)(
    'preserves cached values on a rejected %s and does not retry the request',
    async (action) => {
      const request = vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ code: 'config_conflict', correlation_id: revisionId }, 409),
        )
      const { consoleQuery } = await loadConsoleWithRequest(request)
      const casdoor = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient()
      const configurationKey = casdoor.get.queryKey()
      const staticKey = casdoor.validate.post.queryKey({
        input: { body: { revision_id: revisionId, etag: 1 } },
      })
      client.setQueryData(configurationKey, emptyConfiguration)
      client.setQueryData(staticKey, staticResult)
      const mutation =
        action === 'save'
          ? new MutationObserver(client, casdoor.put.mutationOptions()).mutate({
              body: { configuration, etag: 1, secret: 'conflict-input-secret' },
            })
          : new MutationObserver(client, casdoor.clearSecret.post.mutationOptions()).mutate({
              body: { revision_id: revisionId, etag: 1 },
            })

      await expect(mutation).rejects.toThrow()
      expect(request).toHaveBeenCalledOnce()
      expect(client.getQueryState(configurationKey)?.isInvalidated).toBe(false)
      expect(client.getQueryState(staticKey)?.isInvalidated).toBe(false)
      expect(client.getQueryData(configurationKey)).toEqual(emptyConfiguration)
      expect(client.getQueryData(staticKey)).toEqual(staticResult)
      expect(JSON.stringify(dehydrate(client))).not.toContain('conflict-input-secret')
      client.clear()
    },
  )

  it('attempts a Secret mutation immediately offline and excludes it from default dehydration', async () => {
    const request = vi.fn().mockRejectedValue(new Error('synthetic offline failure'))
    const { consoleQuery } = await loadConsoleWithRequest(request)
    const client = new QueryClient()
    onlineManager.setOnline(false)
    const observer = new MutationObserver(
      client,
      consoleQuery.systemManageExtend.integration.casdoor.put.mutationOptions(),
    )

    await expect(
      observer.mutate({ body: { configuration, etag: 0, secret: 'offline-input-secret' } }),
    ).rejects.toThrow('synthetic offline failure')
    expect(request).toHaveBeenCalledOnce()
    expect(observer.getCurrentResult().isPaused).toBe(false)
    expect(dehydrate(client).mutations).toEqual([])
    expect(consoleQuery.systemManageExtend.integration.casdoor.put.mutationOptions()).toMatchObject(
      {
        gcTime: 0,
        networkMode: 'always',
        retry: false,
      },
    )
    observer.reset()
    await vi.waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0))
    client.clear()
  })

  it('keeps a static passed validation separate from enabled configuration', async () => {
    const request = vi.fn().mockResolvedValue(jsonResponse(staticResult))
    const { consoleQuery } = await loadConsoleWithRequest(request)
    const casdoor = consoleQuery.systemManageExtend.integration.casdoor
    const client = new QueryClient()
    const configurationKey = casdoor.get.queryKey()
    client.setQueryData(configurationKey, emptyConfiguration)

    const result = await new MutationObserver(
      client,
      casdoor.validate.post.mutationOptions(),
    ).mutate({ body: { revision_id: revisionId, etag: 1 } })

    expect(result).toMatchObject({ kind: 'static', status: 'passed', static_only: true })
    expect(client.getQueryData(configurationKey)).toEqual(emptyConfiguration)
    expect(client.getQueryState(configurationKey)?.isInvalidated).toBe(false)
    expect(casdoor.validate.post.mutationOptions()).toMatchObject({
      gcTime: 0,
      networkMode: 'always',
      retry: false,
    })
    client.clear()
  })
})
