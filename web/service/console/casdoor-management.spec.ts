import type { CasdoorConfiguration } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { zPostSystemManageExtendIntegrationCasdoorValidateResponse } from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { dehydrate, MutationObserver, onlineManager, QueryClient } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'

const revisionId = '11111111-1111-4111-8111-111111111111'
const configuration: CasdoorConfiguration = {
  application: 'synthetic-app',
  backend_api_url: 'https://idp.example.test',
  browser_frontend_url: 'https://idp.example.test',
  client_id: 'synthetic-client',
  default_workspace_id: revisionId,
  expected_issuer: 'https://idp.example.test',
  organization: 'synthetic-org',
}
const emptyConfiguration = {
  enabled: false,
  etag: 0,
  active: null,
  active_revision_id: null,
  draft: null,
  draft_revision_id: null,
}
const staticResult = zPostSystemManageExtendIntegrationCasdoorValidateResponse.parse({
  revision_id: revisionId,
  etag: 1,
  kind: 'static',
  status: 'passed',
  static_only: true,
  checked_at: '2026-10-01T00:00:00Z',
  certificate_summaries: [
    {
      fingerprint: 'a'.repeat(64),
      not_before: '2026-09-01T00:00:00Z',
      accept_until: '2026-11-01T00:00:00Z',
    },
  ],
})

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
  })
}

async function loadConsole(request: ReturnType<typeof vi.fn>) {
  vi.resetModules()
  vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
  vi.doMock('../base', () => ({ request }))
  return import('@/service/console')
}

function sentRequest(request: ReturnType<typeof vi.fn>, index = 0): Request {
  const call = request.mock.calls[index]
  if (!call) throw new Error('Expected a Console transport request')
  return call[2].request as Request
}

