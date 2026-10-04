import type { GetSystemFeaturesResponse } from '@dify/contracts/api/console/system-features/types.gen'
import type { ReactNode } from 'react'
import type { DeepPartial } from '@/test/console/system-features'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { Component } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { casdoorDisplayQueryOptions } from '@/features/casdoor/signin/client'
import { consoleQuery } from '@/service/console'
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

describe('ordinary native Casdoor sign in', () => {
  it('shows Casdoor as the sole method after the ordinary profile 401', async () => {
    mount()
    expect(await screen.findByRole('link', { name: display.button_text })).toBeInTheDocument()
    expect(screen.queryByText('login.or')).not.toBeInTheDocument()
    expect(screen.queryByText('login.noLoginMethod')).not.toBeInTheDocument()
    expect(fetchDisplay).toHaveBeenCalledTimes(1)
    expect(requests[0]?.method).toBe('GET')
    expect(requests[0]?.credentials).toBe('include')
    expect(requests[0]?.cache).toBe('no-store')
    expect(new URL(requests[0]!.url).search).toBe('')
    expect(boundary.replace).not.toHaveBeenCalled()
  })

  it('keeps old methods, OR, password/code switching, registration and SAML activation', async () => {
    const user = userEvent.setup()
    mount({
      enable_social_oauth_login: true,
      sso_enforced_for_signin: true,
      sso_enforced_for_signin_protocol: 'saml',
      enable_email_code_login: true,
      enable_email_password_login: true,
      is_allow_register: true,
    })
    await screen.findByRole('link', { name: display.button_text })
    expect(screen.getByText('login.or')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'login.withGitHub' })).toHaveAttribute(
      'href',
      expect.stringContaining('/oauth/login/github'),
    )
    expect(screen.getByRole('link', { name: 'login.withGoogle' })).toHaveAttribute(
      'href',
      expect.stringContaining('/oauth/login/google'),
    )
    expect(screen.getByRole('link', { name: 'login.signup.signUp' })).toHaveAttribute(
      'href',
      '/signup',
    )
    await user.click(screen.getByRole('button', { name: 'login.withSSO' }))
    await waitFor(() => expect(boundary.push).toHaveBeenCalledWith('/synthetic-saml'))
    expect(boundary.sso).toHaveBeenCalledWith('')
    await user.click(screen.getByRole('button', { name: 'login.useVerificationCode' }))
    expect(screen.queryByLabelText('login.password')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('login.email'), 'person@example.com')
    await user.click(screen.getByRole('button', { name: 'login.signup.verifyMail' }))
    await waitFor(() =>
      expect(boundary.mail).toHaveBeenCalledWith('person@example.com', 'zh-Hans', undefined),
    )
    await user.click(screen.getByRole('button', { name: 'login.usePassword' }))
    expect(screen.getByLabelText('login.password')).toBeInTheDocument()
  })

  it.each([false, true])(
    'disabled display preserves old no-method and OR behavior; email=%s',
    async (email) => {
      fetchDisplay.mockImplementation(async () => response({ ...display, enabled: false }))
      mount({ enable_email_password_login: email })
      await screen.findByRole('heading', { level: 1 })
      await waitFor(() => expect(screen.queryByRole('status')).not.toBeInTheDocument())
      expect(Boolean(screen.queryByText('login.noLoginMethod'))).toBe(!email)
      expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
      expect(screen.queryByText('login.or')).not.toBeInTheDocument()
    },
  )

  it('keeps old email usable during display pending and only adds OR on success', async () => {
    const pending = Promise.withResolvers<Response>()
    fetchDisplay.mockReturnValue(pending.promise)
    mount({ enable_email_password_login: true })
    expect(await screen.findByLabelText('login.password')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('common.loading')
    expect(screen.queryByText('login.or')).not.toBeInTheDocument()
    await act(async () => pending.resolve(response(display)))
    await screen.findByRole('link', { name: display.button_text })
    expect(screen.getByText('login.or')).toBeInTheDocument()
  })

  it('waits locally, reports a safe error and retries only on manual activation', async () => {
    const user = userEvent.setup()
    const pending = Promise.withResolvers<Response>()
    fetchDisplay
      .mockReturnValueOnce(pending.promise)
      .mockImplementation(async () => response(display))
    mount()
    await screen.findByRole('heading', { level: 1 })
    expect(screen.getByRole('status')).toHaveTextContent('common.loading')
    expect(screen.queryByText('login.noLoginMethod')).not.toBeInTheDocument()
    await act(async () => pending.resolve(response({ secret: 'synthetic-private' }, 503)))
    expect(await screen.findByRole('alert')).toHaveTextContent('common.api.actionFailed')
    expect(screen.queryByText('login.noLoginMethod')).not.toBeInTheDocument()
    expect(screen.queryByText('synthetic-private')).not.toBeInTheDocument()
    expect(fetchDisplay).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'common.operation.retry' }))
    await screen.findByRole('link', { name: display.button_text })
    expect(fetchDisplay).toHaveBeenCalledTimes(2)
    expect(boundary.push).not.toHaveBeenCalled()
  })

  it('removes stale links and OR during refetch and after failure', async () => {
    const { queryClient } = mount({ enable_email_password_login: true })
    await screen.findByRole('link', { name: display.button_text })
    const pending = Promise.withResolvers<Response>()
    fetchDisplay.mockReturnValue(pending.promise)
    let refetch: Promise<void>
    act(() => {
      refetch = queryClient.invalidateQueries({
        queryKey: consoleQuery.auth.casdoor.display.get.queryOptions().queryKey,
      })
    })
    await screen.findByRole('status')
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    expect(screen.queryByText('login.or')).not.toBeInTheDocument()
    await act(async () => {
      pending.resolve(response({}, 503))
      await refetch
    })
    await screen.findByRole('alert')
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    expect(screen.getByLabelText('login.password')).toBeInTheDocument()
  })

  it('refetches on remount and never displays a cached enabled button while checking', async () => {
    const first = mount()
    await screen.findByRole('link', { name: display.button_text })
    first.unmount()
    const pending = Promise.withResolvers<Response>()
    fetchDisplay.mockReturnValue(pending.promise)
    mount({}, first.queryClient)
    await screen.findByRole('status')
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    await act(async () => pending.resolve(response({ ...display, enabled: false })))
    await screen.findByText('login.noLoginMethod')
    expect(fetchDisplay).toHaveBeenCalledTimes(2)
  })

  it.each([
    ['empty defaults', {}],
    ['null', null],
    ['array', []],
    ['missing label', { enabled: true, start_path: display.start_path }],
    ['missing enabled', { button_text: 'Company', start_path: display.start_path }],
    ['missing path', { enabled: true, button_text: 'Company' }],
    ['wrong boolean', { ...display, enabled: 'true' }],
    ['wrong text', { ...display, button_text: 1 }],
    ['wrong path type', { ...display, start_path: false }],
    ['extra PII', { ...display, email: 'private@example.test' }],
    ['empty text', { ...display, button_text: '' }],
    ['long text', { ...display, button_text: 'a'.repeat(121) }],
    ['external path', { ...display, start_path: 'https://evil.test/login' }],
  ])('rejects raw %s before generated defaults can authorize an entry', async (_name, raw) => {
    fetchDisplay.mockImplementation(async () => response(raw))
    mount()
    await screen.findByRole('alert')
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    expect(screen.queryByText('login.noLoginMethod')).not.toBeInTheDocument()
    expect(fetchDisplay).toHaveBeenCalledTimes(1)
    expect(boundary.replace).not.toHaveBeenCalled()
  })

  it('rejects own undefined values and inherited fields at the actual select boundary', () => {
    const select = casdoorDisplayQueryOptions(true).select!
    for (const raw of [
      { ...display, enabled: undefined },
      Object.create(display),
      { ...display, [Symbol('extra')]: 1 },
    ])
      expect(() => select(raw)).toThrow()
  })

  it.each(['a', 'a'.repeat(120), '<img src=x onerror=alert(1)>'])(
    'renders valid server text literally: %s',
    async (text) => {
      fetchDisplay.mockImplementation(async () => response({ ...display, button_text: text }))
      mount()
      const link = await screen.findByRole('link', { name: text })
      expect(link.textContent).toBe(text)
      expect(link.querySelector('img')).toBeNull()
    },
  )

  it.each([
    '/console/api',
    '/console/api/',
    'https://api.example.test/console/api',
    'https://api.example.test/console/api/',
    '/dify/console/api/',
  ])(
    'constructs fixed native navigation from trusted prefix %s without side effects',
    async (prefix) => {
      boundary.prefix = prefix
      boundary.params = new URLSearchParams({
        redirect_url: '/dify/apps',
        token: 'private',
        account: 'private',
        source: 'private',
        mode: 'private',
        init: '1',
        handoff: 'private',
        locale: 'evil',
        timezone: 'evil',
      })
      const storage = vi.spyOn(Storage.prototype, 'setItem')
      const remove = vi.spyOn(Storage.prototype, 'removeItem')
      const cookie = vi.spyOn(document, 'cookie', 'set')
      const assign = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
      mount()
      const link = await screen.findByRole('link', { name: display.button_text })
      const href = link.getAttribute('href')!
      expect(href.split('?')[0]).toBe(`${prefix.replace(/\/+$/, '')}/auth/casdoor/login`)
      expect([...new URL(href, window.location.origin).searchParams]).toEqual([
        ['return_path', '/dify/apps'],
        ['locale', 'zh-Hans'],
        ['timezone', 'Asia/Shanghai'],
      ])
      expect(storage).not.toHaveBeenCalled()
      expect(remove).not.toHaveBeenCalled()
      expect(cookie).not.toHaveBeenCalled()
      expect(assign).not.toHaveBeenCalled()
      expect(boundary.push).not.toHaveBeenCalled()
      expect(boundary.replace).not.toHaveBeenCalled()
      // Cancel only the browser default; the real anchor is still the activation owner.
      const activation = vi.fn((event: Event) => event.preventDefault())
      link.addEventListener('click', activation)
      const user = userEvent.setup()
      link.focus()
      await user.keyboard('{Enter}')
      expect(activation).toHaveBeenCalledTimes(1)
      expect(fetchDisplay).toHaveBeenCalledTimes(1)
    },
  )

  it.each([
    'https://dify.ai/apps',
    'https://localhost:3000/apps',
    '//evil.test/apps',
    '/apps?x=1',
    '/apps#x',
    '/%61pps',
    '%2Fapps',
    '/apps\\evil',
    '/apps//evil',
    '/apps/../settings',
    '/apps/./settings',
    '/apps\u0001',
    '/apps\u007F',
    '/ apps ',
    `/${'a'.repeat(2048)}`,
  ])('rejects raw return candidate before resolver normalization: %j', async (candidate) => {
    boundary.params = new URLSearchParams({ redirect_url: candidate })
    mount()
    const link = await screen.findByRole('link', { name: display.button_text })
    expect(
      new URL(link.getAttribute('href')!, window.location.origin).searchParams.get('return_path'),
    ).toBe('/apps')
  })

  it('rejects duplicate return candidates and invalid hints without passing through the query', async () => {
    boundary.params = new URLSearchParams(
      'redirect_url=/apps&redirect_url=/settings&arbitrary=secret',
    )
    boundary.locale = 'en&token=secret'
    boundary.timezone = 'Not/A_Timezone'
    mount()
    const link = await screen.findByRole('link', { name: display.button_text })
    expect([...new URL(link.getAttribute('href')!, window.location.origin).searchParams]).toEqual([
      ['return_path', '/apps'],
    ])
  })

  it.each(['lost', 'expired', 'inactive'] as const)(
    'keeps license %s blocking the login form',
    async (status) => {
      mount({ license: { status } })
      await screen.findByText(`login.license${status[0]!.toUpperCase()}${status.slice(1)}`)
      expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    },
  )

  it('preserves profile loading and authorized redirect', async () => {
    const profile = Promise.withResolvers<unknown>()
    boundary.profile.mockReturnValue(profile.promise)
    mount()
    await waitFor(() => expect(fetchDisplay).toHaveBeenCalledTimes(1))
    expect(screen.queryByRole('heading')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
    await act(async () => profile.resolve({ profile: { email: 'person@example.com' } }))
    await waitFor(() =>
      expect(boundary.replace).toHaveBeenCalledWith('/explore/apps-center-extend'),
    )
  })

  it.each(['pending', 'error', 'different', 'matching'] as const)(
    'preserves invitation %s without querying or showing Casdoor',
    async (mode) => {
      boundary.params = new URLSearchParams('invite_token=invite-token')
      if (mode === 'pending') boundary.invite.mockReturnValue(new Promise(() => {}))
      if (mode === 'error') boundary.invite.mockRejectedValue(new Error('synthetic-invite-failure'))
      if (mode === 'different' || mode === 'matching')
        boundary.profile.mockResolvedValue({
          profile: { email: mode === 'matching' ? 'Invitee@Example.com' : 'other@example.com' },
        })
      mount({ enable_email_password_login: true })
      await waitFor(() => expect(boundary.invite).toHaveBeenCalledTimes(1))
      if (mode === 'error') await screen.findByText('login.noLoginMethod')
      if (mode === 'different') await screen.findByRole('button', { name: 'login.signBtn' })
      if (mode === 'matching')
        await waitFor(() =>
          expect(boundary.replace).toHaveBeenCalledWith(
            '/signin/invite-settings?invite_token=invite-token',
          ),
        )
      if (mode === 'pending') expect(screen.queryByRole('heading')).not.toBeInTheDocument()
      expect(fetchDisplay).not.toHaveBeenCalled()
      expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
      expect(screen.queryByText('login.or')).not.toBeInTheDocument()
    },
  )

  it('keeps non-401 profile errors in the existing error boundary', async () => {
    class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
      override state = { failed: false }
      static getDerivedStateFromError() {
        return { failed: true }
      }
      override render() {
        return this.state.failed ? <p>Profile unavailable</p> : this.props.children
      }
    }
    boundary.profile.mockRejectedValue(new Error('synthetic-profile-failure'))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    clients.push(client)
    seedSystemFeatures(client)
    render(
      <QueryClientProvider client={client}>
        <Boundary>
          <NormalForm />
        </Boundary>
      </QueryClientProvider>,
    )
    await screen.findByText('Profile unavailable')
    expect(screen.queryByRole('link', { name: display.button_text })).not.toBeInTheDocument()
  })
})
