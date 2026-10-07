import type { ReactNode } from 'react'
import { QueryClient } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NuqsTestingAdapter } from 'nuqs/adapters/testing'
import { beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import SystemManageLayout from '@/app/(commonLayout)/system-manage-extend/layout'
import SystemIntegrationPage from '@/app/(commonLayout)/system-manage-extend/system-integration/page'
import SystemManageNavExtend from '@/app/components/main-nav/components/system-manage-nav-extend'
import { consoleQuery } from '@/service/console'
import { useLogout } from '@/service/use-common'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { seedCurrentWorkspaceQuery } from '@/test/console/current-workspace'
import { QueryClientTestProvider } from '@/test/console/query-provider'

const { transport, route } = vi.hoisted(() => ({
  transport: vi.fn(),
  route: { pathname: '/system-manage-extend/system-integration' },
}))
vi.mock('@/service/base', () => ({ request: transport }))
vi.mock('@/utils/client', () => ({ isClient: true, isServer: false }))
vi.mock('@/config', () => ({ API_PREFIX: 'https://console.example.test/console/api' }))
vi.mock('@/env', () => ({
  env: { NEXT_PUBLIC_API_PREFIX: 'https://console.example.test/console/api' },
}))
vi.mock('@/next/navigation', async (original) => ({
  ...(await original<typeof import('@/next/navigation')>()),
  usePathname: () => route.pathname,
  useSelectedLayoutSegment: () => route.pathname.split('/')[2] ?? null,
}))
vi.mock('@/app/(commonLayout)/system-manage-extend/system-integration/dingtalk-config', () => ({
  default: () => <div>DingTalk boundary</div>,
}))
vi.mock('@/app/(commonLayout)/system-manage-extend/system-integration/oauth2-config', () => ({
  default: () => <div>OAuth2 boundary</div>,
}))
vi.mock('@/app/(commonLayout)/system-manage-extend/system-integration/email-api-config', () => ({
  default: () => <div>Email API boundary</div>,
}))
vi.mock('@/app/(commonLayout)/system-manage-extend/system-integration/forward-token-list', () => ({
  default: () => <div>Forward Token boundary</div>,
}))

const workspaceId = '11111111-1111-4111-8111-111111111111'
const accountId = '22222222-2222-4222-8222-222222222222'
const permissionKey = () => [
  ...consoleQuery.systemManageExtend.permissions.get.key(),
  { accountId, workspaceId },
]
const grant = () => ({ can_manage_system: true, workspace_id: workspaceId, account_id: accountId })
let permission: unknown
let casdoorPermission: unknown
let requests: Request[]
let responseStatus: number
let pending: boolean
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.spyOn(console, 'error').mockImplementation(() => {})
  route.pathname = '/system-manage-extend/system-integration'
  permission = grant()
  casdoorPermission = { can_manage_casdoor: true }
  responseStatus = 200
  pending = false
  requests = []
  transport.mockImplementation(
    async (_url: string, _init: RequestInit, { request }: { request: Request }) => {
      requests.push(request)
      const path = new URL(request.url).pathname
      if (path.endsWith('/logout')) return json({ result: 'success' })
      if (path.endsWith('/system-manage-extend/permissions')) {
        if (pending) return new Promise(() => {})
        return json(permission, responseStatus)
      }
      if (path.endsWith('/permissions')) return json(casdoorPermission)
      if (path.endsWith('/summary')) return new Promise(() => {})
      if (path.endsWith('/workspaces'))
        return json({
          page: 1,
          limit: 100,
          total: 0,
          has_more: false,
          earliest_created_workspace: null,
          earliest_created_ambiguous: false,
          workspaces: [],
        })
      if (path.endsWith('/casdoor'))
        return json({
          enabled: false,
          etag: 0,
          active: null,
          draft: null,
          active_revision_id: null,
          draft_revision_id: null,
        })
      throw new Error(`Unexpected offline boundary: ${path}`)
    },
  )
})
function LogoutButton() {
  const logout = useLogout()
  return (
    <button type="button" onClick={() => logout.mutate()}>
      Log out synthetic account
    </button>
  )
}
function mount({
  role = 'owner',
  search = '',
  child,
  logout = false,
  directPage = false,
  queryClient,
}: {
  role?: 'normal' | 'owner' | 'admin' | 'editor' | 'dataset_operator'
  search?: string
  child?: ReactNode
  logout?: boolean
  directPage?: boolean
  queryClient?: QueryClient
} = {}) {
  const client =
    queryClient ??
    new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  seedCurrentWorkspaceQuery(client, { role, id: workspaceId })
  seedAccountProfileQuery(client, { id: accountId })
  const onUrlUpdate = vi.fn()
  const tree = (searchParams: string) => (
    <QueryClientTestProvider queryClient={client}>
      <NuqsTestingAdapter searchParams={searchParams} hasMemory onUrlUpdate={onUrlUpdate}>
        <nav aria-label="Primary">
          <SystemManageNavExtend pathname={route.pathname} />
        </nav>
        <main>
          {directPage ? (
            <SystemIntegrationPage />
          ) : (
            <SystemManageLayout>{child ?? <SystemIntegrationPage />}</SystemManageLayout>
          )}
        </main>
        {logout && <LogoutButton />}
      </NuqsTestingAdapter>
    </QueryClientTestProvider>
  )
  const view = render(tree(search))
  return { client, onUrlUpdate, ...view, navigate: (next: string) => view.rerender(tree(next)) }
}
const primary = () => within(screen.getByRole('navigation', { name: 'Primary' }))
const noPanels = () => {
  for (const text of [
    'DingTalk boundary',
    'OAuth2 boundary',
    'Email API boundary',
    'Forward Token boundary',
  ])
    expect(screen.queryByText(text)).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()
}
async function ready() {
  await screen.findByRole('button', { name: /casdoor.save$/ })
}

