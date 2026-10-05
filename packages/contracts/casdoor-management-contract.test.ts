import type { CasdoorConfiguration } from './generated/api/console/system-manage-extend/types.gen'
import { describe, expect, it } from 'vite-plus/test'
import { contractLoaders } from './generated/api/console/orpc.gen'
import {
  zCasdoorConfigurationResponse,
  zCasdoorDraftDiagnosticPreviewResponse,
  zCasdoorTestLoginResponse,
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
  it('loads the twenty-one current management operations through the generated segment', async () => {
    const { systemManageExtend } = await contractLoaders.systemManageExtend()
    const casdoor = systemManageExtend.integration.casdoor
    const prefix = '/system-manage-extend/integration/casdoor'
    const operations = [
      [casdoor.resetNamespace.review.post, 'POST', `${prefix}/reset-namespace/review`],
      [casdoor.resetNamespace.post, 'POST', `${prefix}/reset-namespace`],
      [casdoor.sync.retryTargets.get, 'GET', `${prefix}/sync/retry-targets`],
      [casdoor.sync.retry.post, 'POST', `${prefix}/sync/retry`],
      [casdoor.get, 'GET', prefix],
      [casdoor.put, 'PUT', prefix],
      [casdoor.permissions.get, 'GET', `${prefix}/permissions`],
      [casdoor.clearSecret.post, 'POST', `${prefix}/clear-secret`],
      [casdoor.validate.post, 'POST', `${prefix}/validate`],
      [casdoor.workspaces.get, 'GET', `${prefix}/workspaces`],
      [casdoor.activate.post, 'POST', `${prefix}/activate`],
      [casdoor.disable.post, 'POST', `${prefix}/disable`],
      [casdoor.testLogin.post, 'POST', `${prefix}/test-login`],
      [casdoor.testReauth.post, 'POST', `${prefix}/test-reauth`],
      [casdoor.testRpLogout.post, 'POST', `${prefix}/test-rp-logout`],
      [casdoor.rpLogoutStatus.get, 'GET', `${prefix}/rp-logout-status`],
      [casdoor.localMembership.get, 'GET', `${prefix}/local-membership`],
      [casdoor.localMembership.targets.get, 'GET', `${prefix}/local-membership/targets`],
      [casdoor.localMembership.review.post, 'POST', `${prefix}/local-membership/review`],
      [casdoor.localMembership.release.post, 'POST', `${prefix}/local-membership/release`],
      [casdoor.localMembership.adopt.post, 'POST', `${prefix}/local-membership/adopt`],
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

describe('native diagnostic response contract', () => {
  it('exports started with opaque handoff and constrains safe blocked reason literals', () => {
    expect(
      zCasdoorTestLoginResponse.parse({
        status: 'started',
        reason: null,
        handoff: { handoff_path: `/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}` },
      }).status,
    ).toBe('started')
    expect(
      zCasdoorTestLoginResponse.safeParse({ status: 'blocked', reason: 'provider secret text' })
        .success,
    ).toBe(false)
  })
  it('bounds the generated read-only desired-permission projection and excludes owner targets', () => {
    const preview = {
      revision_id: revisionId,
      namespace_id: revisionId,
      correlation_id: revisionId,
      effective_role_count: 2000,
      stages: [{ stage: 'protocol', status: 'passed' }],
      targets: [{ workspace_id: revisionId, target_role: 'editor', reason: 'role_mapping' }],
    }
    expect(zCasdoorDraftDiagnosticPreviewResponse.safeParse(preview).success).toBe(true)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({ ...preview, effective_role_count: 2001 })
        .success,
    ).toBe(false)
    expect(
      zCasdoorDraftDiagnosticPreviewResponse.safeParse({
        ...preview,
        targets: [{ ...preview.targets[0], target_role: 'owner' }],
      }).success,
    ).toBe(false)
  })
})
