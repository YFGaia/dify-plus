import type { GetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/types.gen'
import type { DeepPartial } from '@/test/console/system-features'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { seedSystemFeatures } from '@/test/console/query-data'
import NormalForm from '../normal-form'

const boundary = vi.hoisted(() => ({
  params: new URLSearchParams(),
  prefix: '/console/api',
  locale: 'zh-Hans',
  timezone: 'Asia/Shanghai' as string | undefined,
  profile: vi.fn(),
  invite: vi.fn(),
  replace: vi.fn(),
  push: vi.fn(),
  sso: vi.fn(),
  mail: vi.fn(),
}))
vi.mock('@/next/navigation', () => ({
  useSearchParams: () => boundary.params,
  useRouter: () => ({ replace: boundary.replace, push: boundary.push }),
}))
vi.mock('@/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config')>()),
  get API_PREFIX() {
    return boundary.prefix
  },
}))
vi.mock('@/context/i18n', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/context/i18n')>()),
  useLocale: () => boundary.locale,
}))
vi.mock('@/utils/timezone', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/utils/timezone')>()),
  getBrowserTimezone: () => boundary.timezone,
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
vi.mock('@/service/common', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/service/common')>()),
  invitationCheck: (...args: unknown[]) => boundary.invite(...args),
  sendEMailLoginCode: (...args: unknown[]) => boundary.mail(...args),
}))
vi.mock('@/service/sso', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/service/sso')>()),
  getUserSAMLSSOUrl: (...args: unknown[]) => boundary.sso(...args),
}))

const display = {
  enabled: true,
  button_text: 'Company sign in',
  start_path: '/console/api/auth/casdoor/login',
}
const response = (raw: unknown, status = 200) =>
  new Response(JSON.stringify(raw), {
    status,
    headers: { 'content-type': 'application/json' },
  })
const clients: QueryClient[] = []
const requests: Request[] = []
let fetchDisplay: ReturnType<typeof vi.fn<() => Promise<Response>>>

