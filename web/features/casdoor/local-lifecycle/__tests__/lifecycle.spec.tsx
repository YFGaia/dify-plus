import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Request as NativeRequest } from 'next/dist/compiled/@edge-runtime/primitives/fetch'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vite-plus/test'
import { CSRF_COOKIE_NAME } from '@/config'
import { seedAccountProfileQuery } from '@/test/console/account-profile'
import { LocalLifecycle } from '..'

vi.mock('@/config', async (original) => ({
  ...(await original<typeof import('@/config')>()),
  API_PREFIX: 'http://localhost:3000/console/api',
}))
vi.mock('react-i18next', async () => {
  const { createReactI18nextMock } = await import('@/test/i18n-mock')
  const { default: extend } = await import('@/i18n/en-US/extend.json')
  return createReactI18nextMock(extend)
})
const account = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const identity = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
const workspace = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
const membership = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd'
const prefix = '/console/api/system-manage-extend/integration/casdoor/local-membership'
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { 'content-type': 'application/json' },
  })
let client: QueryClient
let requests: Request[]
let ownership: 'managed' | 'released'
let role: 'normal' | 'editor'
let reply: (request: Request) => Promise<Response>
let log: ReturnType<typeof vi.spyOn>
beforeEach(() => {
  vi.stubGlobal('Request', NativeRequest)
  client = new QueryClient()
  seedAccountProfileQuery(client, { id: account })
  requests = []
  ownership = 'managed'
  role = 'normal'
  log = vi.spyOn(console, 'error').mockImplementation(() => {})
  document.cookie = `${CSRF_COOKIE_NAME()}=synthetic-local-csrf; path=/`
  reply = async (request) => {
    const path = new URL(request.url).pathname
    if (path.endsWith('/review')) {
      const input = await request.json()
      return json({
        review_id: 'R'.repeat(43),
        etag: 3,
        operation: input.operation,
        current_role: role,
        target_role: input.operation === 'release' ? role : 'editor',
        expires_in: 60,
      })
    }
    if (path.endsWith('/release')) ownership = 'released'
    else {
      ownership = 'managed'
      role = 'editor'
    }
    return json({
      status: path.endsWith('/release') ? 'released' : 'adopted',
      membership_id: membership,
      ownership_epoch: 4,
    })
  }
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input, init) => {
      const request = new Request(input, init)
      requests.push(request.clone())
      const url = new URL(request.url)
      if (url.pathname.endsWith('/targets'))
        return json({
          items: [
            {
              identity_id: identity,
              workspace_id: workspace,
              account_id: account,
              account_name: 'Synthetic account',
              workspace_name: 'Synthetic workspace',
              current_role: role,
            },
          ],
          has_more: false,
          next_identity_id: null,
          next_workspace_id: null,
        })
      if (url.pathname === prefix)
        return json({
          identity_id: identity,
          workspace_id: workspace,
          account_id: account,
          etag: 3,
          current_role: role,
          ownership,
          local_no_intent: true,
        })
      if (url.pathname.endsWith('/account/casdoor-identity')) return json({})
      return reply(request)
    }),
  )
})
afterEach(() => {
  cleanup()
  client.clear()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.cookie = `${CSRF_COOKIE_NAME()}=; max-age=0; path=/`
})
function mount() {
  render(
    <QueryClientProvider client={client}>
      <LocalLifecycle />
    </QueryClientProvider>,
  )
}
function writes() {
  return requests.filter((request) => request.method === 'POST')
}
async function choose(user: ReturnType<typeof userEvent.setup>) {
  const select = await screen.findByRole('combobox', { name: 'Account and workspace' })
  await user.selectOptions(select, `${identity}/${workspace}`)
  await waitFor(() => expect(screen.getByRole('button', { name: 'Review release' })).toBeEnabled())
}
describe('actual generated LOCAL management UI and private HTTP adapter', () => {
  it('requires a server role review and explicit confirmation, then reads actual released/adopted state', async () => {
    const user = userEvent.setup()
    mount()
    await choose(user)
    await user.click(screen.getByRole('button', { name: 'Review release' }))
    const confirmation = await screen.findByRole('checkbox', {
      name: 'I confirm this reviewed change.',
    })
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeDisabled()
    expect(writes()).toHaveLength(1)
    await user.click(confirmation)
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    await screen.findByText('Management released; permissions retained.')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Review adoption' })).toBeEnabled(),
    )
    await user.click(screen.getByRole('button', { name: 'Review adoption' }))
    await screen.findByText('Current role: Normal member (normal) → reviewed role: Editor (editor)')
    await user.click(screen.getByRole('checkbox', { name: 'I confirm this reviewed change.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    await screen.findByText('Reviewed mapping applied; management restored.')
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Review release' })).toBeEnabled(),
    )
    expect(writes()).toHaveLength(4)
    expect(await writes()[0]!.json()).toEqual({
      identity_id: identity,
      workspace_id: workspace,
      operation: 'release',
      etag: 3,
    })
    expect(await writes()[1]!.json()).toEqual({ review_id: 'R'.repeat(43), etag: 3 })
    expect(
      requests.filter((request) => new URL(request.url).pathname === prefix).length,
    ).toBeGreaterThanOrEqual(3)
    for (const request of requests.filter((request) =>
      new URL(request.url).pathname.startsWith(prefix),
    )) {
      expect(request.cache).toBe('no-store')
      expect(request.credentials).toBe('include')
      expect(request.headers.get('X-CSRF-Token')).toBe('synthetic-local-csrf')
    }
    expect(log).not.toHaveBeenCalled()
  })
  it('hides old targets and rejects a late review after the real profile cache source changes', async () => {
    const user = userEvent.setup()
    let resolve!: (response: Response) => void
    reply = () =>
      new Promise((done) => {
        resolve = done
      })
    mount()
    await choose(user)
    await user.click(screen.getByRole('button', { name: 'Review release' }))
    await act(async () => {
      seedAccountProfileQuery(client, { id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee' })
      resolve(
        json({
          review_id: 'R'.repeat(43),
          etag: 3,
          operation: 'release',
          current_role: 'normal',
          target_role: 'normal',
          expires_in: 60,
        }),
      )
    })
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByText('Synthetic account — Synthetic workspace')).not.toBeInTheDocument()
    expect(writes()).toHaveLength(1)
    expect(log).not.toHaveBeenCalled()
  })
  it('rejects a contradictory review without showing confirmation or automatically retrying', async () => {
    const user = userEvent.setup()
    reply = async () =>
      json({
        review_id: 'R'.repeat(43),
        etag: 4,
        operation: 'adopt',
        current_role: 'normal',
        target_role: 'editor',
        expires_in: 60,
      })
    mount()
    await choose(user)
    await user.click(screen.getByRole('button', { name: 'Review release' }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(writes()).toHaveLength(1)
    expect(log).not.toHaveBeenCalled()
  })
})
