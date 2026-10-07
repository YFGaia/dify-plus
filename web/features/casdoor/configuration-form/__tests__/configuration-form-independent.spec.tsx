import type {
  CasdoorConfiguration,
  CasdoorConfigurationResponse,
  CasdoorWorkspacesResponse,
} from '@dify/contracts/api/console/system-manage-extend/types.gen'
import { dehydrate, QueryClient } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
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
const baseConfiguration: CasdoorConfiguration = {
  schema_version: 2,
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

let serverConfiguration: unknown
let permissionResponse: unknown
let writes: Array<{ method: string; path: string; body: Record<string, unknown> }>
let holdWrite: boolean
let releaseWrite: (() => void) | null
let writeReply: (body: Record<string, unknown>, path: string) => Response | Promise<Response>

beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'error').mockImplementation(() => {})
  serverConfiguration = configuration()
  permissionResponse = { can_manage_casdoor: true }
  writes = []
  holdWrite = false
  releaseWrite = null
  writeReply = (body, path) => {
    if (path.endsWith('/validate'))
      return json(staticPassed(Number(body.etag), String(body.revision_id)))
    const saved = configuration(2, body.configuration as Partial<CasdoorConfiguration> | undefined)
    serverConfiguration = saved
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
        if (url.pathname.endsWith('/permissions')) return json(permissionResponse)
        if (url.pathname.endsWith('/workspaces')) return json(workspaces)
        return json(serverConfiguration)
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

describe('Casdoor form privacy and API boundaries', () => {
  it('blocks a normal member before permission or configuration requests', async () => {
    await mount('normal')
    await screen.findByRole('alert')
    expect(screen.queryByLabelText('Casdoor organization')).not.toBeInTheDocument()
    expect(transport).not.toHaveBeenCalled()
  })

  it('stops when permission data is malformed before fetching configuration', async () => {
    permissionResponse = { can_manage_casdoor: 'true' }
    await mount()
    expect(await screen.findAllByRole('alert')).not.toHaveLength(0)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Casdoor organization')).not.toBeInTheDocument()
    const requests = transport.mock.calls.map((call) => (call[2] as { request: Request }).request)
    expect(requests).toHaveLength(2)
    expect(new URL(requests[0]!.url).pathname).toMatch(/\/permissions$/)
  })

  it('keeps a replacement Secret out of dehydrated query state while saving', async () => {
    holdWrite = true
    const { client } = await ready()
    const user = userEvent.setup()
    await user.clear(screen.getByLabelText('Casdoor organization'))
    await user.type(screen.getByLabelText('Casdoor organization'), 'changed-org')
    await user.type(screen.getByLabelText('Client Secret'), 'synthetic-active-secret')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await waitFor(() => expect(writes).toHaveLength(1))

    expect(writes[0]?.body.secret).toBe('synthetic-active-secret')
    expect(screen.getByLabelText('Client Secret')).toHaveValue('')
    expect(JSON.stringify(dehydrate(client))).not.toContain('synthetic-active-secret')

    releaseWrite?.()
    await screen.findByText('Configuration saved and checked.')
    await waitFor(() => expect(writes).toHaveLength(2))
    expect(writes[1]).toMatchObject({
      method: 'POST',
      path: '/console/api/system-manage-extend/integration/casdoor/validate',
      body: { etag: 2, revision_id: nextRevisionId },
    })
  })

  it('omits a blank replacement Secret and validates the updated saved revision', async () => {
    await ready()
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Casdoor organization'), ' updated')
    await user.click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByText('Configuration saved and checked.')
    expect(writes[0]?.method).toBe('PUT')
    expect(writes[0]?.body).not.toHaveProperty('secret')
    expect(writes[1]?.body).toEqual({ etag: 2, revision_id: nextRevisionId })
    expect(screen.getByLabelText('Client Secret')).toHaveValue('')
  })

  it('does not announce a successful save when automatic validation cannot be confirmed', async () => {
    await ready()
    writeReply = () =>
      json({
        certificate_summaries: [],
        kind: 'static',
        status: 'passed',
        static_only: true,
        revision_id: nextRevisionId,
        etag: 1,
      })
    await userEvent.setup().click(screen.getByRole('button', { name: 'Save configuration' }))
    await screen.findByRole('alert')
    expect(screen.queryByText('Configuration saved and checked.')).not.toBeInTheDocument()
    expect(writes).toHaveLength(1)
    expect(writes[0]?.path).toMatch(/\/validate$/)
  })

  it('runs login testing against the saved revision and surfaces backend gate failures', async () => {
    await ready()
    writeReply = (_body, path) => {
      expect(path).toMatch(/\/test-login$/)
      return json({ status: 'blocked', reason: 'deployment_proof_missing' })
    }
    await userEvent.setup().click(screen.getByRole('button', { name: 'Test login' }))
    await screen.findByRole('alert')
    expect(writes).toMatchObject([
      {
        method: 'POST',
        path: '/console/api/system-manage-extend/integration/casdoor/test-login',
        body: { etag: 1, revision_id: revisionId },
      },
    ])
    expect(screen.queryByText('synthetic')).not.toBeInTheDocument()
  })
})
