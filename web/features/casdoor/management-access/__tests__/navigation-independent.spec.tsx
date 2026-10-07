import { QueryClient } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import SystemManageLayout from '@/app/(commonLayout)/system-manage-extend/layout'
import SystemManageNavExtend from '@/app/components/main-nav/components/system-manage-nav-extend'
import { consoleQuery } from '@/service/console'
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
vi.mock('@/next/navigation', async (original) => ({
  ...(await original<typeof import('@/next/navigation')>()),
  useSelectedLayoutSegment: () => 'system-integration',
}))
const initialWorkspace = '11111111-1111-4111-8111-111111111111'
const otherWorkspace = '33333333-3333-4333-8333-333333333333'
const accountId = '22222222-2222-4222-8222-222222222222'
const nextAccount = '44444444-4444-4444-8444-444444444444'
const grant = (workspace = initialWorkspace, account = accountId) => ({
  can_manage_system: true,
  workspace_id: workspace,
  account_id: account,
})
const scopedKey = (workspaceId = initialWorkspace, account = accountId) => [
  ...consoleQuery.systemManageExtend.permissions.get.key(),
  { accountId: account, workspaceId },
]
let replies: ((response: Response) => void)[]
const reply = (index: number, data: unknown) =>
  replies[index]!(
    new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } }),
  )
beforeEach(() => {
  vi.clearAllMocks()
  replies = []
  transport.mockImplementation(
    (_url: string, _init: RequestInit, { request }: { request: Request }) => {
      if (new URL(request.url).pathname.endsWith('/system-manage-extend/permissions'))
        return new Promise<Response>((resolve) => replies.push(resolve))
      throw new Error('Unexpected network request')
    },
  )
})
function mount(
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } }),
) {
  seedCurrentWorkspaceQuery(client, { id: initialWorkspace, role: 'owner' })
  seedAccountProfileQuery(client, { id: accountId })
  const view = render(
    <QueryClientTestProvider queryClient={client}>
      <nav aria-label="Primary">
        <SystemManageNavExtend pathname="/system-manage-extend/system-integration" />
      </nav>
      <SystemManageLayout>
        <p>Global configuration content</p>
        <input aria-label="Unsaved global draft" defaultValue="" />
      </SystemManageLayout>
    </QueryClientTestProvider>,
  )
  return { client, ...view }
}
const nav = () => within(screen.getByRole('navigation', { name: 'Primary' }))
const denied = () => {
  expect(nav().queryByRole('link')).not.toBeInTheDocument()
  expect(screen.queryByText('Global configuration content')).not.toBeInTheDocument()
}
async function allowFirst() {
  await waitFor(() => expect(replies).toHaveLength(1))
  await act(async () => {
    reply(0, grant())
  })
  await screen.findByText('Global configuration content')
}
describe('global management identity and workspace query boundaries', () => {
  it('a stale cached grant is hidden until its authoritative request completes', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
    client.setQueryData(scopedKey(), grant())
    mount(client)
    await waitFor(() => expect(replies).toHaveLength(1))
    denied()
    await act(async () => {
      reply(0, { ...grant(), can_manage_system: false })
    })
    await waitFor(() => expect(client.getQueryState(scopedKey())?.fetchStatus).toBe('idle'))
    denied()
  })
  it('switching to another owner workspace immediately drops the initialization workspace grant', async () => {
    const { client } = mount()
    await allowFirst()
    await act(async () => {
      seedCurrentWorkspaceQuery(client, { id: otherWorkspace, role: 'owner' })
    })
    await waitFor(() => expect(replies.length).toBeGreaterThanOrEqual(2))
    denied()
    await act(async () => {
      for (let index = 1; index < replies.length; index++)
        reply(index, { ...grant(otherWorkspace), can_manage_system: false })
    })
    await waitFor(() =>
      expect(client.getQueryState(scopedKey(otherWorkspace))?.fetchStatus).toBe('idle'),
    )
    denied()
  })
  it('a late initialization-workspace response cannot authorize the newly selected workspace', async () => {
    const { client } = mount()
    await waitFor(() => expect(replies).toHaveLength(1))
    await act(async () => {
      seedCurrentWorkspaceQuery(client, { id: otherWorkspace, role: 'admin' })
    })
    await waitFor(() => expect(replies.length).toBeGreaterThanOrEqual(2))
    await act(async () => {
      reply(0, grant())
    })
    denied()
    await act(async () => {
      for (let index = 1; index < replies.length; index++)
        reply(index, { ...grant(otherWorkspace), can_manage_system: false })
    })
    await waitFor(() =>
      expect(client.getQueryState(scopedKey(otherWorkspace))?.fetchStatus).toBe('idle'),
    )
    denied()
  })
  it('a different account in the same workspace cannot inherit the previous account grant', async () => {
    const { client } = mount()
    await allowFirst()
    await act(async () => {
      seedAccountProfileQuery(client, { id: nextAccount })
    })
    await waitFor(() => expect(replies.length).toBeGreaterThanOrEqual(2))
    denied()
    // Even a wrongly echoed old-account grant remains unusable.
    await act(async () => {
      for (let index = 1; index < replies.length; index++) reply(index, grant())
    })
    await waitFor(() =>
      expect(client.getQueryState(scopedKey(initialWorkspace, nextAccount))?.fetchStatus).toBe(
        'idle',
      ),
    )
    denied()
  })
  it('background revalidation retains an unsaved draft for the same account and workspace', async () => {
    const { client } = mount()
    await allowFirst()
    await userEvent
      .setup()
      .type(screen.getByRole('textbox', { name: 'Unsaved global draft' }), 'unsaved value')
    await act(async () => {
      client
        .invalidateQueries({ queryKey: consoleQuery.systemManageExtend.permissions.get.key() })
        .catch(() => {})
    })
    await waitFor(() => expect(replies.length).toBeGreaterThanOrEqual(2))
    expect(screen.getByRole('textbox', { name: 'Unsaved global draft' })).toHaveValue(
      'unsaved value',
    )
    await act(async () => {
      for (let index = 1; index < replies.length; index++) reply(index, grant())
    })
    await waitFor(() => expect(client.getQueryState(scopedKey())?.fetchStatus).toBe('idle'))
    expect(screen.getByRole('textbox', { name: 'Unsaved global draft' })).toHaveValue(
      'unsaved value',
    )
  })
  it('a role revocation in the same initialization workspace removes visible management', async () => {
    const { client } = mount()
    await allowFirst()
    await act(async () => {
      seedCurrentWorkspaceQuery(client, { id: initialWorkspace, role: 'normal' })
    })
    await waitFor(() =>
      expect(screen.queryByText('Global configuration content')).not.toBeInTheDocument(),
    )
    denied()
  })
})
