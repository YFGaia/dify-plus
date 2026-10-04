import type {
  CasdoorConfiguration,
  CasdoorConfigurationResponse,
  CasdoorWorkspacesResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { dehydrate, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { consoleQuery } from '@/service/console'

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
const baseConfiguration: CasdoorConfiguration = {
  application: 'offline-app',
  organization: 'offline-org',
  client_id: 'offline-client',
  backend_api_url: 'https://idp.example.test',
  browser_frontend_url: 'https://idp.example.test',
  expected_issuer: 'https://idp.example.test',
  default_workspace_id: workspaceId,
  button_text: 'Casdoor',
}

function configuration(etag = 1, overrides: Partial<CasdoorConfiguration> = {}) {
  const id = etag === 1 ? revisionId : nextRevisionId
  return {
    enabled: false,
    etag,
    active: null,
    active_revision_id: null,
    draft_revision_id: id,
    draft: {
      configuration: { ...baseConfiguration, ...overrides },
      namespace_id: namespaceId,
      revision_id: id,
      secret_configured: true,
    },
  } satisfies CasdoorConfigurationResponse
}

const workspaces: CasdoorWorkspacesResponse = {
  page: 1,
  limit: 100,
  total: 1,
  has_more: false,
  earliest_created_ambiguous: false,
  earliest_created_workspace: {
    workspace_id: workspaceId,
    name: 'Offline workspace',
    created_at: '2020-01-01T00:00:00Z',
    available: true,
  },
  workspaces: [
    {
      workspace_id: workspaceId,
      name: 'Offline workspace',
      created_at: '2020-01-01T00:00:00Z',
      available: true,
    },
  ],
}

function staticPassed(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    certificate_summaries: [
      {
        accept_until: '2030-01-01T00:00:00Z',
        fingerprint: 'a'.repeat(64),
        kid: 'offline-kid',
        not_before: '2020-01-01T00:00:00Z',
      },
    ],
    kind: 'static',
    status: 'passed',
    static_only: true,
    revision_id: revisionId,
    etag: 1,
    checked_at: '2026-10-01T00:00:00Z',
    certificates: [],
    ...overrides,
  }
}

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

let serverConfiguration: unknown
let permissionResponse: unknown
let malformedOnConfigurationRead = false
let writes: Array<{ method: string; path: string; body: Record<string, unknown> }> = []
let holdWrite = false
let releaseWrite: (() => void) | null = null
let writeReply: (body: Record<string, unknown>, path: string) => Response

beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'error').mockImplementation(() => {})
  serverConfiguration = configuration()
  permissionResponse = { can_manage_casdoor: true }
  malformedOnConfigurationRead = false
  writes = []
  holdWrite = false
  releaseWrite = null
  writeReply = (body) =>
    json(configuration(2, body.configuration as Partial<CasdoorConfiguration> | undefined))
  transport.mockImplementation(
    async (_url: string, _init: RequestInit, options: { request: Request }) => {
      const request = options.request
      const url = new URL(request.url)
      if (request.method === 'GET') {
        if (url.pathname.endsWith('/permissions')) return json(permissionResponse)
        if (url.pathname.endsWith('/workspaces')) return json(workspaces)
        return malformedOnConfigurationRead
          ? json({ enabled: false, etag: 'untrusted-etag' })
          : json(serverConfiguration)
      }
      const body = (await request.json()) as Record<string, unknown>
      writes.push({ method: request.method, path: url.pathname, body })
      if (holdWrite) {
        await new Promise<void>((resolve) => {
          releaseWrite = resolve
        })
        holdWrite = false
        releaseWrite = null
      }
      const response = writeReply(body, url.pathname)
      if (response.status === 200 && url.pathname.endsWith('/integration/casdoor'))
        serverConfiguration = await response.clone().json()
      return response
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
  await screen.findByText('Page 1 · 1 workspaces')
  return result
}

describe('Casdoor form independent query and mutation boundaries', () => {
  it('stops on malformed permission data without fetching configuration or remaining in loading', async () => {
    permissionResponse = { can_manage_casdoor: 'true' }
    await mount()
    expect(await screen.findAllByRole('alert')).not.toHaveLength(0)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Casdoor organization')).not.toBeInTheDocument()
    const requests = transport.mock.calls.map((call) => (call[2] as { request: Request }).request)
    expect(requests).toHaveLength(1)
    expect(new URL(requests[0]!.url).pathname).toMatch(/\/permissions$/)
  })

  it('retains dirty fields and Secret through a newer refetch and malformed refetch error until confirmed re-edit', async () => {
    await ready()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'locally-edited-org')
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-private-secret')

    serverConfiguration = configuration(2, { organization: 'newer-server-org' })
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await screen.findByText('Server ETag: 2')
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('locally-edited-org')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('synthetic-private-secret')

    malformedOnConfigurationRead = true
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('locally-edited-org')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('synthetic-private-secret')
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Re-edit from latest server draft' })).toBeDisabled()
    expect(screen.queryByText(/untrusted-etag|synthetic-private-secret/)).not.toBeInTheDocument()

    malformedOnConfigurationRead = false
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Re-edit from latest server draft' }),
      ).not.toBeDisabled(),
    )
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Confirm' }))
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('newer-server-org')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
  })

  it('retains only active mutation input in runtime memory, excludes it from bare dehydration, then resets after completion', async () => {
    holdWrite = true
    const { client } = await ready()
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Sign-in button text'), ' updated')
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-active-secret')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(writes).toHaveLength(1))

    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('synthetic-active-secret')
    expect(writes[0]?.body.secret).toBe('synthetic-active-secret')
    expect(JSON.stringify(dehydrate(client))).not.toContain('synthetic-active-secret')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('synthetic-active-secret')

    releaseWrite?.()
    await screen.findByText('Draft saved. SSO activation has not changed.')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0))
  })

  it('clears a failed transient Secret and blocks another write until explicit re-edit', async () => {
    const { client } = await ready()
    transport.mockImplementation(
      async (_url: string, _init: RequestInit, options: { request: Request }) => {
        const request = options.request
        const url = new URL(request.url)
        if (request.method === 'GET') {
          if (url.pathname.endsWith('/permissions')) return json(permissionResponse)
          if (url.pathname.endsWith('/workspaces')) return json(workspaces)
          return json(serverConfiguration)
        }
        const body = (await request.json()) as Record<string, unknown>
        writes.push({ method: request.method, path: url.pathname, body })
        return json(
          { code: 'config_conflict', correlation_id: namespaceId, message: 'raw-provider-secret' },
          409,
        )
      },
    )
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-failed-secret')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
    expect(
      screen.queryByText(/raw-provider-secret|synthetic-failed-secret/),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(writes).toHaveLength(1)
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0))

    serverConfiguration = configuration()
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    expect(screen.getByRole('button', { name: 'Save draft' })).not.toBeDisabled()
    expect(writes).toHaveLength(1)
  })

  it('keeps a configured Secret when saving a blank replacement and retains shared invalidation', async () => {
    const { client } = await ready()
    const staticKey = consoleQuery.systemManageExtend.integration.casdoor.validate.post.key()
    client.setQueryData(staticKey, staticPassed())
    expect(client.getQueryState(staticKey)?.isInvalidated).toBe(false)
    await userEvent.setup().type(screen.getByLabelText('Sign-in button text'), ' updated')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved. SSO activation has not changed.')
    expect(writes[0]?.body).not.toHaveProperty('secret')
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
    expect(screen.getByText('A Secret is configured for the saved draft.')).toBeInTheDocument()
    expect(client.getQueryState(staticKey)?.isInvalidated).toBe(true)
    await waitFor(() => expect(client.getMutationCache().getAll()).toHaveLength(0))
    const configReads = transport.mock.calls.filter((call) => {
      const request = (call[2] as { request: Request }).request
      return (
        request.method === 'GET' && new URL(request.url).pathname.endsWith('/integration/casdoor')
      )
    })
    expect(configReads.length).toBeGreaterThanOrEqual(2)
  })

  it('blocks further writes after a malformed successful save until a valid refresh and confirmed re-edit', async () => {
    await ready()
    writeReply = () => json({ enabled: false, etag: 2, draft: 'malformed-success' })
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Sign-in button text'), ' changed')
    await user.type(screen.getByLabelText('Replace Client Secret'), 'synthetic-save-secret')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    expect(await screen.findAllByRole('alert')).not.toHaveLength(0)
    expect(screen.getByLabelText('Replace Client Secret')).toHaveValue('')
    expect(screen.queryByText(/malformed-success|synthetic-save-secret/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(writes).toHaveLength(1)

    serverConfiguration = configuration(2, { organization: 'verified-after-refresh' })
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Re-edit from latest server draft' }),
      ).not.toBeDisabled(),
    )
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    expect(screen.getByLabelText('Casdoor organization')).toHaveValue('verified-after-refresh')
    expect(screen.getByRole('button', { name: 'Save draft' })).not.toBeDisabled()
    expect(writes).toHaveLength(1)
  })

  it('gates exact-revision Secret clearing on a clean draft and preserves active state after malformed success', async () => {
    const active = configuration().draft
    serverConfiguration = {
      ...configuration(),
      enabled: true,
      active,
      active_revision_id: revisionId,
    }
    const user = userEvent.setup()
    await ready()
    await user.type(screen.getByLabelText('Sign-in button text'), ' dirty')
    expect(screen.getByRole('button', { name: 'Clear saved draft Secret' })).toBeDisabled()

    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    writeReply = () => json({ enabled: true, etag: 2, draft: 'malformed-clear' })
    await user.click(screen.getByRole('button', { name: 'Clear saved draft Secret' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Confirm' }),
    )
    await screen.findByRole('alert')
    expect(writes[0]).toMatchObject({ method: 'POST', body: { etag: 1, revision_id: revisionId } })
    expect(screen.getByText('SSO enabled')).toBeInTheDocument()
    expect(screen.getByText(`Active revision: ${revisionId}`)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save draft' })).toBeDisabled()
    expect(screen.queryByText(/malformed-clear/)).not.toBeInTheDocument()

    const refreshed = configuration(2)
    serverConfiguration = {
      ...refreshed,
      enabled: true,
      active,
      active_revision_id: revisionId,
      draft: { ...refreshed.draft!, secret_configured: false },
    }
    await user.click(screen.getByRole('button', { name: 'Refresh server state' }))
    await screen.findByText('Server ETag: 2')
    await user.click(screen.getByRole('button', { name: 'Re-edit from latest server draft' }))
    expect(screen.getByRole('button', { name: 'Save draft' })).not.toBeDisabled()
    expect(writes).toHaveLength(1)
  })

  it('accepts static success only with raw explicit kind, status, static flag and exact revision metadata', async () => {
    await ready()
    writeReply = (_body, path) =>
      path.endsWith('/validate') ? json(staticPassed()) : json(configuration(2))
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Check saved draft (static only)' }))
    await screen.findByText(/Static checks passed for revision/)
    expect(screen.getByText('SSO disabled')).toBeInTheDocument()
    expect(writes[0]).toMatchObject({ method: 'POST', body: { etag: 1, revision_id: revisionId } })

    let completedWrites = 1
    for (const field of ['kind', 'status', 'static_only', 'revision_id', 'etag'] as const) {
      const malformed = staticPassed()
      if (field === 'revision_id') malformed[field] = nextRevisionId
      else if (field === 'etag') malformed[field] = 2
      else delete malformed[field]
      writeReply = () => json(malformed)
      await user.click(screen.getByRole('button', { name: 'Check saved draft (static only)' }))
      completedWrites += 1
      await waitFor(() => expect(writes).toHaveLength(completedWrites))
      expect(screen.queryByText(/Static checks passed for revision/)).not.toBeInTheDocument()
    }
  })
})
