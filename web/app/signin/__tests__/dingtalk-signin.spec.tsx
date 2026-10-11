import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { API_PREFIX } from '@/config'
import { seedSystemFeatures } from '@/test/console/query-data'
import NormalForm from '../normal-form'

const boundary = vi.hoisted(() => ({
  params: new URLSearchParams(),
  profile: vi.fn(),
  replace: vi.fn(),
}))
vi.mock('@/next/navigation', () => ({
  useSearchParams: () => boundary.params,
  useRouter: () => ({ replace: boundary.replace, push: vi.fn() }),
}))
vi.mock('@/features/account-profile/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/account-profile/client')>()
  return {
    ...actual,
    userProfileQueryOptions: () => ({
      ...actual.userProfileQueryOptions(),
      queryFn: boundary.profile,
      retry: false,
    }),
  }
})

const display = {
  enabled: true,
  button_text: '钉钉',
  start_path: '/console/api/auth/dingtalk/start',
}
const response = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
const clients: QueryClient[] = []
const requests: Request[] = []
let fetchDisplay: ReturnType<typeof vi.fn<() => Promise<Response>>>
const mount = (emailLogin = false) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(client)
  seedSystemFeatures(client, {
    enable_email_password_login: emailLogin,
    enable_email_code_login: false,
    enable_social_oauth_login: false,
  })
  return render(
    <QueryClientProvider client={client}>
      <NormalForm />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  requests.length = 0
  boundary.params = new URLSearchParams()
  boundary.profile.mockRejectedValue(new Response(null, { status: 401 }))
  fetchDisplay = vi.fn().mockResolvedValue(response(display))
  vi.stubGlobal('Request', NativeRequest)
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const request = new Request(input, init)
    const path = new URL(request.url).pathname
    if (path.endsWith('/auth/casdoor/display'))
      return response({
        enabled: false,
        button_text: 'Casdoor',
        start_path: '/console/api/auth/casdoor/login',
      })
    if (path.endsWith('/auth/dingtalk/display')) {
      requests.push(request)
      return fetchDisplay()
    }
    throw new Error(`Unexpected request: ${path}`)
  })
})
afterEach(() => {
  cleanup()
  for (const client of clients.splice(0)) client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('independent DingTalk sign-in entry', () => {
  it('reads enabled status through the generated API and navigates to the backend transaction start', async () => {
    mount()
    const entry = await screen.findByRole('link', { name: 'extend.sidebar.withDingTalk' })
    expect(entry).toHaveAttribute('href', `${API_PREFIX.replace(/\/+$/, '')}/auth/dingtalk/start`)
    expect(requests[0]?.method).toBe('GET')
    expect(requests[0]?.credentials).toBe('include')
    expect(requests[0]?.cache).toBe('no-store')
    expect(screen.queryByText('login.noLoginMethod')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Casdoor' })).not.toBeInTheDocument()
  })

  it('hides a disabled provider and reports no configured login methods', async () => {
    fetchDisplay.mockResolvedValue(response({ ...display, enabled: false }))
    mount()
    expect(await screen.findByText('login.noLoginMethod')).toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: 'extend.sidebar.withDingTalk' }),
    ).not.toBeInTheDocument()
  })

  it('keeps local email login available while the provider display API fails', async () => {
    fetchDisplay.mockResolvedValue(response({}, 503))
    mount(true)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.dingtalk.loginFailed',
    )
    expect(screen.getByPlaceholderText('login.emailPlaceholder')).toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: 'extend.sidebar.withDingTalk' }),
    ).not.toBeInTheDocument()
  })

  it('shows a localized explicit binding instruction from the callback code', async () => {
    boundary.params = new URLSearchParams('dingtalk_error=binding_required')
    mount()
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.dingtalk.accountBindingRequired',
    )
    expect(screen.getByRole('link', { name: 'extend.sidebar.withDingTalk' })).toBeInTheDocument()
  })

  it('explains missing enterprise email without suggesting callback or permission failures', async () => {
    boundary.params = new URLSearchParams('dingtalk_error=enterprise_email_required')
    mount(true)
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'extend.systemManage.dingtalk.enterpriseEmailRequired',
    )
    expect(screen.queryByText('extend.systemManage.dingtalk.loginFailed')).not.toBeInTheDocument()
    expect(screen.getByPlaceholderText('login.emailPlaceholder')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'extend.sidebar.withDingTalk' })).toBeInTheDocument()
  })

  it('rejects a malformed display response instead of advertising a login method', async () => {
    fetchDisplay.mockResolvedValue(response({ ...display, enabled: 'true' }))
    mount(true)
    await waitFor(() => expect(fetchDisplay).toHaveBeenCalledTimes(1))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: 'extend.sidebar.withDingTalk' }),
    ).not.toBeInTheDocument()
  })
})
