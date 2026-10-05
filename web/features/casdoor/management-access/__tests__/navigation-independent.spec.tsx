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

let permissionRequest: (request: Request, call: number) => Promise<Response>
let permissionCall: number
let requests: Request[]
let logoutRequests: Request[]

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
  permissionCall = 0
  requests = []
  logoutRequests = []
  permissionRequest = async () => json({ can_manage_casdoor: true })
  logoutPost.mockResolvedValue({ result: 'success' })
  transport.mockImplementation(
    async (_url: string, _init: RequestInit, { request }: { request: Request }) => {
      const path = new URL(request.url).pathname
      if (path.endsWith('/logout') && request.method === 'POST') {
        logoutRequests.push(request)
        return json({ result: 'success' })
      }
      if (path.endsWith('/permissions')) {
        requests.push(request)
        return permissionRequest(request, ++permissionCall)
      }
      if (path.endsWith('/casdoor')) return json(emptyConfiguration)
      throw new Error(`Unexpected offline boundary: ${new URL(request.url).pathname}`)
    },
  )
})

function LogoutButton() {
  const logout = useLogout()
  return (
    <button type="button" onClick={() => logout.mutate()}>
      Log out old account
    </button>
  )
}

function mountComposed(queryClient: QueryClient, role: 'normal' | 'owner', includeLogout = false) {
  seedCurrentWorkspaceQuery(queryClient, { role })
  const onUrlUpdate = vi.fn()
  const view = render(
    <QueryClientTestProvider queryClient={queryClient}>
      <NuqsTestingAdapter searchParams="" hasMemory onUrlUpdate={onUrlUpdate}>
        <nav aria-label="Primary">
          <SystemManageNavExtend pathname={route.pathname} />
        </nav>
        <main>
          <SystemManageLayout>
            <SystemIntegrationPage />
          </SystemManageLayout>
        </main>
        {includeLogout && <LogoutButton />}
      </NuqsTestingAdapter>
    </QueryClientTestProvider>,
  )
  return { onUrlUpdate, ...view }
}

const primary = () => within(screen.getByRole('navigation', { name: 'Primary' }))

describe('legacy integration URL compatibility', () => {
  it('leaves ignored tab and unrelated query parameters untouched when an old button is used', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
    seedCurrentWorkspaceQuery(queryClient, { role: 'owner' })
    const onUrlUpdate = vi.fn()
    render(
      <QueryClientTestProvider queryClient={queryClient}>
        <NuqsTestingAdapter
          searchParams="tab=oauth2&filter=kept"
          hasMemory
          onUrlUpdate={onUrlUpdate}
        >
          <SystemIntegrationPage />
        </NuqsTestingAdapter>
      </QueryClientTestProvider>,
    )

    const user = userEvent.setup()
    await user.click(
      await screen.findByRole('button', { name: 'extend.systemManage.emailApi.title' }),
    )
    expect(screen.getByText('Email API boundary')).toBeVisible()
    expect(onUrlUpdate).not.toHaveBeenCalled()
    await waitFor(() =>
      expect(queryClient.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: true }),
    )
  })
})

describe('independent permission cache boundaries', () => {
  it('does not let an old account response restore Casdoor after logout and a denied next account', async () => {
    let resolveOldResponse!: (response: Response) => void
    let oldTransportResponse!: Promise<Response>
    permissionRequest = (_request, call) => {
      if (call === 1) {
        oldTransportResponse = new Promise((resolve) => {
          resolveOldResponse = resolve
        })
        return oldTransportResponse
      }
      return Promise.resolve(json({ can_manage_casdoor: false }))
    }

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
    const oldAccount = mountComposed(queryClient, 'normal', true)
    await screen.findByRole('status')
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    expect(requests.length).toBeGreaterThan(0)
    expect(
      requests.every(
        (request) =>
          request.method === 'GET' && new URL(request.url).pathname.endsWith('/permissions'),
      ),
    ).toBe(true)

    await userEvent.setup().click(screen.getByRole('button', { name: 'Log out old account' }))
    await waitFor(() =>
      expect(
        queryClient.getQueryCache().find({ queryKey: permissionKey(), exact: true }),
      ).toBeUndefined(),
    )
    expect(logoutRequests).toHaveLength(1)
    expect(logoutRequests[0]?.method).toBe('POST')
    expect(new URL(logoutRequests[0]!.url).pathname).toBe('/console/api/logout')
    expect(logoutPost).not.toHaveBeenCalled()
    expect(requests[0]?.signal.aborted).toBe(true)
    oldAccount.unmount()

    const requestsBeforeNextAccount = requests.length
    const nextAccount = mountComposed(queryClient, 'normal')
    await screen.findByRole('alert')
    expect(requests.length).toBeGreaterThan(requestsBeforeNextAccount)
    expect(queryClient.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: false })
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()

    await act(async () => {
      resolveOldResponse(json({ can_manage_casdoor: true }))
      await oldTransportResponse
      await Promise.resolve()
    })
    expect(queryClient.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: false })
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()
    nextAccount.unmount()
  })

  it('revokes the real page, layout, and navigation after a malformed permission refetch over cached true', async () => {
    let malformed = false
    permissionRequest = () =>
      Promise.resolve(json({ can_manage_casdoor: malformed ? 'true' : true }))
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    })
    const view = mountComposed(queryClient, 'normal')
    await screen.findByRole('button', { name: /casdoor.save$/ })
    expect(primary().getByRole('link')).toBeVisible()
    expect(queryClient.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: true })

    malformed = true
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: permissionKey() })
    })
    await screen.findByRole('alert')
    expect(queryClient.getQueryData(permissionKey())).toEqual({ can_manage_casdoor: 'true' })
    expect(primary().queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /casdoor.save$/ })).not.toBeInTheDocument()
    view.unmount()
  })
})