describe('Casdoor generated consoleQuery integration', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    onlineManager.setOnline(true)
    vi.restoreAllMocks()
    vi.doUnmock('../base')
  })

  it('reads permissions, configuration, and workspace pagination using the generated loader and transport', async () => {
    const request = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ can_manage_casdoor: false }))
      .mockResolvedValueOnce(jsonResponse(emptyConfiguration))
      .mockResolvedValueOnce(
        jsonResponse({
          workspaces: [],
          page: 2,
          limit: 25,
          total: 0,
          has_more: false,
          earliest_created_workspace: null,
          earliest_created_ambiguous: false,
        }),
      )
    const { consoleQuery } = await loadConsole(request)
    const casdoor = consoleQuery.systemManageExtend.integration.casdoor
    const client = new QueryClient()
    expect(await client.fetchQuery(casdoor.permissions.get.queryOptions())).toEqual({
      can_manage_casdoor: false,
    })
    expect(await client.fetchQuery(casdoor.get.queryOptions())).toEqual(emptyConfiguration)
    expect(
      await client.fetchQuery(
        casdoor.workspaces.get.queryOptions({
          input: { query: { page: 2, limit: 25 } },
        }),
      ),
    ).toMatchObject({ workspaces: [], earliest_created_workspace: null })
    const requests = request.mock.calls.map((_call, index) => sentRequest(request, index))
    expect(requests.map((req) => req.method)).toEqual(['GET', 'GET', 'GET'])
    expect(new URL(sentRequest(request, 0).url).pathname).toMatch(
      /\/system-manage-extend\/integration\/casdoor\/permissions$/,
    )
    expect(new URL(sentRequest(request, 1).url).pathname).toMatch(
      /\/system-manage-extend\/integration\/casdoor$/,
    )
    expect(new URL(sentRequest(request, 2).url).searchParams.get('page')).toBe('2')
    expect(new URL(sentRequest(request, 2).url).searchParams.get('limit')).toBe('25')
    expect(requests.every((req) => req.body === null)).toBe(true)
    client.clear()
  })

  it.each(['save', 'clear'] as const)(
    'invalidates configuration and prior static queries only after successful %s',
    async (action) => {
      const request = vi.fn().mockResolvedValue(jsonResponse({ ...emptyConfiguration, etag: 2 }))
      const { consoleQuery } = await loadConsole(request)
      const casdoor = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient()
      const configKey = casdoor.get.queryKey()
      const staticKey = casdoor.validate.post.queryKey({
        input: { body: { revision_id: revisionId, etag: 1 } },
      })
      const permissionKey = casdoor.permissions.get.queryKey()
      client.setQueryData(configKey, emptyConfiguration)
      client.setQueryData(staticKey, staticResult)
      client.setQueryData(permissionKey, { can_manage_casdoor: true })
      const onSuccess = vi.fn()
      if (action === 'save') {
        const observer = new MutationObserver(client, casdoor.put.mutationOptions({ onSuccess }))
        await observer.mutate({
          body: { configuration, etag: 1, secret: 'synthetic-input-secret' },
        })
      } else {
        const observer = new MutationObserver(
          client,
          casdoor.clearSecret.post.mutationOptions({ onSuccess }),
        )
        await observer.mutate({ body: { revision_id: revisionId, etag: 1 } })
      }
      expect(onSuccess).toHaveBeenCalledOnce()
      expect(client.getQueryState(configKey)?.isInvalidated).toBe(true)
      expect(client.getQueryState(staticKey)?.isInvalidated).toBe(true)
      expect(client.getQueryState(permissionKey)?.isInvalidated).toBe(false)
      expect(client.getQueryData(configKey)).toEqual(emptyConfiguration)
      expect(JSON.stringify(dehydrate(client))).not.toContain('synthetic-input-secret')
      const outgoing = sentRequest(request)
      expect(outgoing.method).toBe(action === 'save' ? 'PUT' : 'POST')
      expect(new URL(outgoing.url).pathname).toMatch(
        action === 'save' ? /\/casdoor$/ : /\/casdoor\/clear-secret$/,
      )
      expect(await outgoing.json()).toMatchObject(
        action === 'save'
          ? { etag: 1, configuration, secret: 'synthetic-input-secret' }
          : { etag: 1, revision_id: revisionId },
      )
      expect(casdoor.put.mutationOptions().gcTime).toBe(0)
      expect(casdoor.clearSecret.post.mutationOptions().gcTime).toBe(0)
      client.clear()
    },
  )

  it.each(['save', 'clear'] as const)(
    'preserves configuration and static cache when %s is rejected',
    async (action) => {
      const request = vi.fn().mockResolvedValue(
        jsonResponse(
          {
            code: 'config_conflict',
            correlation_id: revisionId,
            message: 'Casdoor management request failed.',
          },
          409,
        ),
      )
      const { consoleQuery } = await loadConsole(request)
      const casdoor = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient()
      const configKey = casdoor.get.queryKey()
      const staticKey = casdoor.validate.post.queryKey({
        input: { body: { revision_id: revisionId, etag: 1 } },
      })
      client.setQueryData(configKey, emptyConfiguration)
      client.setQueryData(staticKey, staticResult)
      if (action === 'save') {
        const observer = new MutationObserver(client, casdoor.put.mutationOptions())
        await expect(
          observer.mutate({ body: { configuration, etag: 1, secret: 'synthetic-input-secret' } }),
        ).rejects.toThrow()
      } else {
        const observer = new MutationObserver(client, casdoor.clearSecret.post.mutationOptions())
        await expect(
          observer.mutate({ body: { revision_id: revisionId, etag: 1 } }),
        ).rejects.toThrow()
      }
      expect(client.getQueryState(configKey)?.isInvalidated).toBe(false)
      expect(client.getQueryState(staticKey)?.isInvalidated).toBe(false)
      expect(client.getQueryData(configKey)).toEqual(emptyConfiguration)
      expect(client.getQueryData(staticKey)).toEqual(staticResult)
      expect(JSON.stringify(dehydrate(client))).not.toContain('synthetic-input-secret')
      client.clear()
    },
  )

  it('keeps a passed static mutation separate from enabled configuration', async () => {
    const request = vi.fn().mockResolvedValue(jsonResponse(staticResult))
    const { consoleQuery } = await loadConsole(request)
    const casdoor = consoleQuery.systemManageExtend.integration.casdoor
    const client = new QueryClient()
    const configKey = casdoor.get.queryKey()
    const displayKey = consoleQuery.auth.casdoor.display.get.queryKey()
    const permissionKey = casdoor.permissions.get.queryKey()
    client.setQueryData(configKey, emptyConfiguration)
    client.setQueryData(displayKey, {
      enabled: false,
      button_text: 'Casdoor',
      start_path: '/console/api/auth/casdoor/login',
    })
    client.setQueryData(permissionKey, { can_manage_casdoor: true })
    const observer = new MutationObserver(client, casdoor.validate.post.mutationOptions())
    expect(await observer.mutate({ body: { revision_id: revisionId, etag: 1 } })).toEqual(
      staticResult,
    )
    expect(client.getQueryData(configKey)).toEqual(emptyConfiguration)
    expect(client.getQueryState(configKey)?.isInvalidated).toBe(true)
    expect(client.getQueryData(configKey)?.enabled).toBe(false)
    expect(client.getQueryState(displayKey)?.isInvalidated).toBe(false)
    expect(client.getQueryData(displayKey)?.enabled).toBe(false)
    expect(client.getQueryState(permissionKey)?.isInvalidated).toBe(false)
    expect(casdoor.validate.post.mutationOptions().gcTime).toBe(0)
    const outgoing = sentRequest(request)
    expect(outgoing.method).toBe('POST')
    expect(new URL(outgoing.url).pathname).toMatch(/\/casdoor\/validate$/)
    expect(await outgoing.json()).toEqual({ revision_id: revisionId, etag: 1 })
    client.clear()
  })

  it.each(['activate', 'disable'] as const)(
    'refreshes configuration and sign-in display after %s while preserving caller callbacks',
    async (action) => {
      const request = vi.fn().mockResolvedValue(
        jsonResponse(
          action === 'activate'
            ? { ...emptyConfiguration, enabled: true, etag: 2 }
            : {
                configuration: { ...emptyConfiguration, etag: 2 },
                reconciliation_required: false,
              },
        ),
      )
      const { consoleQuery } = await loadConsole(request)
      const casdoor = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient()
      const configKey = casdoor.get.queryKey()
      const displayKey = consoleQuery.auth.casdoor.display.get.queryKey()
      const permissionKey = casdoor.permissions.get.queryKey()
      client.setQueryData(configKey, emptyConfiguration)
      client.setQueryData(displayKey, {
        enabled: false,
        button_text: 'Casdoor',
        start_path: '/console/api/auth/casdoor/login',
      })
      client.setQueryData(permissionKey, { can_manage_casdoor: true })
      const onSuccess = vi.fn()
      if (action === 'activate') {
        await new MutationObserver(
          client,
          casdoor.activate.post.mutationOptions({ onSuccess }),
        ).mutate({ body: { etag: 1, revision_id: revisionId } })
      } else {
        await new MutationObserver(
          client,
          casdoor.disable.post.mutationOptions({ onSuccess }),
        ).mutate({ body: { etag: 1 } })
      }
      expect(onSuccess).toHaveBeenCalledOnce()
      expect(client.getQueryState(configKey)?.isInvalidated).toBe(true)
      expect(client.getQueryState(displayKey)?.isInvalidated).toBe(true)
      expect(client.getQueryState(permissionKey)?.isInvalidated).toBe(false)
      expect(client.getQueryData(configKey)).toEqual(emptyConfiguration)
      client.clear()
    },
  )

  it.each(['activate', 'testLogin'] as const)(
    'does not queue or retry %s even when global mutation defaults would retry',
    async (action) => {
      const request = vi.fn().mockRejectedValue(new Error('synthetic connection loss'))
      const { consoleQuery } = await loadConsole(request)
      const casdoor = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient({ defaultOptions: { mutations: { retry: 3 } } })
      onlineManager.setOnline(false)
      const options =
        action === 'activate'
          ? casdoor.activate.post.mutationOptions()
          : casdoor.testLogin.post.mutationOptions()
      // Both operations share the revision input, but own distinct generated response types.
      if (action === 'activate') {
        await expect(
          new MutationObserver(client, casdoor.activate.post.mutationOptions()).mutate({
            body: { etag: 1, revision_id: revisionId },
          }),
        ).rejects.toThrow('synthetic connection loss')
      } else {
        await expect(
          new MutationObserver(client, casdoor.testLogin.post.mutationOptions()).mutate({
            body: { etag: 1, revision_id: revisionId },
          }),
        ).rejects.toThrow('synthetic connection loss')
      }
      expect(request).toHaveBeenCalledOnce()
      expect(options.gcTime).toBe(0)
      expect(dehydrate(client).mutations).toEqual([])
      client.clear()
    },
  )

  it('does not queue or dehydrate a Secret mutation while the online manager is offline', async () => {
    const request = vi.fn().mockRejectedValue(new Error('synthetic offline failure'))
    const { consoleQuery } = await loadConsole(request)
    const client = new QueryClient()
    onlineManager.setOnline(false)
    const observer = new MutationObserver(
      client,
      consoleQuery.systemManageExtend.integration.casdoor.put.mutationOptions(),
    )
    await expect(
      observer.mutate({
        body: { configuration, etag: 0, secret: 'synthetic-offline-secret' },
      }),
    ).rejects.toThrow('synthetic offline failure')
    expect(request).toHaveBeenCalledOnce()
    expect(observer.getCurrentResult().isPaused).toBe(false)
    expect(dehydrate(client).mutations).toEqual([])
    observer.reset()
    await vi.waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0))
    client.clear()
  })
})

