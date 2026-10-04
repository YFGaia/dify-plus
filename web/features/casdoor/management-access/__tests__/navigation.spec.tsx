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
import { seedCurrentWorkspaceQuery } from '@/test/console/current-workspace'
import { QueryClientTestProvider } from '@/test/console/query-provider'

const { transport, logoutPost, route } = vi.hoisted(() => ({
  transport: vi.fn(),
  logoutPost: vi.fn(),
  route: { pathname: '/system-manage-extend/system-integration' },
}))
vi.mock('@/service/base', () => ({ request: transport, post: logoutPost }))
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

const permissionKey = () =>
  consoleQuery.systemManageExtend.integration.casdoor.permissions.get.queryKey()
const emptyConfiguration = {
  enabled: false,
  etag: 0,
  active: null,
  draft: null,
  active_revision_id: null,
  draft_revision_id: null,
}
const workspacePage = {
  page: 1,
  limit: 100,
  total: 0,
  has_more: false,
  earliest_created_workspace: null,
  earliest_created_ambiguous: false,
  workspaces: [],
}
let permission: unknown
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
  permission = { can_manage_casdoor: true }
  responseStatus = 200
  pending = false
  requests = []
  logoutPost.mockResolvedValue({ result: 'success' })
  transport.mockImplementation(
    async (_url: string, _init: RequestInit, options: { request: Request }) => {
      const request = options.request
      requests.push(request)
      const path = new URL(request.url).pathname
      if (path.endsWith('/permissions')) {
        if (pending) return new Promise(() => {})
        return json(permission, responseStatus)
      }
      if (path.endsWith('/summary')) return new Promise(() => {})
      if (path.endsWith('/workspaces')) return json(workspacePage)
      if (path.endsWith('/casdoor')) return json(emptyConfiguration)
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
  owner = false,
  search = '',
  child,
  logout = false,
  directPage = false,
  queryClient,
}: {
  owner?: boolean
  search?: string
  child?: ReactNode
  logout?: boolean
  directPage?: boolean
  queryClient?: QueryClient
} = {}) {
  const client =
    queryClient ??
    new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
  seedCurrentWorkspaceQuery(client, { role: owner ? 'owner' : 'normal' })
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
const noOldPanels = () => {
  for (const text of [
    'DingTalk boundary',
    'OAuth2 boundary',
    'Email API boundary',
    'Forward Token boundary',
  ])
    expect(screen.queryByText(text)).not.toBeInTheDocument()
}
async function ready() {
  await screen.findByRole('button', { name: /casdoor.save$/ })
}

describe('Casdoor management admission through actual composed owners and generated transport', () => {
  it.each(['', '?tab=oauth2', '?tab=casdoor', '?tab=unrecognized'])(
    'granted normal user sees only Casdoor for %s',
    async (search) => {
      const { client } = mount({ search })
      await ready()
      noOldPanels()
      const link = primary().getByRole('link', { name: 'extend.systemManage.title' })
      expect(link).toHaveAttribute('href', '/system-manage-extend/system-integration?tab=casdoor')
      const menu = screen.getByRole('navigation', { name: 'extend.systemManage.title' })
      expect(within(menu).getAllByRole('link')).toHaveLength(1)
      expect(within(menu).getByRole('link')).toHaveAttribute(
        'href',
        '/system-manage-extend/system-integration?tab=casdoor',
      )
      expect(screen.queryByRole('button', { name: /dingtalk.title$/ })).not.toBeInTheDocument()
      expect(screen.getAllByRole('main')).toHaveLength(1)
      expect(
        client.getQueryCache().findAll({ queryKey: permissionKey(), exact: true }),
      ).toHaveLength(1)
      expect(requests.every((r) => r.method === 'GET')).toBe(true)
    },
  )

  it.each([
    '/system-manage-extend',
    '/system-manage-extend/quota-management',
    '/system-manage-extend/code-execution-control',
    '/system-manage-extend/unknown',
    '/system-manage-extend/system-integration/nested',
    '/system-manage-extend/system-integration-like',
  ])('grant cannot mount management children at %s', async (path) => {
    route.pathname = path
    mount({ search: '?tab=casdoor', child: <p>Forbidden child boundary</p> })
    await screen.findByText('extend.systemManage.common.noPermission')
    await waitFor(() => expect(primary().getByRole('link')).toBeInTheDocument())
    expect(screen.queryByText('Forbidden child boundary')).not.toBeInTheDocument()
    expect(requests.every((r) => new URL(r.url).pathname.endsWith('/permissions'))).toBe(true)
  })

  it.each([
    {},
    { can_manage_casdoor: false },
    { can_manage_casdoor: 'true' },
    { can_manage_casdoor: 1 },
    null,
  ])('raw permission %j fails closed', async (value) => {
    permission = value
    mount({ search: '?tab=casdoor' })
    await screen.findByRole('alert')
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    noOldPanels()
    expect(requests).toHaveLength(1)
  })

  it.each(['pending', 'error'])('normal user is denied during %s', async (state) => {
    pending = state === 'pending'
    responseStatus = state === 'error' ? 503 : 200
    permission = { code: 'provider_unavailable' }
    mount({ search: '?tab=casdoor' })
    await screen.findByRole(state === 'pending' ? 'status' : 'alert')
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    noOldPanels()
  })

  it.each(['pending', 'error', 'denied'])(
    'owner keeps all old panels during Casdoor %s',
    async (state) => {
      pending = state === 'pending'
      responseStatus = state === 'error' ? 503 : 200
      permission = { can_manage_casdoor: false }
      mount({ owner: true })
      expect(screen.getByText('DingTalk boundary')).toBeVisible()
      expect(primary().getByRole('link')).toHaveAttribute(
        'href',
        '/system-manage-extend/system-integration',
      )
      expect(
        within(screen.getByRole('navigation', { name: 'extend.systemManage.title' })).getAllByRole(
          'link',
        ),
      ).toHaveLength(3)
      const user = userEvent.setup()
      for (const [label, content] of [
        ['oauth2', 'OAuth2 boundary'],
        ['emailApi', 'Email API boundary'],
        ['forwardToken', 'Forward Token boundary'],
        ['dingtalk', 'DingTalk boundary'],
      ] as const) {
        await user.click(screen.getByRole('button', { name: `extend.systemManage.${label}.title` }))
        expect(screen.getByText(content)).toBeVisible()
      }
    },
  )

  it('owner forged Casdoor URL never mounts denied form and can return to DingTalk', async () => {
    permission = { can_manage_casdoor: false }
    mount({ owner: true, search: '?tab=casdoor' })
    await screen.findByRole('alert')
    noOldPanels()
    expect(requests).toHaveLength(1)
    await userEvent.setup().click(screen.getByRole('button', { name: /dingtalk.title$/ }))
    expect(screen.getByText('DingTalk boundary')).toBeVisible()
  })

  it.each(['oauth2', 'unrecognized'])(
    'legacy button leaves ignored tab=%s URL untouched',
    async (tab) => {
      const view = mount({ owner: true, search: `?tab=${tab}&filter=kept` })
      expect(screen.getByText('DingTalk boundary')).toBeVisible()
      await userEvent.setup().click(screen.getByRole('button', { name: /emailApi.title$/ }))
      expect(screen.getByText('Email API boundary')).toBeVisible()
      expect(view.onUrlUpdate).not.toHaveBeenCalled()
    },
  )

  it('nuqs owns Casdoor push and subsequent history input while preserving unrelated parameters', async () => {
    const view = mount({ owner: true, search: '?filter=kept' })
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: /casdoor.title$/ }))
    await ready()
    await waitFor(() => expect(view.onUrlUpdate).toHaveBeenCalled())
    expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.get('filter')).toBe('kept')
    expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.get('tab')).toBe('casdoor')
    expect(view.onUrlUpdate.mock.lastCall?.[0].options.history).toBe('push')
    await user.click(screen.getByRole('button', { name: /oauth2.title$/ }))
    expect(screen.getByText('OAuth2 boundary')).toBeVisible()
    await waitFor(() =>
      expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.has('tab')).toBe(false),
    )
    expect(view.onUrlUpdate.mock.lastCall?.[0].searchParams.get('filter')).toBe('kept')
    view.navigate('?filter=kept&tab=casdoor')
    await ready()
    noOldPanels()
    view.navigate('?filter=kept')
    expect(screen.getByText('OAuth2 boundary')).toBeVisible()
  })

  it.each(['denied', 'malformed', 'error'])(
    'refetch %s revokes previous true across page layout and nav',
    async (state) => {
      const { client } = mount()
      await ready()
      permission =
        state === 'malformed' ? { can_manage_casdoor: 'true' } : { can_manage_casdoor: false }
      responseStatus = state === 'error' ? 503 : 200
      await act(async () => {
        await client.invalidateQueries({ queryKey: permissionKey() })
      })
      await screen.findByRole('alert')
      expect(primary().queryByRole('link')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()
      noOldPanels()
    },
  )

  it.each(['denied', 'malformed', 'error'])(
    'direct page gate rejects forged old tab during %s',
    async (state) => {
      permission =
        state === 'malformed' ? { can_manage_casdoor: 'true' } : { can_manage_casdoor: false }
      responseStatus = state === 'error' ? 503 : 200
      mount({ directPage: true, search: '?tab=oauth2' })
      await screen.findByRole('alert')
      noOldPanels()
      expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()
      expect(requests).toHaveLength(1)
    },
  )

  it('owner new panel unmounts on permission refetch error while legacy buttons remain usable', async () => {
    const { client } = mount({ owner: true, search: '?tab=casdoor' })
    await ready()
    responseStatus = 503
    await act(async () => {
      await client.invalidateQueries({ queryKey: permissionKey() })
    })
    await screen.findByRole('alert')
    expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()
    expect(primary().getByRole('link')).toHaveAttribute(
      'href',
      '/system-manage-extend/system-integration',
    )
    await userEvent.setup().click(screen.getByRole('button', { name: /dingtalk.title$/ }))
    expect(screen.getByText('DingTalk boundary')).toBeVisible()
  })

  it('actual logout owner clears permission cache and the next account cannot inherit the grant', async () => {
    const view = mount({ logout: true })
    await ready()
    expect(view.client.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: true })
    permission = { can_manage_casdoor: false }
    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out synthetic account' }))
    await waitFor(() =>
      expect(
        view.client.getQueryCache().find({ queryKey: permissionKey(), exact: true }),
      ).toBeUndefined(),
    )
    expect(logoutPost).toHaveBeenCalledWith('/logout')
    view.unmount()
    const next = mount({ queryClient: view.client })
    await screen.findByRole('alert')
    expect(next.client.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: false })
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    noOldPanels()
  })
})
