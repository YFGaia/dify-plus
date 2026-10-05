import type {
  CasdoorConfiguration,
  CasdoorConfigurationResponse,
  CasdoorWorkspacesResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { dehydrate, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { seedAccountProfileQuery } from '@/test/console/account-profile'

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
const nextRevision = '33333333-3333-4333-8333-333333333333'
const namespaceId = '44444444-4444-4444-8444-444444444444'
const configuration: CasdoorConfiguration = {
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
): CasdoorConfigurationResponse {
  return {
    enabled: false,
    etag: 1,
    active: null,
    active_revision_id: null,
    draft_revision_id: revisionId,
    draft: {
      configuration,
      namespace_id: namespaceId,
      revision_id: revisionId,
      secret_configured: true,
    },
    ...overrides,
  }
}
function workspaces(page = 1): CasdoorWorkspacesResponse {
  return {
    page,
    limit: 100,
    total: 101,
    has_more: page === 1,
    earliest_created_ambiguous: true,
    earliest_created_workspace: {
      workspace_id: workspaceId,
      name: 'Earliest synthetic workspace',
      created_at: '2020-01-01T00:00:00Z',
      available: true,
    },
    workspaces:
      page === 1
        ? [
            {
              workspace_id: workspaceId,
              name: 'Synthetic workspace',
              created_at: '2020-01-01T00:00:00Z',
              available: true,
            },
          ]
        : [],
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
  writeReply = () =>
    json(
      configured({
        etag: 2,
        draft_revision_id: nextRevision,
        draft: {
          configuration,
          namespace_id: namespaceId,
          revision_id: nextRevision,
          secret_configured: true,
        },
      }),
    )
  transport.mockImplementation(
    async (_url: string, _init: RequestInit, options: { request: Request }) => {
      const request = options.request
      const url = new URL(request.url)
      if (request.method === 'GET') {
        if (url.pathname.endsWith('/permissions')) return json(permission)
        if (url.pathname.endsWith('/workspaces'))
          return json(workspaces(Number(url.searchParams.get('page'))))
        return json(server)
      }
      const body = (await request.json()) as Record<string, unknown>
      writes.push({ method: request.method, path: url.pathname, body })
      return writeReply(body, url.pathname)
    },
  )
})

async function mount() {
  const { CasdoorConfigurationForm } = await import('../index')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const view = render(
    <QueryClientProvider client={client}>
      <CasdoorConfigurationForm />
    </QueryClientProvider>,
  )
  return { client, ...view }
}
async function ready() {
  const result = await mount()
  await screen.findByLabelText('Casdoor organization')
  await screen.findByText('Page 1 · 101 workspaces')
  return result
}

describe('Casdoor configuration form through generated Console APIs', () => {
  it.each([{ can_manage_casdoor: false }, { can_manage_casdoor: 'true' }])(
    'fails closed for permission %j without fetching configuration',
    async (value) => {
      permission = value
      await mount()
      await screen.findByRole('alert')
      expect(screen.queryByLabelText('Casdoor organization')).not.toBeInTheDocument()
      expect(transport).toHaveBeenCalledOnce()
    },
  )

  it('starts with empty credentials and no guessed default workspace', async () => {
    server = {
      enabled: false,
      etag: 0,
      active: null,
      draft: null,
      active_revision_id: null,
      draft_revision_id: null,
    }
    await ready()
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('')
    expect(screen.getByLabelText('Client ID')).toHaveValue('')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
    expect(
      screen.getByRole('combobox', { name: 'Default workspace (required fixed UUID)' }),
    ).toHaveTextContent('Choose a workspace')
    expect(screen.getByText(/earliest creation time is tied/)).toBeInTheDocument()
    expect(screen.getByLabelText('Planned sign-in callback')).toHaveValue(
      'https://console.example.test/console/api/auth/casdoor/callback',
    )
    expect(screen.getByText(/callback is unavailable until sign-in/)).toBeInTheDocument()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(writes).toHaveLength(0)
    expect(screen.getAllByText('Check this field.').length).toBeGreaterThan(0)
  })

  it('submits through the native form using Enter from a labeled input', async () => {
    await ready()
    await userEvent.setup().type(screen.getByLabelText('Sign-in button text'), ' keyboard{Enter}')
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0]?.body.configuration).toMatchObject({ button_text: 'Casdoor keyboard' })
  })

  it('requires complete server version metadata before exposing writable controls', async () => {
    server = { enabled: false, draft: null, active: null }
    await mount()
    await screen.findByText('Casdoor configuration could not be loaded.')
    expect(screen.queryByRole('button', { name: 'Save draft' })).not.toBeInTheDocument()
    expect(writes).toHaveLength(0)
  })

  it('allows only two certificate rows and retains the remaining controlled draft when one is removed', async () => {
    await ready()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Add public certificate' }))
    await user.click(screen.getByRole('button', { name: 'Add public certificate' }))
    const second = screen.getByRole('group', { name: 'Certificate 2' })
    await user.type(within(second).getByLabelText('kid (optional)'), 'remaining-kid')
    expect(screen.getByRole('button', { name: 'Add public certificate' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Remove certificate 1' }))
    expect(screen.getByLabelText('kid (optional)')).toHaveValue('remaining-kid')
    expect(screen.getByRole('button', { name: 'Add public certificate' })).not.toBeDisabled()
  })

  it('renders exactly three role slots and associates duplicate-reference errors with their inputs', async () => {
    await ready()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Add workspace mapping' }))
    const row = screen.getByRole('group', { name: 'Workspace mapping 1' })
    await user.click(within(row).getByRole('combobox', { name: 'Target workspace' }))
    await user.click(await screen.findByRole('option', { name: /Synthetic workspace/ }))
    for (const name of ['Administrator (admin)', 'Normal member (normal)']) {
      const slot = within(row).getByRole('group', { name })
      await user.type(within(slot).getByLabelText('Exact role organization'), 'synthetic-org')
      await user.type(within(slot).getByLabelText('Exact role name'), 'same/exact-role')
    }
    expect(within(row).getByRole('group', { name: 'Editor (editor)' })).toBeInTheDocument()
    expect(within(row).queryByRole('group', { name: /owner/i })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    const normal = within(row).getByRole('group', { name: 'Normal member (normal)' })
    expect(within(normal).getByLabelText('Exact role name')).toHaveAttribute('aria-invalid', 'true')
    expect(within(normal).getByLabelText('Exact role name')).toHaveAccessibleDescription(
      /unique workspace and exact organization\/role pair/,
    )
    expect(writes).toHaveLength(0)
  })

  it('preserves dirty fields and Secret across a newer refetch until confirmed re-edit', async () => {
    await ready()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'local-edited-org')
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-local-secret')
    server = configured({
      etag: 2,
      draft_revision_id: nextRevision,
      draft: {
        configuration: { ...configuration, organization: 'remote-org' },
        namespace_id: namespaceId,
        revision_id: nextRevision,
        secret_configured: true,
      },
    })
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await screen.findByText('Server ETag: 2')
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('local-edited-org')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('synthetic-local-secret')
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Clear saved draft Secret' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('local-edited-org')
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('remote-org')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
  })

  it('keeps the mounted dirty session when a refetch returns malformed server data', async () => {
    await ready()
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Sign-in button text'), ' locally edited')
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-preserved-secret')
    server = { enabled: false, etag: 'bad-etag', draft: 'bad-draft' }
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Sign-in button text')).toHaveValue('Casdoor locally edited')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('synthetic-preserved-secret')
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(screen.queryByText(/bad-etag|bad-draft/)).not.toBeInTheDocument()
  })

  it('saves the current ETag, clears transient Secret, preserves blank-keep and shared query invalidation', async () => {
    const { client } = await ready()
    const user = userEvent.setup()
    writeReply = (body) => {
      const etag = Number(body.etag) + 1
      server = configured({
        etag,
        draft_revision_id: nextRevision,
        draft: {
          configuration: body.configuration as CasdoorConfiguration,
          namespace_id: namespaceId,
          revision_id: nextRevision,
          secret_configured: true,
        },
      })
      return json(server)
    }
    await user.type(screen.getByLabelText('Sign-in button text'), ' updated')
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-replacement-secret')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved. SSO activation has not changed.')
    expect(writes[0]).toMatchObject({
      method: 'PUT',
      path: '/console/api/system-manage-extend/integration/casdoor',
      body: { etag: 1, secret: 'synthetic-replacement-secret' },
    })
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
    expect(JSON.stringify(dehydrate(client))).not.toContain('synthetic-replacement-secret')
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0))
    await screen.findByText('Server ETag: 2')
    await user.type(screen.getByLabelText('Sign-in button text'), ' again')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(writes).toHaveLength(2))
    expect(writes[1]?.body).toHaveProperty('etag', 2)
    expect(writes[1]?.body).not.toHaveProperty('secret')
    expect(screen.getByText('SSO disabled')).toBeInTheDocument()
  })

  it.each(['conflict', 'malformed'] as const)(
    'blocks further writes after an unconfirmed %s until refresh and re-edit',
    async (failure) => {
      await ready()
      const user = userEvent.setup()
      writeReply = () =>
        failure === 'conflict'
          ? json(
              {
                code: 'config_conflict',
                correlation_id: namespaceId,
                message: 'unsafe-provider-details',
              },
              409,
            )
          : json({ enabled: false, etag: 2, draft: 'bad-draft' })
      await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-failed-secret')
      await user.click(screen.getByRole('button', { name: 'Save draft' }))
      await screen.findByRole('alert')
      expect(screen.queryByText('unsafe-provider-details')).not.toBeInTheDocument()
      expect(screen.queryByText('bad-draft')).not.toBeInTheDocument()
      expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
      expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
      await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Refresh server state' })).not.toHaveAttribute(
          'aria-disabled',
          'true',
        ),
      )
      expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
      await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
      expect(screen.getByRole('button', { name: 'Save draft' })).not.toBeDisabled()
      expect(writes).toHaveLength(1)
    },
  )

  it('clears only the confirmed saved revision and preserves active state', async () => {
    const active = configured().draft
    server = configured({ enabled: true, active, active_revision_id: revisionId })
    await ready()
    const user = userEvent.setup()
    writeReply = () => {
      server = configured({
        enabled: true,
        active,
        active_revision_id: revisionId,
        etag: 2,
        draft_revision_id: nextRevision,
        draft: {
          configuration,
          namespace_id: namespaceId,
          revision_id: nextRevision,
          secret_configured: false,
        },
      })
      return json(server)
    }
    await user.click(screen.getByRole('button', { name: 'Clear saved draft Secret' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }),
    )
    expect(writes).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Clear saved draft Secret' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    await screen.findByText('The saved draft Secret was cleared.')
    expect(writes[0]).toMatchObject({ method: 'POST', body: { etag: 1, revision_id: revisionId } })
    expect(writes[0]?.path).toBe(
      '/console/api/system-manage-extend/integration/casdoor/clear-secret',
    )
    expect(screen.getByText('SSO enabled')).toBeInTheDocument()
    expect(screen.getByText(`Active revision: ${revisionId}`)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Clear saved draft Secret' })).toBeDisabled()
  })

  it('disables only after confirmation and reports unresolved remote reconciliation', async () => {
    const active = configured().draft
    server = configured({
      enabled: true,
      etag: 1,
      active,
      active_revision_id: revisionId,
      draft: null,
      draft_revision_id: null,
    })
    writeReply = () => {
      server = configured({
        enabled: false,
        etag: 2,
        active,
        active_revision_id: revisionId,
        draft: null,
        draft_revision_id: null,
      })
      return json({ configuration: server, reconciliation_required: true })
    }
    await ready()
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Disable SSO' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Disable Casdoor SSO?' })
    expect(within(dialog).getByText(/blocks new Casdoor sign-ins/i)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    await screen.findAllByText('SSO disabled')
    expect(
      await screen.findByText(/remote role or resource operations may remain unresolved/i),
    ).toBeInTheDocument()
    expect(writes.at(-1)).toMatchObject({
      method: 'POST',
      path: '/console/api/system-manage-extend/integration/casdoor/disable',
      body: { etag: 1 },
    })
    expect(screen.queryByRole('button', { name: 'Disable SSO' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    expect(
      screen.getByText(/remote role or resource operations may remain unresolved/i),
    ).toBeInTheDocument()
  })

  it('closes a rejected clear confirmation, shows the safe error and blocks subsequent changes', async () => {
    await ready()
    writeReply = () => json({ code: 'config_conflict', correlation_id: namespaceId }, 409)
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Clear saved draft Secret' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    await screen.findByRole('alert')
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(writes[0]?.body).toEqual({ etag: 1, revision_id: revisionId })
  })

  it('shows exact static-only success without enabling SSO', async () => {
    await ready()
    writeReply = () => json(staticResult())
    await userEvent
      .setup()
      .click(screen.getByRole('button', { name: 'Check saved draft (static only)' }))
    await screen.findByText(/Static checks passed for revision/)
    expect(writes[0]?.body).toEqual({ etag: 1, revision_id: revisionId })
    expect(screen.getByText('SSO disabled')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Activate SSO' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test sign-in' })).toBeInTheDocument()
  })

  it('keeps activation and test sign-in blocked when deployment proof is missing', async () => {
    await ready()
    writeReply = (_body, path) =>
      path.endsWith('/test-login')
        ? json({ status: 'blocked', reason: 'deployment_proof_missing' })
        : json(
            {
              code: 'config_conflict',
              reason: 'deployment_proof_missing',
              correlation_id: namespaceId,
            },
            409,
          )
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Test sign-in' }))
    await screen.findAllByText(/Real deployment evidence is required/)
    expect(writes.at(-1)).toMatchObject({
      method: 'POST',
      path: '/console/api/system-manage-extend/integration/casdoor/test-login',
      body: { etag: 1, revision_id: revisionId },
    })

    await user.click(screen.getByRole('button', { name: 'Activate SSO' }))
    const dialog = await screen.findByRole('alertdialog', {
      name: 'Activate this Casdoor configuration?',
    })
    const confirm = within(dialog).getByRole('button', { name: 'Confirm' })
    await waitFor(() => expect(confirm).toBeEnabled())
    await user.click(confirm)
    await waitFor(() =>
      expect(writes.at(-1)?.path).toBe(
        '/console/api/system-manage-extend/integration/casdoor/activate',
      ),
    )
    expect(writes.at(-1)).toMatchObject({
      method: 'POST',
      path: '/console/api/system-manage-extend/integration/casdoor/activate',
      body: { etag: 1, revision_id: revisionId },
    })
    expect(screen.getByText('SSO disabled')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Activate SSO' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Refresh server state' })).toBeEnabled()
  })

  it('can activate a saved new draft while the previous revision is still enabled', async () => {
    const draft = configured().draft!
    server = configured({
      enabled: true,
      active: { ...draft, revision_id: nextRevision },
      active_revision_id: nextRevision,
    })
    await ready()
    writeReply = () => {
      server = configured({ enabled: true, etag: 2, active: draft, active_revision_id: revisionId })
      return json(server)
    }
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Activate SSO' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    await screen.findByText('Casdoor SSO is active.')
    expect(writes).toHaveLength(1)
    expect(writes[0]?.body).toEqual({ etag: 1, revision_id: revisionId })
    expect(screen.getByText(`Active revision: ${revisionId}`)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Activate SSO' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Disable SSO' })).toBeEnabled()
  })

  it('blocks draft writes while a sign-in test request is pending', async () => {
    await ready()
    let finish!: (response: Response) => void
    writeReply = () =>
      new Promise<Response>((resolve) => {
        finish = resolve
      })
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Test sign-in' }))
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Activate SSO' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(writes).toHaveLength(1)
    finish(json({ status: 'blocked', reason: 'live_test_not_wired' }))
    await screen.findByText(/Live sign-in testing is unavailable/)
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeEnabled()
  })

  it.each(['disabled', 'wrong-revision', 'stale-etag', 'no-revision'] as const)(
    'does not announce activation from a %s response',
    async (failure) => {
      await ready()
      const draft = configured().draft!
      writeReply = () =>
        json(
          configured({
            enabled: failure !== 'disabled',
            etag: failure === 'stale-etag' ? 1 : 2,
            active_revision_id:
              failure === 'no-revision'
                ? null
                : failure === 'wrong-revision'
                  ? nextRevision
                  : revisionId,
            active:
              failure === 'no-revision'
                ? null
                : {
                    ...draft,
                    revision_id: failure === 'wrong-revision' ? nextRevision : revisionId,
                  },
            ...(failure === 'no-revision' ? { draft: null, draft_revision_id: null } : {}),
          }),
        )
      const user = userEvent.setup()
      await user.click(screen.getByRole('button', { name: 'Activate SSO' }))
      await user.click(
        within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
      )
      await screen.findByRole('alert')
      expect(screen.queryByText('Casdoor SSO is active.')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Activate SSO' })).toBeDisabled()
    },
  )

  it.each(['still-enabled', 'missing-enabled', 'stale-etag'] as const)(
    'does not announce disable from a %s response',
    async (failure) => {
      server = configured({
        enabled: true,
        active: configured().draft,
        active_revision_id: revisionId,
      })
      await ready()
      writeReply = () => {
        const result = configured({
          enabled: failure === 'still-enabled',
          etag: failure === 'stale-etag' ? 1 : 2,
          active: configured().draft,
          active_revision_id: revisionId,
        })
        if (failure === 'missing-enabled') delete result.enabled
        return json({
          configuration: result,
          reconciliation_required: false,
        })
      }
      const user = userEvent.setup()
      await user.click(screen.getByRole('button', { name: 'Disable SSO' }))
      await user.click(
        within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
      )
      await screen.findByRole('alert')
      expect(screen.queryByText('SSO disabled')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Disable SSO' })).toBeDisabled()
    },
  )

  it.each(['kind', 'status', 'static_only', 'revision_id', 'etag'])(
    'rejects missing or mismatched static %s without declaring passed',
    async (field) => {
      await ready()
      const result: Record<string, unknown> = staticResult()
      if (field === 'revision_id') result[field] = nextRevision
      else if (field === 'etag') result[field] = 2
      else delete result[field]
      writeReply = () => json(result)
      await userEvent
        .setup()
        .click(screen.getByRole('button', { name: 'Check saved draft (static only)' }))
      await screen.findByRole('alert')
      expect(screen.queryByText(/Static checks passed for revision/)).not.toBeInTheDocument()
    },
  )

  it('uses numeric 100-item pagination and retains an off-page selected UUID', async () => {
    await ready()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Next workspace page' }))
    await screen.findByText('Page 2 · 101 workspaces')
    expect(
      screen.getByRole('combobox', { name: 'Default workspace (required fixed UUID)' }),
    ).toHaveTextContent(workspaceId)
    expect(
      screen.getByText(/outside the current page; this does not mean it is unavailable/),
    ).toBeInTheDocument()
    const requests = transport.mock.calls.map((call) => call[2].request as Request)
    const pageRequest = requests.find(
      (request) => new URL(request.url).searchParams.get('page') === '2',
    )
    expect(new URL(pageRequest?.url ?? '').searchParams.get('limit')).toBe('100')
    expect(writes).toHaveLength(0)
  })

  it('binds avatar mode independently and keeps optional capabilities off in the saved payload', async () => {
    await ready()
    const user = userEvent.setup()
    await user.click(screen.getByRole('combobox', { name: 'Avatar synchronization' }))
    await user.click(await screen.findByRole('option', { name: 'Managed synchronization' }))
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(writes).toHaveLength(1))
    expect(writes[0]?.body.configuration).toMatchObject({
      avatar_sync: true,
      avatar_mode: 'managed',
      name_sync: 'fill_empty',
      rp_logout: false,
      self_unlink: false,
    })
  })
})

