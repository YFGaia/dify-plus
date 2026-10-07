import {
  focusManager,
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { StrictMode } from 'react'
import { renderToString } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import en from '@/i18n/en-US/extend.json'
import zh from '@/i18n/zh-Hans/extend.json'
import { createSystemFeaturesFixture } from '@/test/console/system-features'

const handoff = 'A'.repeat(43)
const path = '/portal/signin/casdoor-result'
const correlation = '12345678-abcd-4123-8123-123456789abc'
const payload = { code: 'authorization_pending', correlation_id: correlation, retry_allowed: false }
const copy = (key: keyof typeof en) => en[key]
const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
const deferred = () => Promise.withResolvers<void>()

async function loadFeature(
  delay?: { entered: ReturnType<typeof deferred>; release: ReturnType<typeof deferred> },
  realTranslations = false,
) {
  vi.resetModules()
  vi.doMock('@/config', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/config')>()),
    API_PREFIX: '/console/api',
  }))
  vi.doMock('@/utils/var', async (importOriginal) => ({
    ...(await importOriginal<typeof import('@/utils/var')>()),
    basePath: '/portal',
  }))
  vi.doMock('@/utils/client', () => ({ isClient: true, isServer: false }))
  vi.doMock('react-i18next', async () => {
    if (realTranslations) return vi.importActual<typeof import('react-i18next')>('react-i18next')
    const { createReactI18nextMock } = await import('@/test/i18n-mock')
    return createReactI18nextMock(en)
  })
  vi.doMock('@/service/refresh-token', () => ({ refreshAccessTokenOrReLogin: vi.fn() }))
  const loaderCalls = vi.fn()
  vi.doMock('@dify/contracts/api/console/orpc.gen', async (importOriginal) => {
    const actual = await importOriginal<typeof import('@dify/contracts/api/console/orpc.gen')>()
    return {
      ...actual,
      contractLoaders: {
        ...actual.contractLoaders,
        auth: async () => {
          loaderCalls()
          if (delay) {
            delay.entered.resolve()
            await delay.release.promise
          }
          return actual.contractLoaders.auth()
        },
      },
    }
  })
  const feature = await import('..')
  const refresh = await import('@/service/refresh-token')
  return { ...feature, refresh: refresh.refreshAccessTokenOrReLogin, loaderCalls }
}

function setURL(suffix = `?handoff=${handoff}#synthetic-fragment`) {
  window.history.replaceState(null, '', `${path}${suffix}`)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Request', NativeRequest)
  setURL()
})

afterEach(async () => {
  await act(async () => {
    cleanup()
  })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  for (const name of [
    '@/config',
    '@/utils/var',
    '@/utils/client',
    'react-i18next',
    '@/service/refresh-token',
    '@dify/contracts/api/console/orpc.gen',
    '@/i18n-config/server',
    'server-only',
    '@/app/signin/_header',
  ])
    vi.doUnmock(name)
  focusManager.setFocused(undefined)
  onlineManager.setOnline(true)
})

