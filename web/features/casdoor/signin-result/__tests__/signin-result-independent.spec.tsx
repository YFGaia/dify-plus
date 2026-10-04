import { focusManager, onlineManager } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import en from '@/i18n/en-US/extend.json'

const handoff = 'A'.repeat(43)
const path = '/portal/signin/casdoor-result'
const sensitive = 'synthetic-private@example.test'
const copy = (key: keyof typeof en) => en[key]
const response = (body: unknown) =>
  new Response(JSON.stringify(body), { headers: { 'content-type': 'application/json' } })

async function loadFeatureWithRejectedAuthLoader() {
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
          throw new Error(`synthetic loader failure ${sensitive}`)
        },
      },
    }
  })
  const feature = await import('..')
  return { ...feature, loaderCalls }
}

function setURL(url: string) {
  window.history.replaceState(null, '', url)
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('Request', NativeRequest)
  setURL(`${path}?handoff=${handoff}`)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  for (const name of [
    '@/config',
    '@/utils/var',
    '@/utils/client',
    'react-i18next',
    '@/service/refresh-token',
    '@dify/contracts/api/console/orpc.gen',
  ])
    vi.doUnmock(name)
  focusManager.setFocused(undefined)
  onlineManager.setOnline(true)
})

describe('independent one-use Casdoor sign-in result behavior', () => {
  it('keeps an actual generated auth-loader failure private and does not replay it under StrictMode', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({}))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { CasdoorSigninResult, loaderCalls } = await loadFeatureWithRejectedAuthLoader()
    const view = render(
      <StrictMode>
        <CasdoorSigninResult />
      </StrictMode>,
    )

    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.generic'))
    expect(screen.queryByText(new RegExp(sensitive))).not.toBeInTheDocument()
    expect(loaderCalls).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    expect(log).not.toHaveBeenCalled()

    view.rerender(
      <StrictMode>
        <CasdoorSigninResult />
      </StrictMode>,
    )
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.generic')),
    )
    focusManager.setFocused(false)
    focusManager.setFocused(true)
    onlineManager.setOnline(false)
    onlineManager.setOnline(true)

    expect(loaderCalls).toHaveBeenCalledTimes(1)
    expect(fetch).not.toHaveBeenCalled()
    expect(screen.queryByText(new RegExp(sensitive))).not.toBeInTheDocument()
    expect(log).not.toHaveBeenCalled()
  })

  it('keeps sensitive callback query and fragment data out of the expired state and fixed recovery link', async () => {
    setURL(
      `${path}?handoff=${handoff}&code=authorization_pending&correlation_id=12345678-abcd-4123-8123-123456789abc&email=${encodeURIComponent(sensitive)}#${encodeURIComponent(sensitive)}`,
    )
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(response({}))
    const { CasdoorSigninResult, loaderCalls } = await loadFeatureWithRejectedAuthLoader()
    render(<CasdoorSigninResult />)

    expect(await screen.findByRole('alert')).toHaveTextContent(copy('casdoorSigninResult.expired'))
    expect(screen.queryByText(new RegExp(sensitive))).not.toBeInTheDocument()
    expect(screen.queryByText(/12345678-abcd-4123-8123-123456789abc/)).not.toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: copy('casdoorSigninResult.freshSignin') }),
    ).toHaveAttribute('href', '/portal/signin')
    expect(loaderCalls).not.toHaveBeenCalled()
    expect(fetch).not.toHaveBeenCalled()
  })
})
