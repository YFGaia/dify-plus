import type { CasdoorConfiguration } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { describe, expect, it } from 'vite-plus/test'
import {
  parseServerConfiguration,
  plannedCallback,
  validateConfiguration,
} from '../configuration-draft'
import { safeManagementError } from '../management-error-details'

const workspace = '11111111-1111-4111-8111-111111111111'
const configuration: CasdoorConfiguration = {
  organization: 'exact-org',
  application: 'app',
  client_id: 'client',
  default_workspace_id: workspace,
  browser_frontend_url: 'https://idp.example.test',
  backend_api_url: 'https://idp.example.test',
  expected_issuer: 'https://idp.example.test',
}

describe('Casdoor draft rules', () => {
  it('rejects duplicate workspace rows and duplicate exact role references across slots', () => {
    const ref = { organization: 'exact-org', name: 'role/with/slashes' }
    const errors = validateConfiguration({
      ...configuration,
      workspace_mappings: [
        { workspace_id: workspace, admin: ref, normal: ref },
        { workspace_id: workspace },
      ],
    })
    expect(errors).toMatchObject({
      'workspace_mappings.0.normal': 'invalidMapping',
      'workspace_mappings.1.workspace_id': 'invalidMapping',
    })
  })
  it('preserves exact role strings while requiring the configured organization', () => {
    expect(
      validateConfiguration({
        ...configuration,
        workspace_mappings: [
          {
            workspace_id: workspace,
            admin: { organization: 'exact-org', name: 'CaseSensitive/role' },
            normal: { organization: 'exact-org', name: 'casesensitive/role' },
          },
        ],
      }),
    ).toEqual({})
    expect(
      validateConfiguration({
        ...configuration,
        workspace_mappings: [
          { workspace_id: workspace, admin: { organization: 'EXACT-org', name: 'role' } },
        ],
      }),
    ).toHaveProperty('workspace_mappings.0.admin', 'invalidMapping')
  })
  it('rejects private keys, duplicate kid and non-UTC or reversed certificate windows', () => {
    expect(
      validateConfiguration({
        ...configuration,
        certificates: [
          {
            pem: '-----BEGIN PRIVATE KEY-----',
            kid: 'shared',
            not_before: '2026-10-02T00:00:00Z',
            accept_until: '2026-10-01T00:00:00Z',
          },
          {
            pem: '-----BEGIN CERTIFICATE-----\nsynthetic\n-----END CERTIFICATE-----',
            kid: 'shared',
            not_before: '2026-10-01T00:00:00+08:00',
            accept_until: '2026-11-01T00:00:00Z',
          },
        ],
      }),
    ).toMatchObject({
      'certificates.0.pem': 'invalidCertificate',
      'certificates.0.accept_until': 'invalidCertificate',
      'certificates.1.kid': 'invalidCertificate',
      'certificates.1.not_before': 'invalidCertificate',
    })
  })
  it('does not manufacture server ETag or revision completeness from schema defaults', () => {
    expect(parseServerConfiguration({ enabled: false, draft: null })).toBeNull()
    expect(
      parseServerConfiguration({
        enabled: false,
        etag: 0,
        draft: null,
        draft_revision_id: workspace,
      }),
    ).toBeNull()
    expect(
      parseServerConfiguration({ enabled: false, etag: 0, draft: null, draft_revision_id: null }),
    ).toMatchObject({ etag: 0 })
  })
  it.each([
    undefined,
    '/console/api',
    'https://user:password@console.example.test/console/api',
    'https://console.example.test/console/api?token=value',
  ])('refuses untrusted or relative callback base %s', (base) => {
    expect(plannedCallback(base)).toBeUndefined()
  })
  it('shows only fixed error codes and UUID correlation IDs, never raw error strings', () => {
    expect(
      safeManagementError({
        data: {
          body: {
            code: 'config_conflict',
            correlation_id: workspace,
            message: 'raw-secret-and-provider-data',
          },
        },
      }),
    ).toEqual({ code: 'config_conflict', correlationId: workspace, unauthorized: false })
    expect(
      safeManagementError({
        code: 'raw-provider-secret',
        correlation_id: 'raw-provider-PII',
        message: 'raw-secret',
      }),
    ).toEqual({ unauthorized: false })
  })
})
