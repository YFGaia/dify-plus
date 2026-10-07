import type { CasdoorConfiguration } from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { describe, expect, it } from 'vite-plus/test'
import {
  configurationInput,
  initialConfiguration,
  parseServerConfiguration,
  plannedCallback,
  validateConfiguration,
} from '../configuration-draft'
import { safeManagementError } from '../management-error-details'

const workspace = '11111111-1111-4111-8111-111111111111'
const configuration: CasdoorConfiguration = {
  schema_version: 2,
  signing_key_mode: 'automatic',
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
  it('preserves exact role strings and inherits the configured organization for submission', () => {
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
    const draft = {
      ...configuration,
      workspace_mappings: [
        {
          workspace_id: workspace,
          admin: { organization: 'old-org', name: 'CaseSensitive/role' },
          editor: { organization: '', name: 'editor' },
          normal: null,
        },
      ],
    }
    expect(validateConfiguration(draft)).toEqual({})
    expect(configurationInput(draft).workspace_mappings).toEqual([
      {
        workspace_id: workspace,
        admin: { organization: 'exact-org', name: 'CaseSensitive/role' },
        editor: { organization: 'exact-org', name: 'editor' },
        normal: null,
      },
    ])
    expect(draft.workspace_mappings[0]?.admin.organization).toBe('old-org')
  })

  it('rejects duplicate roles after inheriting the sign-in organization', () => {
    expect(
      validateConfiguration({
        ...configuration,
        workspace_mappings: [
          {
            workspace_id: workspace,
            admin: { organization: 'old-org', name: 'same-role' },
            editor: { organization: 'exact-org', name: 'same-role' },
          },
        ],
      }),
    ).toHaveProperty('workspace_mappings.0.editor', 'invalidMapping')
  })
  it('converts legacy configuration into an automatic draft without retaining certificate input', () => {
    const draft = initialConfiguration({
      enabled: true,
      etag: 3,
      active_revision_id: workspace,
      active: {
        revision_id: workspace,
        namespace_id: workspace,
        secret_configured: true,
        configuration: {
          ...configuration,
          schema_version: 1,
          certificates: [
            {
              pem: 'legacy-certificate',
              kid: 'legacy-key',
              not_before: '',
              accept_until: '',
            },
          ],
        },
      },
    })
    expect(draft.schema_version).toBe(2)
    expect(draft).not.toHaveProperty('certificates')
    expect(draft.organization).toBe(configuration.organization)
    expect(validateConfiguration(draft)).toEqual({})
    const input = configurationInput(draft)
    expect(input).not.toHaveProperty('certificates')
    expect(input).not.toHaveProperty('signing_key_mode')
    expect(input).not.toHaveProperty('default_workspace_id')
    expect(input.schema_version).toBe(2)
  })
  it('ignores legacy certificate metadata when shaping a new automatic write', () => {
    const legacy = {
      ...configuration,
      schema_version: 1 as const,
      certificates: [
        {
          pem: 'legacy-certificate',
          kid: null,
          not_before: '',
          accept_until: '',
        },
      ],
    }
    expect(configurationInput(legacy)).not.toHaveProperty('certificates')
    expect(validateConfiguration(legacy)).toEqual({})
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
