import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { CSRF_COOKIE_NAME } from '@/config'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { IdentityActions } from '../actions'

vi.mock('@/config', async (original) => ({
  ...(await original<typeof import('@/config')>()),
  API_PREFIX: 'http://localhost:3000/console/api',
}))
vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: extend } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(extend)
})
const id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const local = `/console/api/auth/casdoor/identity/${'A'.repeat(43)}`
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
let client: QueryClient
let requests: Request[]
let status: unknown
let reply: (request: Request) => Promise<Response>
let navigation: ReturnType<typeof vi.spyOn>
let log: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.stubGlobal('Request', NativeRequest)
  client = new QueryClient()
  seedAccountProfileQuery(client, { id })
  requests = []
  status = { link: true, reauthenticate: false, unlink: false, reason: null }
  reply = async () => json({ handoff_path: local })
  document.cookie = `${CSRF_COOKIE_NAME()}=synthetic-csrf; path=/`
  navigation = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
  log = vi.spyOn(console, 'error').mockImplementation(() => {})
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init)
      requests.push(request)
      return new URL(request.url).pathname.endsWith('/actions') ? json(status) : reply(request)
    }),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
  document.cookie = `${CSRF_COOKIE_NAME()}=; max-age=0; path=/`
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
const mount = () =>
  render(
    <QueryClientProvider client={client}>
      <IdentityActions enabled={true} />
    </QueryClientProvider>,
  )
const writes = () => requests.filter((request) => request.method === 'POST')

describe('actual source-session identity action transport and UI', () => {
  it('posts the empty CSRF-protected LINK action and navigates only to its trusted opaque local GET', async () => {
    const user = userEvent.setup()
    mount()
    const button = await screen.findByRole('button', { name: 'Link enterprise account' })
    await waitFor(() => expect(button).toBeEnabled())
    await user.click(button)
    await waitFor(() => expect(navigation).toHaveBeenCalledWith(`http://localhost:3000${local}`))
    expect(writes()).toHaveLength(1)
    expect(new URL(writes()[0]!.url).pathname).toBe('/console/api/account/casdoor-identity/link')
    expect(await writes()[0]!.json()).toEqual({})
    expect(writes()[0]!.headers.get('X-CSRF-Token')).toBe('synthetic-csrf')
    expect(writes()[0]!.credentials).toBe('include')
    expect(writes()[0]!.cache).toBe('no-store')
    expect(log).not.toHaveBeenCalled()
  })
  it('defaults reviewed capability unavailable to closed unlink while showing its actionable reason', async () => {
    status = {
      link: false,
      reauthenticate: false,
      unlink: false,
      reason: 'reauthentication_unavailable',
    }
    mount()
    await screen.findByText(
      'Your administrator must validate recent authentication before unlinking is available.',
    )
    expect(screen.getByRole('button', { name: 'Verify account for unlinking' })).toBeDisabled()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })
  it('asks the administrator to release or transfer managed access and reconcile history while unlink remains blocked', async () => {
    status = {
      link: false,
      reauthenticate: false,
      unlink: false,
      reason: 'managed_history_requires_release',
    }
    mount()
    expect(
      await screen.findByText(
        'Contact your administrator to release or transfer managed workspace access and reconcile synchronization history before unlinking.',
      ),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Verify account for unlinking' })).toBeDisabled()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })
  it('requires explicit confirmation after actual reauthentication and refreshes current availability after unlink', async () => {
    const user = userEvent.setup()
    status = { link: false, reauthenticate: true, unlink: true, reason: null }
    reply = async () => {
      status = { link: true, reauthenticate: false, unlink: false, reason: null }
      return json({ status: 'unlinked' })
    }
    mount()
    const confirmation = await screen.findByRole('checkbox', {
      name: 'I confirm that I want to unlink this account.',
    })
    const button = screen.getByRole('button', { name: 'Unlink enterprise account' })
    expect(button).toBeDisabled()
    await user.click(confirmation)
    await user.click(button)
    await screen.findByText('Not linked')
    expect(writes()).toHaveLength(1)
    expect(new URL(writes()[0]!.url).pathname).toBe('/console/api/account/casdoor-identity/unlink')
    expect(await writes()[0]!.json()).toEqual({})
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Link enterprise account' })).toBeEnabled(),
    )
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(navigation).not.toHaveBeenCalled()
  })
  it.each([
    'https://evil.example/steal',
    `${local}?token=raw`,
    '/console/api/auth/casdoor/identity/short',
  ])(
    'refuses unsafe server handoff %s without navigation or raw error logs',
    async (handoff_path) => {
      const user = userEvent.setup()
      reply = async () => json({ handoff_path })
      mount()
      await waitFor(() =>
        expect(screen.getByRole('button', { name: 'Link enterprise account' })).toBeEnabled(),
      )
      await user.click(screen.getByRole('button', { name: 'Link enterprise account' }))
      await screen.findByRole('alert')
      expect(navigation).not.toHaveBeenCalled()
      expect(log).not.toHaveBeenCalled()
      expect(screen.queryByText(handoff_path)).not.toBeInTheDocument()
    },
  )
  it('rejects a late action response when the actual profile cache anchor changes', async () => {
    const user = userEvent.setup()
    let resolve!: (value: Response) => void
    reply = () =>
      new Promise((done) => {
        resolve = done
      })
    mount()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Link enterprise account' })).toBeEnabled(),
    )
    await user.click(screen.getByRole('button', { name: 'Link enterprise account' }))
    await act(async () => {
      seedAccountProfileQuery(client, { id: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb' })
      resolve(json({ handoff_path: local }))
    })
    expect(navigation).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Link enterprise account' })).toBeDisabled()
    expect(writes()).toHaveLength(1)
  })
  it('rejects contradictory availability before enabling any identity mutation', async () => {
    status = { link: true, reauthenticate: true, unlink: true, reason: null }
    mount()
    await screen.findByRole('alert')
    expect(screen.getByRole('button', { name: 'Link enterprise account' })).toBeDisabled()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })
})
