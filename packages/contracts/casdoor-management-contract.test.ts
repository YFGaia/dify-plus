import type { CasdoorConfiguration } from './generated/api/console/system-manage-extend/types.gen'
import { describe, expect, it } from 'vite-plus/test'
import { contractLoaders } from './generated/api/console/orpc.gen'
import {
  zCasdoorConfigurationResponse,
  zCasdoorRevisionResponse,
  zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery,
  zGetSystemManageExtendIntegrationCasdoorWorkspacesResponse,
  zPostSystemManageExtendIntegrationCasdoorClearSecretBody,
  zPostSystemManageExtendIntegrationCasdoorValidateResponse,
  zPutSystemManageExtendIntegrationCasdoorBody,
} from './generated/api/console/system-manage-extend/zod.gen'

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

describe('generated Casdoor management contract', () => {
  it('loads exactly the six accepted operations through the generated segment', async () => {
    const { systemManageExtend } = await contractLoaders.systemManageExtend()
    const casdoor = systemManageExtend.integration.casdoor
    const prefix = '/system-manage-extend/integration/casdoor'
    const operations = [
      [casdoor.get, 'GET', prefix],
      [casdoor.put, 'PUT', prefix],
      [casdoor.permissions.get, 'GET', `${prefix}/permissions`],
      [casdoor.clearSecret.post, 'POST', `${prefix}/clear-secret`],
      [casdoor.validate.post, 'POST', `${prefix}/validate`],
      [casdoor.workspaces.get, 'GET', `${prefix}/workspaces`],
    ] as const
    expect(Object.keys(casdoor).sort()).toEqual([
      'clearSecret',
      'get',
      'permissions',
      'put',
      'validate',
      'workspaces',
    ])
    for (const [operation, method, path] of operations)
      expect(operation['~orpc'].route).toMatchObject({ method, path, inputStructure: 'detailed' })
  })

  it('accepts a write-only replacement or blank keep input without adding a response secret', () => {
    for (const secret of [undefined, null, '', 'synthetic-contract-secret']) {
      expect(
        zPutSystemManageExtendIntegrationCasdoorBody.safeParse({
          configuration,
          etag: 0,
          secret,
        }).success,
      ).toBe(true)
    }
    expect(zCasdoorConfigurationResponse.shape).not.toHaveProperty('secret')
    expect(zCasdoorRevisionResponse.shape).not.toHaveProperty('secret')
    expect(zCasdoorRevisionResponse.shape).toHaveProperty('secret_configured')
  })

  it('requires exact revision and ETag for the explicit clear operation', () => {
    expect(
      zPostSystemManageExtendIntegrationCasdoorClearSecretBody.parse({
        revision_id: revisionId,
        etag: 2,
      }),
    ).toEqual({ revision_id: revisionId, etag: 2 })
    expect(
      zPostSystemManageExtendIntegrationCasdoorClearSecretBody.safeParse({ etag: 2 }).success,
    ).toBe(false)
    expect(
      zPostSystemManageExtendIntegrationCasdoorClearSecretBody.safeParse({
        revision_id: revisionId,
        etag: -1,
      }).success,
    ).toBe(false)
  })

  it('keeps native numeric pagination bounds and empty workspace metadata without choosing a target', () => {
    expect(zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.parse({})).toEqual({
      page: 1,
      limit: 50,
    })
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.safeParse({ page: 0 }).success,
    ).toBe(false)
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.safeParse({ limit: 101 }).success,
    ).toBe(false)
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.safeParse({ page: '2' }).success,
    ).toBe(false)
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesResponse.parse({
        workspaces: [],
        page: 1,
        limit: 50,
        total: 0,
        has_more: false,
        earliest_created_workspace: null,
        earliest_created_ambiguous: false,
      }),
    ).toMatchObject({ workspaces: [], earliest_created_workspace: null })
  })

  it('preserves static-only success with exact revision and no activation fields', () => {
    const result = zPostSystemManageExtendIntegrationCasdoorValidateResponse.parse({
      revision_id: revisionId,
      etag: 2,
      checked_at: '2026-10-01T00:00:00Z',
      kind: 'static',
      status: 'passed',
      static_only: true,
      certificate_summaries: [
        {
          fingerprint: 'a'.repeat(64),
          not_before: '2026-09-01T00:00:00Z',
          accept_until: '2026-11-01T00:00:00Z',
        },
      ],
    })
    expect(result).toMatchObject({
      revision_id: revisionId,
      etag: 2,
      kind: 'static',
      static_only: true,
    })
    expect(result).not.toHaveProperty('enabled')
    expect(result).not.toHaveProperty('activation_ready')
    expect(
      zPostSystemManageExtendIntegrationCasdoorValidateResponse.safeParse({
        ...result,
        static_only: false,
      }).success,
    ).toBe(false)
  })
})