function staticResult() {
  return {
    etag: 1,
    revision_id: revisionId,
    kind: 'static',
    status: 'passed',
    static_only: true,
    checked_at: '2026-10-01T00:00:00Z',
    certificate_summaries: [
      {
        fingerprint: 'a'.repeat(64),
        kid: null,
        not_before: '2026-09-01T00:00:00Z',
        accept_until: '2026-11-01T00:00:00Z',
      },
    ],
  }
}

describe('live diagnostic navigation and return refresh', () => {
  it('runs optional recent-authentication validation against the exact saved draft and safe local handler', async () => {
    const user = userEvent.setup()
    const navigation = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    const path = `/console/api/auth/casdoor/identity/${'A'.repeat(43)}`
    writeReply = () => json({ handoff_path: path })
    await ready()
    await user.click(screen.getByRole('button', { name: 'Test recent authentication' }))
    await waitFor(() =>
      expect(navigation).toHaveBeenCalledWith(`https://console.example.test${path}`),
    )
    expect(writes[0]).toMatchObject({
      path: '/console/api/system-manage-extend/integration/casdoor/test-reauth',
      body: { etag: 1, revision_id: revisionId },
    })
    navigation.mockRestore()
  })
  it('uses the saved draft identity and navigates to the trusted one-use handoff', async () => {
    const user = userEvent.setup()
    const navigation = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    const path = `/console/api/auth/casdoor/diagnostic/${'A'.repeat(43)}`
    writeReply = () => json({ status: 'started', reason: null, handoff: { handoff_path: path } })
    await ready()
    await user.click(screen.getByRole('button', { name: 'Test sign-in' }))
    await waitFor(() =>
      expect(navigation).toHaveBeenCalledWith(`https://console.example.test${path}`),
    )
    expect(writes[0]).toMatchObject({
      path: '/console/api/system-manage-extend/integration/casdoor/test-login',
      body: { etag: 1, revision_id: revisionId },
    })
    navigation.mockRestore()
  })
  it('rejects an external response without navigating or marking the diagnostic passed', async () => {
    const user = userEvent.setup()
    const navigation = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
    writeReply = () =>
      json({ status: 'started', handoff: { handoff_path: 'https://evil.example/steal' } })
    await ready()
    await user.click(screen.getByRole('button', { name: 'Test sign-in' }))
    await screen.findByRole('alert')
    expect(navigation).not.toHaveBeenCalled()
    expect(screen.queryByText('Test sign-in: Passed')).not.toBeInTheDocument()
    navigation.mockRestore()
  })
  it('refreshes same-ETag summaries while preserving dirty edits and blocking diagnostic start', async () => {
    const user = userEvent.setup()
    await ready()
    const organization = screen.getByLabelText('Casdoor organization')
    await user.clear(organization)
    await user.type(organization, 'unsaved-local-org')
    const current = configured()
    current.draft!.validation = [
      { kind: 'diagnostic', revision_id: revisionId, status: 'failed', code: 'identity_conflict' },
    ]
    server = current
    const pageReturn = new Event('pageshow')
    Object.defineProperty(pageReturn, 'persisted', { value: true })
    window.dispatchEvent(pageReturn)
    await screen.findByText('Test sign-in: Failed')
    expect(organization).toHaveValue('unsaved-local-org')
    expect(screen.getByRole('button', { name: 'Test sign-in' })).toBeDisabled()
    expect(writes).toHaveLength(0)
  })
})