describe('reset and avatar retry shared defaults', () => {
  it.each(['reset', 'retry'] as const)(
    'invalidates all %s read keys after a lost response without replacing per-call callbacks',
    async (kind) => {
      const request = vi.fn().mockRejectedValue(new Error('synthetic connection loss'))
      const { consoleQuery } = await loadConsole(request)
      const api = consoleQuery.systemManageExtend.integration.casdoor
      const client = new QueryClient({ defaultOptions: { mutations: { retry: 3 } } })
      const keys =
        kind === 'reset'
          ? [
              api.get.queryKey(),
              consoleQuery.auth.casdoor.display.get.queryKey(),
              api.localMembership.targets.get.queryKey({ input: { query: {} } }),
              api.sync.retryTargets.get.queryKey({ input: { query: {} } }),
              consoleQuery.account.casdoorIdentity.get.queryKey({ input: { query: {} } }),
              consoleQuery.account.casdoorIdentity.actions.get.queryKey(),
              consoleQuery.auth.casdoor.session.get.queryKey(),
            ]
          : [
              api.sync.retryTargets.get.queryKey({ input: { query: {} } }),
              consoleQuery.account.casdoorIdentity.get.queryKey({ input: { query: {} } }),
              consoleQuery.account.casdoorIdentity.actions.get.queryKey(),
              consoleQuery.account.profile.get.queryKey(),
            ]
      for (const key of keys) client.setQueryData<unknown>(key, { synthetic: true })
      const permissionKey = api.permissions.get.queryKey()
      client.setQueryData(permissionKey, { can_manage_casdoor: true })
      const onError = vi.fn()
      const onSettled = vi.fn()
      onlineManager.setOnline(false)
      if (kind === 'reset') {
        const observer = new MutationObserver(client, api.resetNamespace.post.mutationOptions())
        const unsubscribe = observer.subscribe(() => {})
        await expect(
          observer.mutate({ body: { review_id: 'R'.repeat(43), etag: 1 } }, { onError, onSettled }),
        ).rejects.toThrow('synthetic connection loss')
        expect(observer.options).toMatchObject({ gcTime: 0, retry: false, networkMode: 'always' })
        unsubscribe()
      } else {
        const observer = new MutationObserver(client, api.sync.retry.post.mutationOptions())
        const unsubscribe = observer.subscribe(() => {})
        await expect(
          observer.mutate({ body: { intent_id: revisionId } }, { onError, onSettled }),
        ).rejects.toThrow('synthetic connection loss')
        expect(observer.options).toMatchObject({ gcTime: 0, retry: false, networkMode: 'always' })
        unsubscribe()
      }
      expect(onError).toHaveBeenCalledOnce()
      expect(onSettled).toHaveBeenCalledOnce()
      expect(request).toHaveBeenCalledOnce()
      for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true)
      expect(client.getQueryState(permissionKey)?.isInvalidated).toBe(false)
      expect(dehydrate(client).mutations).toEqual([])
      onlineManager.setOnline(true)
      client.clear()
      vi.doUnmock('../base')
    },
  )
})
