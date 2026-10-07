import type {
  CasdoorConfiguration,
  CasdoorConfigurationResponse,
  CasdoorWorkspacesResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { QueryClient } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { seedCurrentWorkspaceQuery } from '@/test/console/current-workspace'
import { QueryClientTestProvider } from '@/test/console/query-provider'

const { transport } = vi.hoisted(() => ({ transport: vi.fn() }))
vi.mock('@/service/base', () => ({ request: transport }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example.test/console/api' }))
vi.mock('@/env', () => ({
  env: { NEXT_PUBLIC_API_PREFIX: 'https://console.example.test/console/api' },
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: translations } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(translations)
})

const workspaceId = '11111111-1111-4111-8111-111111111111'
const revisionId = '22222222-2222-4222-8222-222222222222'
const nextRevisionId = '33333333-3333-4333-8333-333333333333'
const namespaceId = '44444444-4444-4444-8444-444444444444'
const configuration: CasdoorConfiguration = {
  schema_version: 2,
  application: 'synthetic-app',
  organization: 'synthetic-org',
  client_id: 'synthetic-client',
  backend_api_url: 'https://idp.example.test',
  browser_frontend_url: 'https://idp.example.test',
  expected_issuer: 'https://idp.example.test',
  default_workspace_id: workspaceId,
  button_text: 'Casdoor',
}

function configured(
  overrides: Partial<CasdoorConfigurationResponse> = {},
  config = configuration,
): CasdoorConfigurationResponse {
  const revision = overrides.draft_revision_id ?? revisionId
  return {
    enabled: false,
    etag: 1,
    active: null,
    active_revision_id: null,
    draft_revision_id: revision,
    draft: {
      configuration: config,
      namespace_id: namespaceId,
      revision_id: revision,
      secret_configured: true,
    },
    ...overrides,
  }
}

function workspaces(): CasdoorWorkspacesResponse {
  return {
    page: 1,
    limit: 100,
    total: 1,
    has_more: false,
    earliest_created_ambiguous: false,
    earliest_created_workspace: {
      workspace_id: workspaceId,
      name: 'Synthetic workspace',
      created_at: '2020-01-01T00:00:00Z',
      available: true,
    },
    workspaces: [
      {
        workspace_id: workspaceId,
        name: 'Synthetic workspace',
        created_at: '2020-01-01T00:00:00Z',
        available: true,
      },
    ],
  }
}

function staticPassed(etag: number, revision: string) {
  return {
    certificate_summaries: [],
    kind: 'static',
    status: 'passed',
    static_only: true,
    revision_id: revision,
    etag,
    checked_at: '2026-10-01T00:00:00Z',
    certificates: [],
  }
}

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

let server: unknown
let permission: unknown
let writes: Array<{ method: string; path: string; body: Record<string, unknown> }>
let writeReply: (body: Record<string, unknown>, path: string) => Response | Promise<Response>

beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'error').mockImplementation(() => {})
  server = configured()
  permission = { can_manage_casdoor: true }
  writes = []
  writeReply = (body, path) => {
    if (path.endsWith('/validate')) {
      return json(staticPassed(Number(body.etag), String(body.revision_id)))
    }
    const bodyConfiguration = body.configuration as Partial<CasdoorConfiguration> | undefined
    const saved = configured(
      { etag: 2, draft_revision_id: nextRevisionId },
      { ...configuration, ...bodyConfiguration },
    )
    server = saved
    return json(saved)
  }
  transport.mockImplementation(
    async (_url: string, _init: RequestInit, options: { request: Request }) => {
      const request = options.request
      const url = new URL(request.url)
      if (request.method === 'GET') {
        if (url.pathname.endsWith('/system-manage-extend/permissions'))
          return json({
            can_manage_system: true,
            workspace_id: workspaceId,
            account_id: '55555555-5555-4555-8555-555555555555',
          })
        if (url.pathname.endsWith('/permissions')) return json(permission)
        if (url.pathname.endsWith('/workspaces')) return json(workspaces())
        return json(server)
      }
      const body = (await request.json()) as Record<string, unknown>
      writes.push({ method: request.method, path: url.pathname, body })
      return writeReply(body, url.pathname)
    },
  )
})

