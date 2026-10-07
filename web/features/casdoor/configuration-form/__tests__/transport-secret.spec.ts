import type { CasdoorConfigurationInput } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import {
  zCasdoorConfigurationResponse,
  zCasdoorSaveConfigurationPayloadWritable,
} from '@dify/contracts/api/console/system-manage-extend/zod.gen'
import { afterEach, describe, expect, it, vi } from 'vite-plus/test'

const configuration: CasdoorConfigurationInput = {
  schema_version: 2,
  application: 'synthetic-app',
  backend_api_url: 'https://idp.example.test',
  browser_frontend_url: 'https://idp.example.test',
  client_id: 'synthetic-client',
  default_workspace_id: '11111111-1111-4111-8111-111111111111',
  expected_issuer: 'https://idp.example.test',
  organization: 'synthetic-org',
}

describe('Casdoor PUT error logging boundary', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it.each([400, 409, 500, 'network'] as const)(
    'does not log transient Secret on %s failure through the real browser link',
    async (failure) => {
      const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
      const request = vi.fn().mockImplementation(() => {
        if (failure === 'network') return Promise.reject(new TypeError('Synthetic network failure'))
        return Promise.resolve(
          new Response(
            JSON.stringify({
              code: failure === 409 ? 'config_conflict' : 'provider_unavailable',
              correlation_id: '22222222-2222-4222-8222-222222222222',
              message: 'Casdoor management request failed.',
            }),
            { status: failure, headers: { 'content-type': 'application/json' } },
          ),
        )
      })
      vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
      vi.doMock('@/service/base', () => ({ request }))
      const { consoleClient } = await import('@/service/console')
      await expect(
        consoleClient.systemManageExtend.integration.casdoor.put(
          {
            body: { configuration, etag: 1, secret: 'synthetic-i26-transient-secret' },
          },
          { context: { silent: true } },
        ),
      ).rejects.toBeDefined()
      expect(request).toHaveBeenCalledOnce()
      expect(logged).toHaveBeenCalled()
      const inspected: string[] = []
      const seen = new Set<object>()
      const inspect = async (value: unknown): Promise<void> => {
        if (typeof value === 'string') {
          inspected.push(value)
          return
        }
        if (!value || typeof value !== 'object' || seen.has(value)) return
        seen.add(value)
        if (value instanceof Request) {
          inspected.push(value.url)
          inspected.push(await value.clone().text())
        }
        for (const key of Object.getOwnPropertyNames(value)) {
          inspected.push(key)
          await inspect(Reflect.get(value, key))
        }
      }
      for (const call of logged.mock.calls) await inspect(call)
      expect(inspected.join('\n')).not.toContain('synthetic-i26-transient-secret')
    },
  )

  it.each(['input', 'output'] as const)(
    'records the actual %s runtime-schema boundary without logging the Secret',
    async (failure) => {
      const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
      const request = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ etag: 'invalid-etag', draft: 'invalid-draft' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        }),
      )
      vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
      vi.doMock('@/service/base', () => ({ request }))
      const { consoleClient } = await import('@/service/console')
      const body = {
        configuration: {
          ...configuration,
          default_workspace_id:
            failure === 'input' ? 'invalid-uuid' : configuration.default_workspace_id,
        },
        etag: 1,
        secret: 'synthetic-i26-schema-secret',
      }
      expect(zCasdoorSaveConfigurationPayloadWritable.safeParse(body).success).toBe(
        failure !== 'input',
      )
      const outcome = await consoleClient.systemManageExtend.integration.casdoor.put(
        { body },
        { context: { silent: true } },
      )
      const inspected: string[] = []
      const seen = new Set<object>()
      function inspect(value: unknown) {
        if (typeof value === 'string') {
          inspected.push(value)
          return
        }
        if (!value || typeof value !== 'object' || seen.has(value)) return
        seen.add(value)
        for (const key of Object.getOwnPropertyNames(value)) {
          inspected.push(key)
          inspect(Reflect.get(value, key))
        }
      }
      for (const call of logged.mock.calls) inspect(call)
      expect(inspected.join('\n')).not.toContain('synthetic-i26-schema-secret')
      // The installed OpenAPILink serializes but does not run schema validation.
      // A consumer must not call this malformed result a validated server revision.
      expect(outcome).toEqual({ etag: 'invalid-etag', draft: 'invalid-draft' })
      expect(zCasdoorConfigurationResponse.safeParse(outcome).success).toBe(false)
      expect(request).toHaveBeenCalledOnce()
      expect(logged).not.toHaveBeenCalled()
    },
  )
})