describe('one-use Casdoor result page with native Console transport', () => {
  it('consumes once under real StrictMode, clears URL synchronously before fetch, and never writes auth storage', async () => {
    const events: string[] = []
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      events.push('fetch')
      expect(window.location.pathname).toBe(path)
      expect(window.location.search).toBe('')
      expect(window.location.hash).toBe('')
      const request = new Request(input)
      expect(request.method).toBe('GET')
      expect([...new URL(request.url).searchParams]).toEqual([['handoff', handoff]])
      expect(request.credentials).toBe('include')
      expect(request.cache).toBe('no-store')
      expect(request.redirect).toBe('error')
      expect(request.headers.has('Authorization')).toBe(false)
      expect(request.body).toBeNull()
      return response(payload)
    })
    const originalReplace = window.history.replaceState.bind(window.history)
    const replace = vi
      .spyOn(window.history, 'replaceState')
      .mockImplementation((data, unused, url) => {
        events.push('clear')
        originalReplace(data, unused, url)
        queueMicrotask(() => events.push('microtask'))
      })
    const localWrite = vi.spyOn(window.localStorage, 'setItem')
    const sessionWrite = vi.spyOn(window.sessionStorage, 'setItem')
    const cookieWrite = vi.spyOn(document, 'cookie', 'set')
    const { CasdoorSigninResult, refresh } = await loadFeature()
    const view = render(
      <StrictMode>
        <CasdoorSigninResult />
      </StrictMode>,
    )
    expect(screen.getByRole('status')).toHaveTextContent(copy('casdoorSigninResult.loading'))
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.pending'))
    expect(screen.getByRole('alert')).toHaveTextContent(correlation)
    expect(events).toEqual(['clear', 'fetch', 'microtask'])
    expect(replace).toHaveBeenCalledExactlyOnceWith(null, '', path)
    view.rerender(
      <StrictMode>
        <CasdoorSigninResult />
      </StrictMode>,
    )
    await act(async () => {
      focusManager.setFocused(false)
      focusManager.setFocused(true)
      onlineManager.setOnline(false)
      onlineManager.setOnline(true)
    })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(refresh).not.toHaveBeenCalled()
    expect(localWrite).not.toHaveBeenCalled()
    expect(sessionWrite).not.toHaveBeenCalled()
    expect(cookieWrite).not.toHaveBeenCalled()
  })

  it.each([
    ['role_snapshot_unknown', 'casdoorSigninResult.unknownRole'],
    ['workspace_unavailable', 'casdoorSigninResult.workspaceUnavailable'],
    ['authorization_pending', 'casdoorSigninResult.pending'],
  ] as const)('displays only validated %s and its correlation', async (code, key) => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({ ...payload, code }))
    const { CasdoorSigninResult } = await loadFeature()
    render(<CasdoorSigninResult />)
    expect(await screen.findByRole('alert')).toHaveTextContent(copy(key))
    expect(screen.getByRole('alert')).toHaveTextContent(correlation)
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([
    ['extra PII', { ...payload, email: 'synthetic-private@example.test' }],
    ['retry true', { ...payload, retry_allowed: true }],
    ['missing code', { correlation_id: correlation, retry_allowed: false }],
    ['missing correlation', { code: 'authorization_pending', retry_allowed: false }],
    ['missing retry', { code: 'authorization_pending', correlation_id: correlation }],
    ['unknown code', { ...payload, code: 'synthetic-private@example.test' }],
    ['uppercase UUID', { ...payload, correlation_id: correlation.toUpperCase() }],
    [
      'invalid UUID variant',
      { ...payload, correlation_id: '12345678-abcd-4123-0123-123456789abc' },
    ],
    ['non UUID', { ...payload, correlation_id: 'synthetic-private@example.test' }],
    ['null body', null],
  ])('rejects raw %s before exposing any payload field', async (_label, raw) => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(raw))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { CasdoorSigninResult } = await loadFeature()
    render(<CasdoorSigninResult />)
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.generic'))
    expect(screen.queryByText(/synthetic-private|Correlation ID/)).not.toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(log).not.toHaveBeenCalled()
  })

  it.each([
    '',
    '?handoff=',
    '?handoff=short',
    `?handoff=${'A'.repeat(42)}B`,
    `?handoff=${'A'.repeat(44)}`,
    `?handoff=${handoff}&handoff=${handoff}`,
    `?handoff=${handoff}&code=authorization_pending`,
    `?code=authorization_pending&correlation_id=${correlation}`,
    `?handoff=${'!'.repeat(43)}`,
  ])('does not call the native operation for invalid input %s', async (suffix) => {
    setURL(suffix)
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    const { CasdoorSigninResult, loaderCalls } = await loadFeature()
    render(<CasdoorSigninResult />)
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.expired'))
    expect(fetch).not.toHaveBeenCalled()
    expect(loaderCalls).not.toHaveBeenCalled()
    expect(screen.queryByText(/Correlation ID/)).not.toBeInTheDocument()
  })

  it('rejects an initial different pathname', async () => {
    window.history.replaceState(null, '', `/signin/casdoor-result?handoff=${handoff}`)
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    const { CasdoorSigninResult } = await loadFeature()
    render(<CasdoorSigninResult />)
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.expired'))
    expect(fetch).not.toHaveBeenCalled()
  })

  it('does not consume during server rendering, metadata generation or repeated route render preparation', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    await loadFeature()
    vi.doMock('server-only', () => ({}))
    vi.doMock('@/i18n-config/server', async () => {
      const { withSelectorKey } = await import('@/test/i18n-mock')
      return {
        getLocaleOnServer: async () => 'en-US',
        getTranslation: async () => ({
          t: withSelectorKey((key) => en[key as keyof typeof en], 'extend'),
        }),
      }
    })
    const { default: Page, generateMetadata } = await import('@/app/signin/casdoor-result/page')
    expect(await generateMetadata()).toEqual({
      title: copy('casdoorSigninResult.title'),
      referrer: 'no-referrer',
    })
    expect(renderToString(<Page />)).toContain(copy('casdoorSigninResult.loading'))
    expect(renderToString(<Page />)).not.toContain(handoff)
    expect(Page().props).toEqual({})
    expect(fetch).not.toHaveBeenCalled()
    expect(window.location.search).toBe(`?handoff=${handoff}`)
  })

  it('cancels a setup before its command microtask without dispatch', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    const { CasdoorSigninResult } = await loadFeature()
    const view = render(<CasdoorSigninResult />)
    view.unmount()
    await act(async () => {})
    expect(fetch).not.toHaveBeenCalled()
    expect(window.location.search).toBe(`?handoff=${handoff}`)
  })

  it.each(['unmount', 'changed handle', 'changed pathname', 'extra query', 'replace failure'])(
    'rechecks the live owner after the actual lazy loader: %s',
    async (mode) => {
      const delay = { entered: deferred(), release: deferred() }
      const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { CasdoorSigninResult } = await loadFeature(delay)
      const view = render(<CasdoorSigninResult />)
      await act(async () => {
        await delay.entered.promise
      })
      expect(fetch).not.toHaveBeenCalled()
      expect(window.location.search).toBe(`?handoff=${handoff}`)
      if (mode === 'unmount') view.unmount()
      if (mode === 'changed handle') setURL(`?handoff=${'B'.repeat(42)}A`)
      if (mode === 'changed pathname')
        window.history.replaceState(null, '', `/portal/signin?handoff=${handoff}`)
      if (mode === 'extra query') setURL(`?handoff=${handoff}&code=synthetic-private`)
      if (mode === 'replace failure')
        vi.spyOn(window.history, 'replaceState').mockImplementation(() => {
          throw new Error('synthetic-private')
        })
      await act(async () => {
        delay.release.resolve()
      })
      if (mode !== 'unmount')
        expect(await screen.findByRole('alert')).toHaveTextContent(
          copy('casdoorSigninResult.generic'),
        )
      expect(fetch).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
    },
  )

  it('allows only the live remount to dispatch when the previous owner unmounted during the lazy loader', async () => {
    const delay = { entered: deferred(), release: deferred() }
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    const { CasdoorSigninResult } = await loadFeature(delay)
    const oldView = render(<CasdoorSigninResult />)
    await act(async () => {
      await delay.entered.promise
    })
    oldView.unmount()
    render(
      <StrictMode>
        <CasdoorSigninResult />
      </StrictMode>,
    )
    await act(async () => {
      delay.release.resolve()
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.pending'))
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('lets only the first of two overlapping mounted owners consume the same live URL', async () => {
    const delay = { entered: deferred(), release: deferred() }
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    const { CasdoorSigninResult } = await loadFeature(delay)
    render(
      <>
        <CasdoorSigninResult />
        <CasdoorSigninResult />
      </>,
    )
    await act(async () => {
      await delay.entered.promise
    })
    expect(fetch).not.toHaveBeenCalled()
    await act(async () => {
      delay.release.resolve()
    })
    expect(await screen.findByText(copy('casdoorSigninResult.pending'))).toBeInTheDocument()
    expect(await screen.findByText(copy('casdoorSigninResult.generic'))).toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('does not abort a dispatched request or reconsume after unmount and a fresh mount', async () => {
    const reply = Promise.withResolvers<Response>()
    let signal: AbortSignal | undefined
    const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation((input) => {
      signal = new Request(input).signal
      return reply.promise
    })
    const { CasdoorSigninResult } = await loadFeature()
    const view = render(<CasdoorSigninResult />)
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    view.unmount()
    expect(signal?.aborted).toBe(false)
    render(<CasdoorSigninResult />)
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.expired'))
    await act(async () => {
      reply.resolve(response(payload))
    })
    expect(screen.getByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.expired'))
    expect(screen.queryByText(/Correlation ID/)).not.toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('keeps one command across real locale changes and rerenders while pending and after settlement', async () => {
    const reply = Promise.withResolvers<Response>()
    const fetch = vi.spyOn(globalThis, 'fetch').mockReturnValue(reply.promise)
    const { CasdoorSigninResult } = await loadFeature(undefined, true)
    const { createInstance } = await import('i18next')
    const { I18nextProvider, initReactI18next } = await import('react-i18next')
    const i18n = createInstance()
    await i18n.use(initReactI18next).init({
      lng: 'en-US',
      fallbackLng: 'en-US',
      keySeparator: false,
      resources: { 'en-US': { extend: en }, 'zh-Hans': { extend: zh } },
    })
    const view = render(
      <I18nextProvider i18n={i18n}>
        <CasdoorSigninResult />
      </I18nextProvider>,
    )
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    await act(async () => {
      await i18n.changeLanguage('zh-Hans')
    })
    expect(screen.getByRole('status')).toHaveTextContent(zh['casdoorSigninResult.loading'])
    view.rerender(
      <I18nextProvider i18n={i18n}>
        <CasdoorSigninResult />
      </I18nextProvider>,
    )
    await act(async () => {
      reply.resolve(response(payload))
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(zh['casdoorSigninResult.pending'])
    await act(async () => {
      await i18n.changeLanguage('en-US')
    })
    expect(screen.getByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.pending'))
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it.each([400, 401, 503, 'network', 'lost reply', 'malformed JSON'] as const)(
    'shows fixed recovery with no automatic retry or raw logs for %s',
    async (fault) => {
      const fetch = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => {
        if (typeof fault === 'number')
          return response({ message: 'synthetic-private', correlation_id: correlation }, fault)
        if (fault === 'malformed JSON')
          return new Response('synthetic-private', {
            headers: { 'content-type': 'application/json' },
          })
        throw new Error(`synthetic-private ${fault}`)
      })
      const log = vi.spyOn(console, 'error').mockImplementation(() => {})
      const { CasdoorSigninResult, refresh } = await loadFeature()
      const view = render(
        <StrictMode>
          <CasdoorSigninResult />
        </StrictMode>,
      )
      expect(await screen.findByRole('alert')).toHaveTextContent(
        copy('casdoorSigninResult.generic'),
      )
      view.rerender(
        <StrictMode>
          <CasdoorSigninResult />
        </StrictMode>,
      )
      await act(async () => {
        focusManager.setFocused(false)
        focusManager.setFocused(true)
        onlineManager.setOnline(false)
        onlineManager.setOnline(true)
      })
      expect(fetch).toHaveBeenCalledTimes(1)
      expect(refresh).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
      expect(screen.queryByText(/synthetic-private|Correlation ID/)).not.toBeInTheDocument()
      expect(window.location.pathname).toBe(path)
      expect(window.location.search).toBe('')
    },
  )

  it('preserves the parent main landmark and offers a native keyboard-focusable fixed sign-in link', async () => {
    setURL('')
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response(payload))
    const { CasdoorSigninResult } = await loadFeature()
    // The unrelated branding/menu boundary is outside this page; its main-owning layout is real.
    vi.doMock('@/app/signin/_header', () => ({ default: () => <header>Brand</header> }))
    const { default: Layout } = await import('@/app/signin/layout')
    const { systemFeaturesQueryOptions } = await import('@/features/system-features/client')
    const queryClient = new QueryClient()
    const options = systemFeaturesQueryOptions()
    queryClient.setQueryData(options.queryKey, createSystemFeaturesFixture())
    render(
      <QueryClientProvider client={queryClient}>
        <Layout>
          <CasdoorSigninResult />
        </Layout>
      </QueryClientProvider>,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.expired'))
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(within(screen.getByRole('main')).getByRole('heading', { level: 1 })).toHaveTextContent(
      copy('casdoorSigninResult.title'),
    )
    const link = screen.getByRole('link', { name: copy('casdoorSigninResult.freshSignin') })
    expect(link.tagName).toBe('A')
    expect(link).toHaveAttribute('href', '/portal/signin')
    expect(link).not.toHaveAttribute('role', 'button')
    const user = userEvent.setup()
    await user.tab()
    expect(link).toHaveFocus()
    const clicked = vi.fn((event: Event) => event.preventDefault())
    link.addEventListener('click', clicked)
    expect(clicked).not.toHaveBeenCalled()
    await user.keyboard('{Enter}')
    expect(clicked).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    queryClient.clear()
  })
})