function mount(features: DeepPartial<GetSystemFeaturesResponse> = {}, client?: QueryClient) {
  const queryClient = client ?? new QueryClient({ defaultOptions: { queries: { retry: false } } })
  clients.push(queryClient)
  seedSystemFeatures(queryClient, { enable_email_password_login: false, ...features })
  return {
    ...render(
      <QueryClientProvider client={queryClient}>
        <NormalForm />
      </QueryClientProvider>,
    ),
    queryClient,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  boundary.params = new URLSearchParams()
  boundary.prefix = '/console/api'
  boundary.locale = 'zh-Hans'
  boundary.timezone = 'Asia/Shanghai'
  boundary.profile.mockRejectedValue(new Response(null, { status: 401 }))
  boundary.invite.mockResolvedValue({
    is_valid: true,
    data: { workspace_name: 'Acme', email: 'invitee@example.com' },
  })
  boundary.sso.mockResolvedValue({ url: '/synthetic-saml' })
  boundary.mail.mockResolvedValue({ result: 'success', data: 'synthetic-mail-token' })
  requests.length = 0
  fetchDisplay = vi.fn().mockImplementation(async () => response(display))
  vi.stubGlobal('Request', NativeRequest)
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const request = new Request(input, init)
    requests.push(request)
    if (!new URL(request.url).pathname.endsWith('/auth/casdoor/display'))
      throw new Error(`Unexpected request: ${request.url}`)
    return fetchDisplay()
  })
})
afterEach(() => {
  cleanup()
  for (const client of clients.splice(0)) client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('consumed invitation new Casdoor authorization entry', () => {
  const oldMethods = {
    enable_social_oauth_login: true,
    sso_enforced_for_signin: true,
    sso_enforced_for_signin_protocol: 'saml' as const,
    enable_email_code_login: true,
    enable_email_password_login: true,
    is_allow_register: true,
  }

  it.each([404, 503, 0])(
    'offers only neutral fresh Casdoor after invite check error %s',
    async (status) => {
      boundary.params = new URLSearchParams(
        'invite_token=invite-token&redirect_url=%2Fapps%2Finvited&account_id=untrusted&source=untrusted',
      )
      boundary.invite.mockRejectedValue(
        status ? new Response(null, { status }) : new Error('synthetic unknown'),
      )
      const user = userEvent.setup()
      const storageWrite = vi.fn()
      vi.stubGlobal('localStorage', { setItem: storageWrite })
      vi.stubGlobal('sessionStorage', { setItem: storageWrite })
      const beforeCookie = document.cookie
      mount(oldMethods)
      const link = await screen.findByRole('link', { name: display.button_text })
      expect(screen.queryByText('login.noLoginMethod')).not.toBeInTheDocument()
      expect(screen.queryByText('login.or')).not.toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'login.withGitHub' })).not.toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'login.withGoogle' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'login.withSSO' })).not.toBeInTheDocument()
      expect(screen.queryByLabelText('login.password')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('login.email')).not.toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'login.signup.signUp' })).not.toBeInTheDocument()
      const href = new URL(link.getAttribute('href')!, window.location.origin)
      expect(href.pathname).toBe('/console/api/auth/casdoor/login')
      expect(Object.fromEntries(href.searchParams)).toEqual({
        return_path: '/apps/invited',
        invite_token: 'invite-token',
        locale: 'zh-Hans',
        timezone: 'Asia/Shanghai',
      })
      let activated: string | null = null
      const navigation = (event: MouseEvent) => {
        activated = (event.target as HTMLElement).closest('a')?.getAttribute('href') ?? null
        event.preventDefault()
      }
      link.addEventListener('click', navigation)
      await user.click(link)
      link.removeEventListener('click', navigation)
      expect(activated).toBe(link.getAttribute('href'))
      expect(boundary.replace).not.toHaveBeenCalled()
      expect(boundary.push).not.toHaveBeenCalled()
      expect(boundary.sso).not.toHaveBeenCalled()
      expect(boundary.mail).not.toHaveBeenCalled()
      expect(requests).toHaveLength(1)
      expect(new URL(requests[0]!.url).pathname).toBe('/console/api/auth/casdoor/display')
      expect(storageWrite).not.toHaveBeenCalled()
      expect(document.cookie).toBe(beforeCookie)
    },
  )

  it.each([
    'invite_token=one&invite_token=two',
    'invite_token=%20padded',
    'invite_token=control%00',
    `invite_token=${'a'.repeat(513)}`,
  ])('preserves old error methods for invalid query %s', async (query) => {
    boundary.params = new URLSearchParams(query)
    boundary.invite.mockRejectedValue(new Error('synthetic check error'))
    mount(oldMethods)
    await screen.findByText('login.noLoginMethod')
    await waitFor(() => expect(fetchDisplay).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'login.withGitHub' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'login.withSSO' })).toBeInTheDocument()
    expect(screen.getByLabelText('login.password')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'login.signup.signUp' })).toBeInTheDocument()
    expect(screen.getByText('login.or')).toBeInTheDocument()
  })

  it.each(['disabled', 'error', 'pending'] as const)(
    'preserves fallback during display %s',
    async (mode) => {
      boundary.params = new URLSearchParams('invite_token=invite-token')
      boundary.invite.mockRejectedValue(new Error('synthetic check error'))
      if (mode === 'disabled')
        fetchDisplay.mockImplementation(async () => response({ ...display, enabled: false }))
      if (mode === 'error') fetchDisplay.mockImplementation(async () => response({}, 503))
      if (mode === 'pending') fetchDisplay.mockReturnValue(new Promise(() => {}))
      mount(oldMethods)
      await screen.findByText('login.noLoginMethod')
      expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
      expect(screen.getByLabelText('login.password')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'login.withGitHub' })).toBeInTheDocument()
    },
  )

  it('requires fresh display while refetching a previously enabled response', async () => {
    boundary.params = new URLSearchParams('invite_token=invite-token')
    boundary.invite.mockRejectedValue(new Error('synthetic check error'))
    const { queryClient } = mount(oldMethods)
    await screen.findByRole('link', { name: display.button_text })
    const next = Promise.withResolvers<Response>()
    fetchDisplay.mockReturnValue(next.promise)
    await act(async () => {
      void queryClient.refetchQueries()
    })
    await screen.findByLabelText('login.password')
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    await act(async () => next.resolve(response(display)))
    await screen.findByRole('link', { name: display.button_text })
    expect(screen.queryByLabelText('login.password')).not.toBeInTheDocument()
  })

  it('keeps the valid first invitation entry and old methods before consumption', async () => {
    boundary.params = new URLSearchParams(`invite_token=${'a'.repeat(512)}`)
    mount(oldMethods)
    const link = await screen.findByRole('link', { name: display.button_text })
    expect(
      new URL(link.getAttribute('href')!, window.location.origin).searchParams.get('invite_token'),
    ).toHaveLength(512)
    expect(screen.getByLabelText('login.password')).toBeInTheDocument()
    expect(screen.getByText('login.or')).toBeInTheDocument()
  })
})