async function mount(role: 'owner' | 'admin' | 'normal' = 'admin') {
  const { CasdoorConfigurationForm } = await import('../index')
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: Infinity } },
  })
  seedCurrentWorkspaceQuery(client, { role, id: workspaceId })
  seedAccountProfileQuery(client, { id: '55555555-5555-4555-8555-555555555555' })
  const view = render(
    <QueryClientTestProvider queryClient={client}>
      <CasdoorConfigurationForm />
    </QueryClientTestProvider>,
  )
  return { client, ...view }
}

async function ready() {
  const result = await mount()
  await screen.findByLabelText('Casdoor organization')
  return result
}

async function readyAdvanced() {
  const result = await ready()
  await userEvent.setup().click(screen.getByText('Advanced settings'))
  return result
}

async function fillRequiredFields(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Casdoor URL'), 'https://idp.example.test')
  await user.type(screen.getByLabelText('Casdoor organization'), 'synthetic-org')
  await user.type(screen.getByLabelText('Casdoor application'), 'synthetic-app')
  await user.type(screen.getByLabelText('Client ID'), 'synthetic-client')
}

describe('Casdoor configuration workflows', () => {
  it('shows role mappings as basic settings and saves role names in the sign-in organization', async () => {
    server = configured(
      {},
      {
        ...configuration,
        workspace_mappings: [
          {
            workspace_id: workspaceId,
            admin: { organization: 'synthetic-org', name: 'pi-agent-admin' },
          },
        ],
      },
    )
    await ready()
    const user = userEvent.setup()
    const admin = within(screen.getByRole('group', { name: 'Administrator (admin)' }))
    const editor = within(screen.getByRole('group', { name: 'Editor (editor)' }))
    const normal = within(screen.getByRole('group', { name: 'Normal member (normal)' }))
    expect(admin.getByRole('textbox', { name: 'Exact role name' })).toHaveValue('pi-agent-admin')
    expect(screen.queryByLabelText('Exact role organization')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Server API URL')).not.toBeInTheDocument()
    await user.type(editor.getByRole('textbox', { name: 'Exact role name' }), 'pi-agent-editor')
    await user.type(normal.getByRole('textbox', { name: 'Exact role name' }), 'pi-agent-member')
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'new-org')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Configuration saved and checked.')
    expect(writes[0]?.body.configuration).toMatchObject({
      organization: 'new-org',
      workspace_mappings: [
        {
          workspace_id: workspaceId,
          admin: { organization: 'new-org', name: 'pi-agent-admin' },
          editor: { organization: 'new-org', name: 'pi-agent-editor' },
          normal: { organization: 'new-org', name: 'pi-agent-member' },
        },
      ],
    })
  })

  it('clears a role mapping when its name is cleared', async () => {
    server = configured(
      {},
      {
        ...configuration,
        workspace_mappings: [
          {
            workspace_id: workspaceId,
            admin: { organization: 'synthetic-org', name: 'pi-agent-admin' },
          },
        ],
      },
    )
    await ready()
    const user = userEvent.setup()
    const admin = within(screen.getByRole('group', { name: 'Administrator (admin)' }))
    await user.clear(admin.getByRole('textbox', { name: 'Exact role name' }))
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Configuration saved and checked.')
    expect(writes[0]?.body.configuration).toMatchObject({
      workspace_mappings: [{ workspace_id: workspaceId, admin: null }],
    })
  })

  it('shows duplicate role errors in basic settings without opening advanced settings', async () => {
    server = configured(
      {},
      {
        ...configuration,
        workspace_mappings: [
          {
            workspace_id: workspaceId,
            admin: { organization: 'synthetic-org', name: 'pi-agent-admin' },
          },
        ],
      },
    )
    await ready()
    const user = userEvent.setup()
    const editor = within(screen.getByRole('group', { name: 'Editor (editor)' }))
    await user.type(editor.getByRole('textbox', { name: 'Exact role name' }), 'pi-agent-admin')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    expect(editor.getByRole('textbox', { name: 'Exact role name' })).toHaveAttribute(
      'aria-invalid',
      'true',
    )
    expect(screen.queryByLabelText('Server API URL')).not.toBeInTheDocument()
    expect(writes).toHaveLength(0)
  })

  it.each([{ can_manage_casdoor: false }, { can_manage_casdoor: 'true' }])(
    'does not show the form without confirmed management permission (%j)',
    async (value) => {
      permission = value
      await mount()
      await screen.findByRole('alert')
      expect(screen.queryByLabelText('Casdoor organization')).not.toBeInTheDocument()
      expect(transport).toHaveBeenCalledTimes(2)
    },
  )

  it('shows the compact configuration first and keeps workspace selection implicit', async () => {
    server = {
      enabled: false,
      etag: 0,
      active: null,
      active_revision_id: null,
      draft: null,
      draft_revision_id: null,
    }
    await ready()
    expect(screen.getByRole('switch', { name: 'Enable' })).not.toBeChecked()
    expect(screen.getByText('Disabled')).toBeInTheDocument()
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('')
    expect(screen.getByLabelText('Client ID')).toHaveValue('')
    expect(screen.getByLabelText('Client Secret')).toHaveValue('')
    expect(screen.getByLabelText('Casdoor URL')).toHaveValue('')
    expect(screen.queryByRole('combobox', { name: 'Default workspace' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Server API URL')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Expected issuer')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Test login' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Validate configuration' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Clear saved draft Secret' }),
    ).not.toBeInTheDocument()
  })

  it('has no certificate or key controls in either configuration mode', async () => {
    await ready()
    expect(
      screen.getByText('The system automatically manages sign-in verification.'),
    ).toBeInTheDocument()
    const assertNoCertificateControls = () => {
      expect(
        screen.queryByLabelText(/certificate|PEM|kid|Valid from|Accept until/i),
      ).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /certificate/i })).not.toBeInTheDocument()
    }
    assertNoCertificateControls()
    await userEvent.setup().click(screen.getByText('Advanced settings'))
    assertNoCertificateControls()
  })

  it('saves an unchanged legacy draft as automatic before testing login and preserves its active revision', async () => {
    const legacy = configured(
      {},
      {
        ...configuration,
        schema_version: 1,
        certificates: [
          {
            pem: '-----BEGIN CERTIFICATE-----\nlegacy\n-----END CERTIFICATE-----',
            kid: 'legacy-key',
            not_before: '2020-01-01T00:00:00Z',
            accept_until: '2030-01-01T00:00:00Z',
          },
        ],
      },
    )
    server = { ...legacy, enabled: true, active: legacy.draft, active_revision_id: revisionId }
    writeReply = (body, path) => {
      if (path.endsWith('/test-login'))
        return json({ status: 'blocked', reason: 'deployment_proof_missing' })
      return json(
        configured(
          {
            etag: 2,
            draft_revision_id: nextRevisionId,
            enabled: true,
            active: legacy.draft,
            active_revision_id: revisionId,
          },
          { ...configuration, ...(body.configuration as Partial<CasdoorConfiguration>) },
        ),
      )
    }
    await ready()
    expect(
      screen.getByText('Save and test login to switch this configuration to automatic management.'),
    ).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Test login' }))
    await screen.findByRole('alert')
    expect(writes.map(({ method }) => method)).toEqual(['PUT', 'POST'])
    expect(writes[0]?.body.configuration).toMatchObject({ schema_version: 2 })
    expect(writes[0]?.body.configuration).not.toHaveProperty('certificates')
    expect(writes[0]?.body).not.toHaveProperty('secret')
    expect(writes[1]?.body).toEqual({ etag: 2, revision_id: nextRevisionId })
    expect(screen.getByRole('switch', { name: 'Enable' })).toBeChecked()
    expect(writes.some(({ path }) => path.endsWith('/activate'))).toBe(false)
  })

  it('saves once, then automatically validates the saved revision', async () => {
    server = {
      enabled: false,
      etag: 0,
      active: null,
      active_revision_id: null,
      draft: null,
      draft_revision_id: null,
    }
    await ready()
    const user = userEvent.setup()
    await fillRequiredFields(user)
    await user.type(screen.getByLabelText('Client Secret'), 'synthetic-secret')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))

    await screen.findByText('Configuration saved and checked.')
    expect(writes.map(({ method, path }) => [method, path])).toEqual([
      ['PUT', '/console/api/system-manage-extend/integration/casdoor'],
      ['POST', '/console/api/system-manage-extend/integration/casdoor/validate'],
    ])
    expect(writes[0]?.body).toMatchObject({ etag: 0, secret: 'synthetic-secret' })
    expect(writes[1]?.body).toEqual({ etag: 2, revision_id: nextRevisionId })
    expect(writes[0]?.body.configuration).not.toHaveProperty('default_workspace_id')
    expect(writes[0]?.body.configuration).toMatchObject({ schema_version: 2 })
    expect(writes[0]?.body.configuration).not.toHaveProperty('certificates')
    expect(screen.getByLabelText('Client Secret')).toHaveValue('')
  })

  it('shows a safe error when the backend rejects an initial setup without a Secret', async () => {
    server = {
      enabled: false,
      etag: 0,
      active: null,
      active_revision_id: null,
      draft: null,
      draft_revision_id: null,
    }
    await ready()
    const user = userEvent.setup()
    await fillRequiredFields(user)
    writeReply = () => json({ code: 'client_secret_required', correlation_id: namespaceId }, 422)
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByRole('alert')
    expect(writes).toHaveLength(1)
    expect(writes[0]?.body).not.toHaveProperty('secret')
    expect(writes.some(({ path }) => path.endsWith('/validate'))).toBe(false)
    expect(screen.getByLabelText('Client Secret')).toBeInTheDocument()
  })

  it('validates the current saved revision without issuing an unnecessary PUT', async () => {
    await ready()
    writeReply = (_body, path) => {
      expect(path).toMatch(/\/validate$/)
      return json(staticPassed(1, revisionId))
    }
    await userEvent.setup().click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Configuration saved and checked.')
    expect(writes).toMatchObject([
      {
        method: 'POST',
        path: '/console/api/system-manage-extend/integration/casdoor/validate',
        body: { etag: 1, revision_id: revisionId },
      },
    ])
  })

  it('retains advanced values when collapsed and omits the implicit workspace from saves', async () => {
    await readyAdvanced()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Server API URL'))
    await user.type(screen.getByLabelText('Server API URL'), 'https://internal-idp.example.test')
    await user.click(screen.getByText('Advanced settings'))
    expect(screen.queryByLabelText('Server API URL')).not.toBeInTheDocument()
    await user.click(screen.getByText('Advanced settings'))
    expect(screen.getByLabelText('Server API URL')).toHaveValue('https://internal-idp.example.test')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Configuration saved and checked.')
    const payload = writes[0]?.body.configuration as Record<string, unknown>
    expect(payload.backend_api_url).toBe('https://internal-idp.example.test')
    expect(payload).not.toHaveProperty('default_workspace_id')
  })

  it('saves dirty edits before starting login and uses the newly saved revision', async () => {
    const user = userEvent.setup()
    const navigation = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    writeReply = (body, path) => {
      if (path.endsWith('/test-login')) {
        return json({
          status: 'started',
          reason: null,
          handoff: {
            handoff_path: `/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}`,
          },
        })
      }
      const updated = configured(
        { etag: 2, draft_revision_id: nextRevisionId },
        { ...configuration, ...(body.configuration as Partial<CasdoorConfiguration>) },
      )
      server = updated
      return json(updated)
    }
    await ready()
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'changed-org')
    await user.click(screen.getByRole('button', { name: 'Test login' }))
    await waitFor(() => expect(navigation).toHaveBeenCalledOnce())
    expect(writes.map(({ method, path }) => [method, path])).toEqual([
      ['PUT', '/console/api/system-manage-extend/integration/casdoor'],
      ['POST', '/console/api/system-manage-extend/integration/casdoor/test-login'],
    ])
    expect(writes[1]?.body).toEqual({ etag: 2, revision_id: nextRevisionId })
    expect(writes[0]?.body.configuration).toMatchObject({ organization: 'changed-org' })
    expect(navigation).toHaveBeenCalledWith(
      `https://console.example.test/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}`,
    )
    navigation.mockRestore()
  })

  it('does not activate an edited draft and keeps the switch off until the server confirms', async () => {
    await ready()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'unsaved-org')
    await user.click(screen.getByRole('switch', { name: 'Enable' }))
    expect(screen.getByRole('switch', { name: 'Enable' })).not.toBeChecked()
    expect(
      screen.getByText(
        'Save the configuration and test login before enabling Casdoor or applying changes.',
      ),
    ).toBeInTheDocument()
    expect(writes).toHaveLength(0)
  })

  it('reflects enabled state only after the activate endpoint confirms it', async () => {
    await ready()
    writeReply = () =>
      json(
        configured({
          enabled: true,
          etag: 2,
          active: { ...configured().draft!, revision_id: revisionId },
          active_revision_id: revisionId,
        }),
      )
    const user = userEvent.setup()
    await user.click(screen.getByRole('switch', { name: 'Enable' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Enable' })).toBeChecked())
    expect(screen.getByText('Enabled')).toBeInTheDocument()
    expect(writes).toMatchObject([
      {
        method: 'POST',
        path: '/console/api/system-manage-extend/integration/casdoor/activate',
        body: { etag: 1, revision_id: revisionId },
      },
    ])
  })

  it('leaves enabled off after activation is rejected and gives the login-test gate', async () => {
    await ready()
    writeReply = () => json({ code: 'config_conflict', correlation_id: namespaceId }, 409)
    const user = userEvent.setup()
    await user.click(screen.getByRole('switch', { name: 'Enable' }))
    await screen.findByText(
      'Save the configuration and test login before enabling Casdoor or applying changes.',
    )
    expect(screen.getByRole('switch', { name: 'Enable' })).not.toBeChecked()
    expect(screen.getByText('Disabled')).toBeInTheDocument()
    expect(writes[0]).toMatchObject({
      path: '/console/api/system-manage-extend/integration/casdoor/activate',
    })
  })

  it('turns off the switch only after the disable endpoint confirms', async () => {
    const draft = configured().draft!
    server = configured({ enabled: true, active: draft, active_revision_id: revisionId })
    await ready()
    writeReply = () =>
      json({
        configuration: configured({
          enabled: false,
          etag: 2,
          active: draft,
          active_revision_id: revisionId,
        }),
        reconciliation_required: false,
      })
    const user = userEvent.setup()
    await user.click(screen.getByRole('switch', { name: 'Enable' }))
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Enable' })).not.toBeChecked())
    expect(screen.getByText('Disabled')).toBeInTheDocument()
    expect(writes[0]).toMatchObject({
      method: 'POST',
      path: '/console/api/system-manage-extend/integration/casdoor/disable',
      body: { etag: 1 },
    })
  })

  it('blocks stale writes after a conflict, clears the transient secret, and offers reload', async () => {
    await ready()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'locally-edited-org')
    await user.type(screen.getByLabelText('Client Secret'), 'synthetic-private-secret')
    writeReply = () => json({ code: 'config_conflict', correlation_id: namespaceId }, 409)
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Client Secret')).toHaveValue('')
    expect(screen.queryByText(/synthetic-private-secret/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save configuration' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Reload configuration' })).toBeEnabled()
    expect(writes).toHaveLength(1)
  })
})

it('can retry a Casdoor permission failure after global access has been confirmed', async () => {
  transport
    .mockImplementationOnce(async () =>
      json({
        can_manage_system: true,
        workspace_id: workspaceId,
        account_id: '55555555-5555-4555-8555-555555555555',
      }),
    )
    .mockRejectedValueOnce(new Error('Synthetic permission request failed'))
  await mount()
  await userEvent.setup().click(await screen.findByRole('button', { name: 'Retry' }))
  await screen.findByLabelText('Casdoor organization')
})