describe('additive namespace reset integration', () => {
  it('refreshes the new disabled draft while retaining the mounted dirty fields and Secret', async () => {
    const { client } = await ready()
    seedAccountProfileQuery(client, { id: workspaceId })
    const user = userEvent.setup()
    await user.type(
      screen.getByLabelText('Replace Client Secret'),
      'synthetic-reset-preserved-secret',
    )
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'unsaved-organization')
    writeReply = (_body, path) => {
      if (path.endsWith('/reset-namespace/review'))
        return json({
          review_id: 'R'.repeat(43),
          namespace_id: namespaceId,
          etag: 1,
          expires_in: 60,
          credential_check: 'format_only',
        })
      server = configured({
        etag: 2,
        draft_revision_id: nextRevision,
        draft: { ...configured().draft!, namespace_id: workspaceId, revision_id: nextRevision },
      })
      return json(server)
    }
    await user.click(
      await screen.findByRole('checkbox', { name: 'I have reviewed the management transfer.' }),
    )
    await user.click(screen.getByRole('button', { name: 'Review namespace reset' }))
    const region = screen.getByRole('region', { name: 'Namespace reset' })
    await user.click(
      await within(region).findByRole('checkbox', { name: 'I confirm this reviewed change.' }),
    )
    await user.click(within(region).getByRole('button', { name: 'Confirm' }))
    await within(region).findByText(
      'Current configuration is a new disabled draft. Existing rows are preserved.',
    )
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue(
      'synthetic-reset-preserved-secret',
    )
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('unsaved-organization')
    expect(writes).toHaveLength(2)
    expect(JSON.stringify(writes)).not.toContain('synthetic-reset-preserved-secret')
    expect(JSON.stringify(dehydrate(client))).not.toContain('synthetic-reset-preserved-secret')
    client.clear()
  })
})