describe('global system management through composed owners and generated transport', () => {
  it.each(['normal', 'editor', 'dataset_operator'] as const)(
    '%s cannot inherit a cached global grant',
    async (role) => {
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: Infinity } },
      })
      client.setQueryData(permissionKey(), grant())
      mount({ role, queryClient: client, search: '?tab=casdoor' })
      expect(primary().queryByRole('link')).not.toBeInTheDocument()
      noPanels()
      expect(requests).toHaveLength(0)
    },
  )
  it.each(['owner', 'admin'] as const)(
    'initial workspace %s sees all destinations and integrations',
    async (role) => {
      mount({ role, search: '?tab=casdoor' })
      await ready()
      expect(primary().getByRole('link')).toHaveAttribute(
        'href',
        '/system-manage-extend/system-integration',
      )
      expect(
        within(screen.getByRole('navigation', { name: 'extend.systemManage.title' })).getAllByRole(
          'link',
        ),
      ).toHaveLength(3)
      for (const label of ['dingtalk', 'oauth2', 'casdoor'])
        expect(
          screen.getByRole('button', { name: `extend.systemManage.${label}.title` }),
        ).toBeVisible()
    },
  )
  it.each(['owner', 'admin'] as const)(
    'other workspace %s has no global menu or direct route content',
    async (role) => {
      permission = { ...grant(), can_manage_system: false }
      const { client } = mount({ role, search: '?tab=casdoor' })
      await waitFor(() => expect(client.getQueryState(permissionKey())?.status).toBe('success'))
      expect(primary().queryByRole('link')).not.toBeInTheDocument()
      expect(
        screen.queryByRole('navigation', { name: 'extend.systemManage.title' }),
      ).not.toBeInTheDocument()
      noPanels()
      expect(requests).toHaveLength(1)
    },
  )
  it.each(['pending', 'error', 'malformed', 'wrong workspace', 'wrong account'] as const)(
    'unconfirmed %s cannot expose management',
    async (state) => {
      pending = state === 'pending'
      responseStatus = state === 'error' ? 503 : 200
      if (state === 'malformed') permission = { ...grant(), can_manage_system: 'true' }
      if (state === 'wrong workspace')
        permission = { ...grant(), workspace_id: '33333333-3333-4333-8333-333333333333' }
      if (state === 'wrong account')
        permission = { ...grant(), account_id: '33333333-3333-4333-8333-333333333333' }
      const { client } = mount({ search: '?tab=casdoor' })
      await waitFor(() => expect(requests).toHaveLength(1))
      if (!pending)
        await waitFor(() => expect(client.getQueryState(permissionKey())?.fetchStatus).toBe('idle'))
      expect(primary().queryByRole('link')).not.toBeInTheDocument()
      noPanels()
      expect(requests).toHaveLength(1)
    },
  )
  it.each([
    '/system-manage-extend',
    '/system-manage-extend/quota-management',
    '/system-manage-extend/code-execution-control',
  ])('denied direct %s never mounts children', async (path) => {
    route.pathname = path
    permission = { ...grant(), can_manage_system: false }
    const { client } = mount({ child: <p>Forbidden child</p> })
    await waitFor(() => expect(client.getQueryState(permissionKey())?.status).toBe('success'))
    expect(screen.queryByText('Forbidden child')).not.toBeInTheDocument()
  })
  it('integration independently gates direct URLs without its parent layout', async () => {
    permission = { ...grant(), can_manage_system: false }
    const { client } = mount({ directPage: true, search: '?tab=casdoor' })
    await waitFor(() => expect(client.getQueryState(permissionKey())?.status).toBe('success'))
    noPanels()
  })
  it('Casdoor denial keeps authorized legacy integrations available', async () => {
    casdoorPermission = { can_manage_casdoor: false }
    mount({ search: '?tab=casdoor' })
    await screen.findByRole('button', { name: /dingtalk.title$/ })
    await userEvent.setup().click(screen.getByRole('button', { name: /dingtalk.title$/ }))
    expect(screen.getByText('DingTalk boundary')).toBeVisible()
    expect(primary().getByRole('link')).toBeInTheDocument()
    expect(requests.some((r) => new URL(r.url).pathname.endsWith('/casdoor'))).toBe(false)
  })
  it('nuqs owns Casdoor push and history while preserving unrelated parameters', async () => {
    const view = mount({ search: '?filter=kept&tab=dingtalk' })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /casdoor.title$/ }))
    await ready()
    await waitFor(() => expect(view.onUrlUpdate).toHaveBeenCalled())
    expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.get('filter')).toBe('kept')
    expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.get('tab')).toBe('casdoor')
    expect(view.onUrlUpdate.mock.lastCall?.[0].options.history).toBe('push')
    await user.click(screen.getByRole('button', { name: /oauth2.title$/ }))
    expect(screen.getByText('OAuth2 boundary')).toBeVisible()
    await waitFor(() =>
      expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.get('tab')).toBe('oauth2'),
    )
    view.navigate('?filter=kept&tab=casdoor')
    await ready()
  })
  it.each(['denied', 'malformed', 'error'] as const)(
    'refetch %s revokes all management surfaces',
    async (state) => {
      const { client } = mount({ search: '?tab=casdoor' })
      await ready()
      permission =
        state === 'malformed'
          ? { ...grant(), can_manage_system: 'true' }
          : { ...grant(), can_manage_system: false }
      responseStatus = state === 'error' ? 503 : 200
      await act(async () => {
        await client.invalidateQueries({
          queryKey: consoleQuery.systemManageExtend.permissions.get.key(),
        })
      })
      await waitFor(() => expect(primary().queryByRole('link')).not.toBeInTheDocument())
      noPanels()
    },
  )
  it('logout clears grants so a subsequent account cannot inherit global management', async () => {
    const view = mount({ search: '?tab=casdoor', logout: true })
    await ready()
    expect(view.client.getQueryData(permissionKey())).toEqual(grant())
    permission = { ...grant(), can_manage_system: false }
    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out synthetic account' }))
    await waitFor(() => expect(view.client.getQueryData(permissionKey())).toBeUndefined())
    view.unmount()
    mount({ queryClient: view.client, role: 'normal' })
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    noPanels()
  })
})
