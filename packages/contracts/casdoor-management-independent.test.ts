import type {
  CasdoorConfiguration,
  CasdoorSaveConfigurationPayload,
  CasdoorSaveConfigurationPayloadWritable,
} from './generated/api/console/system-manage-extend/types.gen'
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
  application: 'independent-app',
  backend_api_url: 'https://casdoor.example.test',
  browser_frontend_url: 'https://casdoor.example.test',
  client_id: 'independent-client',
  default_workspace_id: revisionId,
  expected_issuer: 'https://casdoor.example.test',
  organization: 'independent-org',
}

describe('independent generated Casdoor management contract', () => {
  it('resolves every current generated operation and keeps fork aliases separate', async () => {
    const { systemManageExtend } = await contractLoaders.systemManageExtend()
    const casdoor = systemManageExtend.integration.casdoor
    const root = '/system-manage-extend/integration/casdoor'
    const expected = [
      [casdoor.resetNamespace.review.post, 'POST', `${root}/reset-namespace/review`],
      [casdoor.resetNamespace.post, 'POST', `${root}/reset-namespace`],
      [casdoor.sync.retryTargets.get, 'GET', `${root}/sync/retry-targets`],
      [casdoor.sync.retry.post, 'POST', `${root}/sync/retry`],
      [casdoor.get, 'GET', root],
      [casdoor.put, 'PUT', root],
      [casdoor.permissions.get, 'GET', `${root}/permissions`],
      [casdoor.clearSecret.post, 'POST', `${root}/clear-secret`],
      [casdoor.validate.post, 'POST', `${root}/validate`],
      [casdoor.workspaces.get, 'GET', `${root}/workspaces`],
      [casdoor.activate.post, 'POST', `${root}/activate`],
      [casdoor.disable.post, 'POST', `${root}/disable`],
      [casdoor.testLogin.post, 'POST', `${root}/test-login`],
      [casdoor.testReauth.post, 'POST', `${root}/test-reauth`],
      [casdoor.testRpLogout.post, 'POST', `${root}/test-rp-logout`],
      [casdoor.rpLogoutStatus.get, 'GET', `${root}/rp-logout-status`],
      [casdoor.localMembership.get, 'GET', `${root}/local-membership`],
      [casdoor.localMembership.targets.get, 'GET', `${root}/local-membership/targets`],
      [casdoor.localMembership.review.post, 'POST', `${root}/local-membership/review`],
      [casdoor.localMembership.release.post, 'POST', `${root}/local-membership/release`],
      [casdoor.localMembership.adopt.post, 'POST', `${root}/local-membership/adopt`],
    ] as const

    expect(Object.keys(casdoor).sort()).toEqual([
      'activate',
      'clearSecret',
      'disable',
      'get',
      'localMembership',
      'permissions',
      'put',
      'resetNamespace',
      'rpLogoutStatus',
      'sync',
      'testLogin',
      'testReauth',
      'testRpLogout',
      'validate',
      'workspaces',
    ])
    for (const [operation, method, path] of expected) {
      expect(operation['~orpc'].route).toMatchObject({ method, path, inputStructure: 'detailed' })
    }
  })

  it('uses a writable-only Secret input while successful configuration DTOs expose only configured state', () => {
    const writable: CasdoorSaveConfigurationPayloadWritable = {
      configuration,
      etag: 4,
      secret: 'synthetic-secret-input',
    }
    const nonWritable: CasdoorSaveConfigurationPayload = { configuration, etag: 4, secret: null }
    expect(zPutSystemManageExtendIntegrationCasdoorBody.parse(writable)).toMatchObject(writable)
    expect(zPutSystemManageExtendIntegrationCasdoorBody.safeParse(nonWritable).success).toBe(true)
    expect(zCasdoorConfigurationResponse.shape).not.toHaveProperty('secret')
    expect(zCasdoorRevisionResponse.shape).toHaveProperty('secret_configured')
    expect(zCasdoorRevisionResponse.shape).not.toHaveProperty('secret')
  })

  it('requires the exact revision and ETag for explicit secret clearing', () => {
    expect(
      zPostSystemManageExtendIntegrationCasdoorClearSecretBody.parse({
        revision_id: revisionId,
        etag: 3,
      }),
    ).toEqual({ revision_id: revisionId, etag: 3 })
    expect(
      zPostSystemManageExtendIntegrationCasdoorClearSecretBody.safeParse({
        etag: 3,
      }).success,
    ).toBe(false)
  })

  it('retains bounded numeric page inputs and independent nullable earliest-workspace metadata', () => {
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.parse({ page: 2, limit: 25 }),
    ).toEqual({ page: 2, limit: 25 })
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.safeParse({ page: '2' }).success,
    ).toBe(false)
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesQuery.safeParse({ limit: 101 }).success,
    ).toBe(false)
    expect(
      zGetSystemManageExtendIntegrationCasdoorWorkspacesResponse.parse({
        page: 2,
        limit: 25,
        total: 0,
        has_more: false,
        workspaces: [],
        earliest_created_workspace: null,
        earliest_created_ambiguous: false,
      }),
    ).toMatchObject({ page: 2, limit: 25, earliest_created_workspace: null })
  })

  it('accepts static validation success without configuration activation fields', () => {
    const parsed = zPostSystemManageExtendIntegrationCasdoorValidateResponse.parse({
      revision_id: revisionId,
      etag: 4,
      checked_at: '2026-10-01T00:00:00Z',
      kind: 'static',
      status: 'passed',
      static_only: true,
      certificate_summaries: [
        {
          fingerprint: 'b'.repeat(64),
          not_before: '2026-09-01T00:00:00Z',
          accept_until: '2026-11-01T00:00:00Z',
        },
      ],
    })
    expect(parsed).toMatchObject({ kind: 'static', status: 'passed', static_only: true })
    expect(parsed).not.toHaveProperty('enabled')
    expect(parsed).not.toHaveProperty('activation_ready')
  })
})
